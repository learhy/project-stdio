"""Tests for Bundle 4.2: RemoteSSHWorkerRunner, RemoteWorkerHandle, fleet CLI."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.runner import (
    RemoteSSHWorkerRunner,
    RemoteWorkerHandle,
    capability_to_bwrap_args,
    WorkerSpawnResult,
    _generate_token,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FleetHost,
    RemoteFleetSettings,
    EgressProxySettings,
)


def make_fleet_host(name="test-host", addr="10.0.0.1", max_workers=2):
    return FleetHost(
        name=name, addr=addr, ssh_user="studio",
        ssh_key_path="/tmp/test-key", capabilities=["python"],
        max_concurrent_workers=max_workers, arch="x86_64",
        worktree_mode="clone",
    )


def make_fleet(hosts=None):
    if hosts is None:
        hosts = [make_fleet_host()]
    return RemoteFleetSettings(
        enabled=True,
        hosts=list(hosts),
        selection_policy="least_loaded",
    )


def make_manifest():
    return CapabilityManifest(
        schema_version="1.0",
        grants={
            "filesystem": {"reads": [], "writes": []},
            "network": {"egress": [], "ingress": {"enabled": False}, "dns": {"enabled": True}},
            "process": {"exec": [], "spawn_subtasks": {"enabled": False, "max_depth": 0, "max_count": 0}},
            "secrets": [],
            "rpc": {"methods": ["worker.*"], "artifact_access": {"reads": [], "writes": []}},
            "resources": {"cpu_limit": 0, "memory_limit": 0, "disk_limit": 0, "wall_time_limit": 0,
                          "llm_token_budget": {"input_tokens": 0, "output_tokens": 0, "by_model": {}}},
        },
        metadata={"rationale": "test"},
    )


class TestRemoteWorkerHandle:
    def test_handle_stores_attributes(self):
        conn = MagicMock()
        host = make_fleet_host()
        handle = RemoteWorkerHandle(conn, 12345, host, "/tmp/work", "w1")
        assert handle.remote_pid == 12345
        assert handle.worker_id == "w1"
        assert handle.workdir == "/tmp/work"
        assert handle.host is host
        assert handle.returncode is None

    def test_returncode_settable(self):
        handle = RemoteWorkerHandle(MagicMock(), 1, make_fleet_host(), "/tmp", "w1")
        assert handle.returncode is None
        handle.returncode = 0
        assert handle.returncode == 0

    @pytest.mark.asyncio
    async def test_cancel_sends_sigterm_then_sigkill(self):
        conn = MagicMock()
        conn.run = AsyncMock()
        # First call (kill -TERM) succeeds, second call (kill -0) says process dead
        conn.run.side_effect = [
            MagicMock(exit_status=0),  # SIGTERM sent
            MagicMock(exit_status=1),  # kill -0: process gone
        ]
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp", "w1")
        await handle.cancel()
        assert handle.returncode == -1
        assert conn.run.call_count >= 2

    @pytest.mark.asyncio
    async def test_cancel_force_kill_after_timeout(self):
        conn = MagicMock()
        conn.run = AsyncMock()
        # kill -0 always returns 0 (process alive), so we escalate to SIGKILL
        conn.run.side_effect = [
            MagicMock(exit_status=0),  # SIGTERM sent
        ] + [MagicMock(exit_status=0)] * 31  # kill -0 always alive
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp", "w1")
        # Patch asyncio.sleep to avoid real delay
        with patch("asyncio.sleep", AsyncMock()):
            await handle.cancel()
        assert handle.returncode == -9

    @pytest.mark.asyncio
    async def test_is_alive_returns_true_when_running(self):
        conn = MagicMock()
        conn.run = AsyncMock(return_value=MagicMock(exit_status=0))
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp", "w1")
        alive = await handle.is_alive()
        assert alive is True
        assert handle.returncode is None

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_when_dead(self):
        conn = MagicMock()
        conn.run = AsyncMock(return_value=MagicMock(exit_status=1))
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp", "w1")
        alive = await handle.is_alive()
        assert alive is False
        assert handle.returncode == 1

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_when_already_completed(self):
        handle = RemoteWorkerHandle(MagicMock(), 12345, make_fleet_host(), "/tmp", "w1")
        handle.returncode = 0
        alive = await handle.is_alive()
        assert alive is False

    @pytest.mark.asyncio
    async def test_cleanup_removes_workdir(self):
        conn = MagicMock()
        conn.run = AsyncMock()
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp/work", "w1")
        await handle.cleanup()
        conn.run.assert_called_with("rm -rf /tmp/work", check=False)


class TestRemoteSSHWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        fleet = make_fleet()
        return RemoteSSHWorkerRunner(db_mock, fleet)

    def test_init_creates_semaphores_per_host(self, db_mock):
        fleet = make_fleet([make_fleet_host("h1"), make_fleet_host("h2")])
        runner = RemoteSSHWorkerRunner(db_mock, fleet)
        assert len(runner._host_semaphores) == 2
        assert "h1" in runner._host_semaphores
        assert "h2" in runner._host_semaphores
        assert runner._host_health["h1"] is True

    def test_select_host_least_loaded(self, runner):
        host = runner._select_host()
        assert host is not None
        assert host.name == "test-host"

    def test_select_host_none_when_all_full(self, runner):
        # Exhaust all semaphore slots by directly manipulating internal state
        sem = runner._host_semaphores["test-host"]
        sem._value = 0  # all slots taken
        host = runner._select_host()
        assert host is None
        sem._value = 2  # restore

    def test_select_host_none_when_all_unhealthy(self, runner):
        runner._host_health["test-host"] = False
        host = runner._select_host()
        assert host is None

    def test_select_host_no_hosts(self, db_mock):
        fleet = make_fleet([])
        runner = RemoteSSHWorkerRunner(db_mock, fleet)
        assert runner._select_host() is None

    @pytest.mark.asyncio
    async def test_preflight_checks_all_binaries(self, runner):
        conn = MagicMock()
        conn.run = AsyncMock(return_value=MagicMock(exit_status=0))
        missing = await runner._preflight(conn)
        assert missing == []

    @pytest.mark.asyncio
    async def test_preflight_detects_missing_binaries(self, runner):
        conn = MagicMock()
        conn.run = AsyncMock(side_effect=[
            MagicMock(exit_status=0),   # bwrap: found
            MagicMock(exit_status=1),   # studio-worker: missing
            MagicMock(exit_status=0),   # studio-proxy: found
        ])
        missing = await runner._preflight(conn)
        assert "studio-worker" in missing
        assert "bwrap" not in missing

    @pytest.mark.asyncio
    async def test_spawn_worker_no_healthy_host(self, runner):
        runner._host_health["test-host"] = False
        result = await runner.spawn_worker(
            "w1", "b1", "n1", make_manifest(), "/tmp/work",
        )
        assert result.error == "No healthy fleet host available with capacity"
        assert result.process is None

    @pytest.mark.asyncio
    async def test_spawn_worker_ssh_failure(self, runner):
        with patch("asyncssh.connect", AsyncMock(side_effect=OSError("Connection refused"))):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )
            assert "SSH connection" in result.error
            assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_spawn_worker_preflight_fails(self, runner):
        mock_conn = MagicMock()
        mock_conn.run = AsyncMock(side_effect=[
            MagicMock(exit_status=1),  # bwrap: missing
            MagicMock(exit_status=0),  # studio-worker: found
            MagicMock(exit_status=0),  # studio-proxy: found
        ])
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )
            assert "Missing binaries" in result.error
            assert "bwrap" in result.error

    @pytest.mark.asyncio
    async def test_spawn_worker_success(self, runner):
        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        # preflight: all found
        # mkdir: ok
        # git clone: ok
        # echo task-spec: ok
        # echo manifest: ok
        # proxy: ok
        # bwrap + worker: returns PID
        mock_conn.run = AsyncMock(side_effect=[
            MagicMock(exit_status=0),  # bwrap found
            MagicMock(exit_status=0),  # studio-worker found
            MagicMock(exit_status=0),  # studio-proxy found
            MagicMock(exit_status=0),  # mkdir
            MagicMock(exit_status=0),  # git clone
            MagicMock(exit_status=0),  # echo task-spec
            MagicMock(exit_status=0),  # echo manifest
            MagicMock(exit_status=0),  # nohup proxy
            MagicMock(exit_status=0, stdout="54321\n"),  # bwrap worker (captures PID)
        ])

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)), \
             patch("asyncio.sleep", AsyncMock()):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )
            assert result.error == ""
            assert isinstance(result.process, RemoteWorkerHandle)
            assert result.process.remote_pid == 54321
            assert result.worker_id == "w1"

    @pytest.mark.asyncio
    async def test_kill_worker_handles_remote_handle(self, runner):
        conn = MagicMock()
        conn.run = AsyncMock(side_effect=[
            MagicMock(exit_status=0),  # SIGTERM
            MagicMock(exit_status=1),  # kill -0 dead
        ])
        conn.close = MagicMock()
        conn.wait_closed = AsyncMock()
        handle = RemoteWorkerHandle(conn, 12345, make_fleet_host(), "/tmp/work", "w1")

        await runner.kill_worker(handle, "w1")
        assert handle.returncode == -1
        conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_ping_hosts_healthy(self, runner):
        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            statuses = await runner.ping_hosts()
            assert statuses["test-host"] == "healthy"
            assert runner._host_health["test-host"] is True

    @pytest.mark.asyncio
    async def test_ping_hosts_degraded(self, runner):
        with patch("asyncssh.connect", AsyncMock(side_effect=OSError("timeout"))):
            statuses = await runner.ping_hosts()
            assert statuses["test-host"] == "degraded"
            assert runner._host_health["test-host"] is False


class TestFleetCliHandlers:
    @pytest.mark.asyncio
    async def test_fleet_status_no_fleet(self):
        from studio.orchestrator.main import _cli_fleet_status, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.runner = MagicMock()  # not a RemoteSSHWorkerRunner
        result = await _cli_fleet_status(app, {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_fleet_status_with_hosts(self):
        from studio.orchestrator.main import _cli_fleet_status, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.settings = MagicMock()
        app.settings.remote_fleet = make_fleet([make_fleet_host("h1"), make_fleet_host("h2")])
        app.runner = MagicMock(spec=RemoteSSHWorkerRunner)
        app.runner.ping_hosts = AsyncMock(return_value={"h1": "healthy", "h2": "healthy"})
        app.runner._host_semaphores = {"h1": asyncio.Semaphore(4), "h2": asyncio.Semaphore(4)}
        app.runner._host_last_ping = {"h1": 1000.0, "h2": 1000.0}

        result = await _cli_fleet_status(app, {})
        assert "hosts" in result
        assert len(result["hosts"]) == 2

    @pytest.mark.asyncio
    async def test_fleet_add_new_host(self):
        from studio.orchestrator.main import _cli_fleet_add, _persist_fleet_settings, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.settings = MagicMock()
        app.settings.remote_fleet = make_fleet([])
        app.runner = MagicMock()  # not a RemoteSSHWorkerRunner

        with patch("studio.orchestrator.main._persist_fleet_settings"):
            result = await _cli_fleet_add(app, {"name": "new-host", "addr": "10.0.0.2"})
            assert result["added"] is True
            assert result["name"] == "new-host"
            assert len(app.settings.remote_fleet.hosts) == 1

    @pytest.mark.asyncio
    async def test_fleet_add_duplicate(self):
        from studio.orchestrator.main import _cli_fleet_add, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.settings = MagicMock()
        app.settings.remote_fleet = make_fleet([make_fleet_host("h1")])
        app.runner = MagicMock()

        with patch("studio.orchestrator.main._persist_fleet_settings"):
            result = await _cli_fleet_add(app, {"name": "h1", "addr": "10.0.0.3"})
            assert "error" in result
            assert "already exists" in result["error"]

    @pytest.mark.asyncio
    async def test_fleet_remove_host(self):
        from studio.orchestrator.main import _cli_fleet_remove, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.settings = MagicMock()
        app.settings.remote_fleet = make_fleet([make_fleet_host("h1")])
        app.runner = MagicMock()

        with patch("studio.orchestrator.main._persist_fleet_settings"):
            result = await _cli_fleet_remove(app, {"name": "h1"})
            assert result["removed"] is True
            assert len(app.settings.remote_fleet.hosts) == 0

    @pytest.mark.asyncio
    async def test_fleet_remove_not_found(self):
        from studio.orchestrator.main import _cli_fleet_remove, Orchestrator
        app = MagicMock(spec=Orchestrator)
        app.settings = MagicMock()
        app.settings.remote_fleet = make_fleet([])
        app.runner = MagicMock()

        result = await _cli_fleet_remove(app, {"name": "nonexistent"})
        assert "error" in result
