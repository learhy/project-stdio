"""Tests for main.py — orchestrator lifecycle and CLI dispatch.

Phase 5: DagExecutor, Scheduler, Reconciler removed. Tests updated.
"""
import json
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from pathlib import Path

from studio.orchestrator.main import (
    Orchestrator,
    _cli_submit,
    _cli_approve,
    _cli_reject,
    _cli_list,
    _cli_show,
    _cli_show_worker,
    _cli_kill,
    _cli_status,
    _cli_version,
    _format_age,
)


class TestFormatAge:
    def test_seconds(self):
        assert _format_age(30) == "30s"

    def test_minutes(self):
        assert _format_age(90) == "1m"

    def test_hours(self):
        assert _format_age(7200) == "2h"

    def test_days(self):
        assert _format_age(90000) == "1d"


class TestOrchestratorLifecycle:
    @pytest.mark.asyncio
    async def test_start_initializes_components(self):
        """Phase 5: executor/scheduler/reconciler removed, but runner/RPC/DB still init."""
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-start.db"
        app.settings.orchestrator.socket_path = "/tmp/test-start.sock"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner") as mock_runner_cls, \
             patch("studio.orchestrator.main.RemoteSSHWorkerRunner") as mock_ssh_cls, \
             patch("studio.orchestrator.main.K8sJobWorkerRunner") as mock_k8s_cls, \
             patch("studio.orchestrator.main.RunnerSelector") as mock_selector_cls, \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.execute = AsyncMock()
            mock_db.close = AsyncMock()
            mock_db.conn = MagicMock()
            mock_db.conn.commit = AsyncMock()
            mock_create_db.return_value = mock_db

            mock_dispatcher = MagicMock()
            mock_handlers = MagicMock()
            mock_conn_mgr = MagicMock()
            mock_create_rpc.return_value = (mock_dispatcher, mock_handlers, mock_conn_mgr)

            mock_runner = MagicMock()
            mock_runner._check_bwrap = AsyncMock(return_value=True)
            mock_runner_cls.return_value = mock_runner

            mock_selector = MagicMock()
            mock_selector.runner_names = ["local"]
            mock_selector_cls.return_value = mock_selector

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            await app.start()

            assert app.db is not None
            assert app.sm is not None
            assert app.dispatcher is not None
            assert app.handlers is not None
            assert app.runner is not None
            # Phase 5: executor, scheduler, reconciler no longer exist
            mock_start_server.assert_called_once()

            await app.stop()
            mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_closes_all_components(self):
        """Phase 5: stop closes DB, server, but no scheduler/reconciler."""
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-stop.db"
        app.settings.orchestrator.socket_path = "/tmp/test-stop.sock"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner"), \
             patch("studio.orchestrator.main.RemoteSSHWorkerRunner"), \
             patch("studio.orchestrator.main.K8sJobWorkerRunner"), \
             patch("studio.orchestrator.main.RunnerSelector") as mock_selector_cls, \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.execute = AsyncMock()
            mock_db.close = AsyncMock()
            mock_db.conn = MagicMock()
            mock_db.conn.commit = AsyncMock()
            mock_create_db.return_value = mock_db

            mock_selector = MagicMock()
            mock_selector.runner_names = ["local"]
            mock_selector_cls.return_value = mock_selector

            mock_conn_mgr = MagicMock()
            mock_conn_mgr._by_worker_id = {}
            mock_create_rpc.return_value = (MagicMock(), MagicMock(), mock_conn_mgr)

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            await app.start()
            await app.stop()

            mock_db.close.assert_called_once()


class TestTcpTlsListener:
    @pytest.mark.asyncio
    async def test_remote_workers_disabled_no_tcp_server(self):
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-no-tcp.db"
        app.settings.orchestrator.socket_path = "/tmp/test-no-tcp.sock"
        app.settings.remote_workers.enabled = False

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner"), \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.asyncio.start_server") as mock_tcp_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.close = AsyncMock()
            mock_db.execute = AsyncMock()
            mock_db.conn = MagicMock()
            mock_db.conn.commit = AsyncMock()
            mock_create_db.return_value = mock_db
            mock_create_rpc.return_value = (MagicMock(), MagicMock(), MagicMock())

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            await app.start()

            mock_start_server.assert_called_once()
            mock_tcp_server.assert_not_called()
            assert app._tcp_server is None

            await app.stop()

    @pytest.mark.asyncio
    async def test_remote_workers_enabled_starts_tcp_server(self):
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-tcp.db"
        app.settings.orchestrator.socket_path = "/tmp/test-tcp.sock"
        app.settings.remote_workers.enabled = True
        app.settings.remote_workers.listen_addr = "0.0.0.0:7811"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner"), \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.asyncio.start_server") as mock_tcp_server, \
             patch("studio.orchestrator.main.tls_helpers.generate_ca") as mock_gen_ca, \
             patch("studio.orchestrator.main.tls_helpers.create_server_tls_context") as mock_create_ctx, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.close = AsyncMock()
            mock_db.execute = AsyncMock()
            mock_db.conn = MagicMock()
            mock_db.conn.commit = AsyncMock()
            mock_create_db.return_value = mock_db
            mock_create_rpc.return_value = (MagicMock(), MagicMock(), MagicMock())

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            mock_tcp = MagicMock()
            mock_tcp.close = MagicMock()
            mock_tcp.wait_closed = AsyncMock()
            mock_tcp_server.return_value = mock_tcp

            mock_tls_ctx = MagicMock()
            mock_create_ctx.return_value = mock_tls_ctx

            await app.start()

            mock_gen_ca.assert_called_once()
            mock_create_ctx.assert_called_once()
            mock_tcp_server.assert_called_once_with(
                app._handle_connection,
                host="0.0.0.0",
                port=7811,
                ssl=mock_tls_ctx,
            )
            assert app._tcp_server is not None

            # Verify audit trail was written
            mock_db.execute.assert_called()

            await app.stop()

    def test_create_server_tls_context(self, tmp_path):
        from studio.orchestrator.tls import create_server_tls_context, generate_ca
        import ssl as ssl_mod

        # Generate CA
        ca_cert_path = tmp_path / "ca.crt"
        ca_key_path = tmp_path / "ca.key"
        generate_ca(str(ca_cert_path), str(ca_key_path))

        # Generate server cert signed by CA
        import subprocess
        server_key_path = tmp_path / "server.key"
        server_csr_path = tmp_path / "server.csr"
        server_cert_path = tmp_path / "server.crt"

        subprocess.run([
            "openssl", "genrsa", "-out", str(server_key_path), "2048",
        ], check=True, capture_output=True)
        subprocess.run([
            "openssl", "req", "-new", "-key", str(server_key_path),
            "-out", str(server_csr_path),
            "-subj", "/CN=test-orchestrator",
        ], check=True, capture_output=True)
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(server_csr_path),
            "-CA", str(ca_cert_path), "-CAkey", str(ca_key_path),
            "-CAcreateserial", "-out", str(server_cert_path),
            "-days", "1", "-sha256",
        ], check=True, capture_output=True)

        ctx = create_server_tls_context(
            str(ca_cert_path), str(server_cert_path), str(server_key_path)
        )
        assert isinstance(ctx, ssl_mod.SSLContext)
        assert ctx.minimum_version == ssl_mod.TLSVersion.TLSv1_2
        assert ctx.verify_mode == ssl_mod.CERT_REQUIRED


class TestCliHandlers:
    @pytest.fixture
    def app_mock(self):
        app = MagicMock()
        app.sm = MagicMock()
        app.sm.transition_1_submit = AsyncMock()
        app.sm.transition_1_submit_idea = AsyncMock()
        app.sm.transition_1a_approve = AsyncMock()
        app.sm.transition_1b_reject = AsyncMock()
        app.sm.transition_4_approve_from_review = AsyncMock()
        app.sm.transition_6_start_execution = AsyncMock()
        app.sm.transition_25_fail_execution = AsyncMock()
        app.sm.now = MagicMock(return_value=1700000000)
        app.runner = MagicMock()
        app.db = MagicMock()
        app.db.fetch_all = AsyncMock()
        app.db.fetch_one = AsyncMock()
        app.db.execute = AsyncMock()
        app.db.conn = MagicMock()
        app.db.conn.commit = AsyncMock()
        app.settings = MagicMock()
        app.settings.orchestrator = MagicMock()
        app.settings.orchestrator.socket_path = "/tmp/test.sock"
        app.settings.ollama_cloud = MagicMock()
        app.settings.ollama_cloud.base_url = "https://ollama.com/api"
        return app

    @pytest.mark.asyncio
    async def test_cli_submit_creates_bundle(self, app_mock):
        submission = {
            "bundle_input": {"idea": "Test", "target_repo": "my-repo"},
            "task_dag": {
                "nodes": [{"id": "task-1", "kind": "worker", "spec": {}}],
                "edges": [],
            },
        }
        result = await _cli_submit(app_mock, {"submission": submission})
        assert "bundle_id" in result
        app_mock.sm.transition_1_submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_cli_submit_idea_only_path(self, app_mock):
        """Empty task_dag triggers bundle-input-only path with bundler worker."""
        submission = {"bundle_input": {"idea": "Test idea"}, "task_dag": {"nodes": [], "edges": []}}
        with patch("studio.orchestrator.main._spawn_bundler", new_callable=AsyncMock):
            result = await _cli_submit(app_mock, {"submission": submission})
        assert "bundle_id" in result
        assert result["mode"] == "planning"
        app_mock.sm.transition_1_submit_idea.assert_called_once()
        app_mock.sm.transition_1_submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cli_approve_starts_execution(self, app_mock):
        """Phase 5: approval transitions state, LangGraph handles execution dispatch."""
        result = await _cli_approve(app_mock, {"bundle_id": "01TEST"})
        assert result["approved"] is True
        app_mock.sm.transition_1a_approve.assert_called_once_with("01TEST", "cli")
        app_mock.sm.transition_6_start_execution.assert_called_once_with("01TEST")

    @pytest.mark.asyncio
    async def test_cli_approve_from_review(self, app_mock):
        """Approval from IN_REVIEW state uses the review transition."""
        app_mock.db.fetch_one = AsyncMock(return_value={"state": "in_review"})
        result = await _cli_approve(app_mock, {"bundle_id": "01TEST"})
        assert result["approved"] is True
        app_mock.sm.transition_4_approve_from_review.assert_called_once_with("01TEST", "cli")
        app_mock.sm.transition_6_start_execution.assert_called_once_with("01TEST")

    @pytest.mark.asyncio
    async def test_cli_reject(self, app_mock):
        result = await _cli_reject(app_mock, {"bundle_id": "01TEST", "reason": "not needed"})
        assert result["rejected"] is True
        app_mock.sm.transition_1b_reject.assert_called_once_with("01TEST", "cli", "not needed")

    @pytest.mark.asyncio
    async def test_cli_list(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01T", "state": "in_progress", "created_at": 1699999900, "proposal_json": '{"bundle_input":{"idea":"Build"}}', "tier": "full_review", "repo": "control-plane"},
        ])
        result = await _cli_list(app_mock, {})
        assert len(result["bundles"]) == 1
        assert result["bundles"][0]["state"] == "in_progress"

    @pytest.mark.asyncio
    async def test_cli_list_with_state_filter(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[])
        result = await _cli_list(app_mock, {"state": "proposed"})
        assert result["bundles"] == []

    @pytest.mark.asyncio
    async def test_cli_show(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01TEST", "state": "in_progress", "proposal_json": '{"bundle_input":{"idea":"Test idea"}}', "tier": "full_review",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01TEST:task-1", "node_id": "task-1", "kind": "worker", "state": "completed"},
        ])
        result = await _cli_show(app_mock, {"bundle_id": "01TEST"})
        assert result["bundle"]["id"] == "01TEST"
        assert result["bundle"]["state"] == "in_progress"
        assert len(result["nodes"]) == 1

    @pytest.mark.asyncio
    async def test_cli_show_missing(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Bundle MISSING not found"):
            await _cli_show(app_mock, {"bundle_id": "MISSING"})

    @pytest.mark.asyncio
    async def test_cli_show_worker(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(side_effect=[
            {"id": "w1", "bundle_id": "b1", "node_id": "n1",
             "state": "running", "current_phase": "writing-code",
             "last_heartbeat": 1699999990, "created_at": 1699999990,
             "manifest_json": "{}"},
            {"spec_json": '{"objective":"Test task"}', "output_json": None},
        ])
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"result": "allowed"},
            {"result": "allowed"},
            {"result": "denied"},
        ])
        result = await _cli_show_worker(app_mock, {"worker_id": "w1"})
        assert result["worker"]["id"] == "w1"
        assert result["worker"]["current_phase"] == "writing-code"
        assert result["cap_checks"] == {"allowed": 2, "denied": 1}

    @pytest.mark.asyncio
    async def test_cli_kill(self, app_mock):
        """Phase 5: kill marks workers as failed via DB, no executor needed."""
        app_mock.db.fetch_all = AsyncMock(return_value=[{"id": "w1"}])
        result = await _cli_kill(app_mock, {"bundle_id": "01TEST"})
        assert result["workers_killed"] == 1
        app_mock.sm.transition_25_fail_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_cli_status(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(return_value={"cnt": 3})
        app_mock.db.fetch_all = AsyncMock(return_value=[])
        app_mock.ops = MagicMock()
        app_mock.ops._start_time = time.time() - 3600
        app_mock.settings.remote_workers = MagicMock()
        app_mock.settings.remote_workers.enabled = False
        app_mock._code_stale = False
        app_mock._bwrap_available = True
        result = await _cli_status(app_mock, {})
        assert result["worker_count"] == 3
        assert "uptime" in result

    @pytest.mark.asyncio
    async def test_cli_status_db_error(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(side_effect=Exception("DB error"))
        app_mock.ops = MagicMock()
        app_mock.ops._start_time = time.time()
        app_mock.settings.remote_workers = MagicMock()
        app_mock.settings.remote_workers.enabled = False
        app_mock._code_stale = False
        app_mock._bwrap_available = False
        result = await _cli_status(app_mock, {})
        assert "error" in result
        assert result["db_ok"] is False

    @pytest.mark.asyncio
    async def test_cli_version(self, app_mock):
        app_mock._startup_code_hash = "abc123"
        result = await _cli_version(app_mock, {})
        assert "installed_code_hash" in result
