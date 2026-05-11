"""Tests for runner.py — bubblewrap arg building, worker spawning, token generation."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.runner import (
    LocalBwrapWorkerRunner,
    NoopWorkerRunner,
    WorkerSpawnResult,
    _generate_token,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    FilesystemGrants,
    NetworkGrants,
    ProcessGrants,
    RpcGrants,
    ResourceGrants,
    Grants,
    ManifestSubject,
    ManifestMetadata,
    EgressProxySettings,
)


def make_manifest(**fs_overrides) -> CapabilityManifest:
    """Create a test capability manifest."""
    fs_grants = {
        "reads": [{"path": "/usr/lib", "recursive": True}],
        "writes": [{"path": "/tmp/build", "recursive": True, "create": True}],
        **fs_overrides,
    }
    return CapabilityManifest(
        schema_version="1.0",
        subject=ManifestSubject(kind="bundle", id="test"),
        grants=Grants(
            filesystem=FilesystemGrants(
                reads=[FilesystemPathGrant(**r) for r in fs_grants.get("reads", [])],
                writes=[FilesystemWriteGrant(**w) for w in fs_grants.get("writes", [])],
            ),
            network=NetworkGrants(),
            process=ProcessGrants(),
            rpc=RpcGrants(methods=["worker.*", "cap.*"]),
            resources=ResourceGrants(),
        ),
        metadata=ManifestMetadata(rationale="test"),
    )


class TestGenerateToken:
    def test_token_is_hex_string(self):
        token = _generate_token()
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_tokens_are_unique(self):
        tokens = {_generate_token() for _ in range(100)}
        assert len(tokens) == 100


class TestWorkerSpawnResult:
    def test_attributes(self):
        proc = MagicMock()
        result = WorkerSpawnResult("w1", "tok", "n1", proc)
        assert result.worker_id == "w1"
        assert result.token == "tok"
        assert result.node_id == "n1"
        assert result.process is proc


class TestLocalBwrapWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    def test_default_worker_command(self, db_mock):
        runner = LocalBwrapWorkerRunner(db_mock, "/tmp/socket")
        assert runner.worker_command == ["studio-worker"]

    def test_custom_worker_command(self, db_mock):
        runner = LocalBwrapWorkerRunner(db_mock, "/tmp/socket", worker_command=["/usr/bin/code-buddy"])
        assert runner.worker_command == ["/usr/bin/code-buddy"]

    def test_bwrap_basic_args(self, runner):
        manifest = make_manifest()
        args = runner._build_bwrap_args(manifest, "/tmp/worktree", "token123")

        assert args[0] == "bwrap"
        assert "--die-with-parent" in args
        assert "--tmpfs" in args
        assert "/tmp" in args
        assert "--bind" in args
        assert "/tmp/worktree" in args
        assert "/work" in args
        assert "--chdir" in args
        assert "--proc" in args
        assert "--dev" in args

    def test_bwrap_readonly_mounts(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = runner._build_bwrap_args(manifest, "/tmp/wt", "tok")
        assert "--ro-bind" in args
        assert "/usr/lib" in args

    def test_bwrap_writable_mounts(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = runner._build_bwrap_args(manifest, "/tmp/wt", "tok")
        # Should have bind for /tmp/build
        assert "--bind" in args
        assert "/tmp/build" in args

    def test_bwrap_always_unshare_net(self, runner):
        manifest = make_manifest()
        args = runner._build_bwrap_args(manifest, "/tmp/wt", "tok")
        assert "--unshare-net" in args

    def test_bwrap_socket_directory_bound(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = runner._build_bwrap_args(manifest, "/tmp/wt", "tok")
        assert "/run/studio" in args

    @pytest.mark.asyncio
    async def test_spawn_worker_inserts_db_row(self, runner, db_mock):
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                result = await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

                # Check DB insert
                insert_call = db_mock.execute.call_args_list[0]
                assert "INSERT INTO workers" in insert_call[0][0]
                assert insert_call[0][1][0] == "w1"
                assert insert_call[0][1][1] == "b1"
                assert insert_call[0][1][2] == "n1"
                assert insert_call[0][1][6] == "pending"

                assert result.worker_id == "w1"
                assert result.process is mock_proc
                assert len(result.token) == 64

    @pytest.mark.asyncio
    async def test_spawn_worker_env_includes_token(self, runner, db_mock):
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

                # Only one call (worker process, no worktree creation in test mode)
                _, _, kwargs = mock_spawn.mock_calls[0]
                assert "env" in kwargs
                env = kwargs["env"]
                assert "STUDIO_WORKER_TOKEN" in env
                assert "STUDIO_SOCKET_PATH" in env
                assert env["STUDIO_WORKER_ID"] == "w1"
                assert "STUDIO_WORKTREE_PATH" in env
                assert "STUDIO_BASE_BRANCH" in env
                assert "STUDIO_PROXY_SOCKET" in env
                assert "http_proxy" in env
                assert "https_proxy" in env

    @pytest.mark.asyncio
    async def test_spawn_worker_includes_task_spec_in_env(self, runner, db_mock):
        manifest = make_manifest()
        task_spec = {"objective": "build server", "inputs": {}}
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt", task_spec=task_spec)

                _, _, kwargs = mock_spawn.mock_calls[0]
                env = kwargs["env"]
                assert "STUDIO_TASK_SPEC" in env
                assert "build server" in env["STUDIO_TASK_SPEC"]

    @pytest.mark.asyncio
    async def test_kill_worker_terminate(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        await runner.kill_worker(proc)
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_worker_with_proxy_cleanup(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        # Add a mock proxy process
        proxy_proc = MagicMock()
        proxy_proc.terminate = MagicMock()
        proxy_proc.wait = AsyncMock(return_value=0)
        runner._proxy_processes["w1"] = proxy_proc

        with patch("os.unlink"):
            await runner.kill_worker(proc, "w1")

        proc.terminate.assert_called_once()
        proxy_proc.terminate.assert_called_once()
        assert "w1" not in runner._proxy_processes

    @pytest.mark.asyncio
    async def test_kill_worker_timeout_then_kill(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])

        await runner.kill_worker(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_worker_already_exited(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock(side_effect=ProcessLookupError())

        await runner.kill_worker(proc)
        # Should not raise


class TestNoopWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return NoopWorkerRunner(db_mock)

    @pytest.mark.asyncio
    async def test_spawn_returns_result_with_no_process(self, runner, db_mock):
        manifest = make_manifest()
        result = await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

        assert result.worker_id == "w1"
        assert len(result.token) == 64
        assert result.process is None

        # Check DB insert
        insert_call = db_mock.execute.call_args_list[0]
        assert "INSERT INTO workers" in insert_call[0][0]
        assert insert_call[0][1][0] == "w1"

    @pytest.mark.asyncio
    async def test_noop_runner_inserts_pending_state(self, runner, db_mock):
        manifest = make_manifest()
        await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")
        insert_call = db_mock.execute.call_args_list[0]
        # Index 6 = state (shifted by token_expires_at at index 4)
        assert insert_call[0][1][6] == "pending"
