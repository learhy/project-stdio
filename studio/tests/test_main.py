"""Tests for main.py — orchestrator lifecycle and CLI dispatch."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-start.db"
        app.settings.orchestrator.socket_path = "/tmp/test-start.sock"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner") as mock_runner_cls, \
             patch("studio.orchestrator.main.DagExecutor") as mock_exec_cls, \
             patch("studio.orchestrator.main.Scheduler") as mock_sched_cls, \
             patch("studio.orchestrator.main.Reconciler") as mock_recon_cls, \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.close = AsyncMock()
            mock_create_db.return_value = mock_db

            mock_dispatcher = MagicMock()
            mock_handlers = MagicMock()
            mock_conn_mgr = MagicMock()
            mock_create_rpc.return_value = (mock_dispatcher, mock_handlers, mock_conn_mgr)

            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            mock_exec = MagicMock()
            mock_exec._running_workers = {}
            mock_exec_cls.return_value = mock_exec

            mock_sched = MagicMock()
            mock_sched.start = AsyncMock()
            mock_sched.stop = AsyncMock()
            mock_sched_cls.return_value = mock_sched

            mock_recon = MagicMock()
            mock_recon.reconcile = AsyncMock(return_value={"workers_killed": 0, "nodes_failed": 0, "bundles_recovered": 0})
            mock_recon_cls.return_value = mock_recon

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
            assert app.executor is not None
            assert app.scheduler is not None
            assert app.reconciler is not None
            mock_sched.start.assert_called_once()
            mock_recon.reconcile.assert_called_once()
            mock_start_server.assert_called_once()

            await app.stop()
            mock_sched.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_runs_on_startup(self):
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-recon.db"
        app.settings.orchestrator.socket_path = "/tmp/test-recon.sock"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner"), \
             patch("studio.orchestrator.main.DagExecutor"), \
             patch("studio.orchestrator.main.Scheduler") as mock_sched_cls, \
             patch("studio.orchestrator.main.Reconciler") as mock_recon_cls, \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.close = AsyncMock()
            mock_create_db.return_value = mock_db

            mock_create_rpc.return_value = (MagicMock(), MagicMock(), MagicMock())
            mock_sched_cls.return_value = MagicMock(start=AsyncMock(), stop=AsyncMock())

            mock_recon = MagicMock()
            mock_recon.reconcile = AsyncMock(return_value={
                "workers_killed": 2, "nodes_failed": 2, "bundles_recovered": 1
            })
            mock_recon_cls.return_value = mock_recon

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            await app.start()
            mock_recon.reconcile.assert_called_once()
            await app.stop()

    @pytest.mark.asyncio
    async def test_stop_closes_all_components(self):
        app = Orchestrator()
        app.settings.orchestrator.db_path = "/tmp/test-stop.db"
        app.settings.orchestrator.socket_path = "/tmp/test-stop.sock"

        with patch("studio.orchestrator.main.create_database") as mock_create_db, \
             patch("studio.orchestrator.main.create_rpc_system") as mock_create_rpc, \
             patch("studio.orchestrator.main.LocalBwrapWorkerRunner"), \
             patch("studio.orchestrator.main.DagExecutor"), \
             patch("studio.orchestrator.main.Scheduler") as mock_sched_cls, \
             patch("studio.orchestrator.main.Reconciler") as mock_recon_cls, \
             patch("studio.orchestrator.main.asyncio.start_unix_server") as mock_start_server, \
             patch("studio.orchestrator.main.os.chmod"), \
             patch("studio.orchestrator.main.os.path.exists", return_value=False), \
             patch("studio.orchestrator.main.os.unlink"):
            mock_db = MagicMock()
            mock_db.fetch_all = AsyncMock()
            mock_db.fetch_one = AsyncMock()
            mock_db.close = AsyncMock()
            mock_create_db.return_value = mock_db

            mock_conn_mgr = MagicMock()
            mock_conn_mgr._by_worker_id = {}
            mock_create_rpc.return_value = (MagicMock(), MagicMock(), mock_conn_mgr)

            mock_sched = MagicMock()
            mock_sched.start = AsyncMock()
            mock_sched.stop = AsyncMock()
            mock_sched_cls.return_value = mock_sched

            mock_recon = MagicMock()
            mock_recon.reconcile = AsyncMock(return_value={"workers_killed": 0, "nodes_failed": 0, "bundles_recovered": 0})
            mock_recon_cls.return_value = mock_recon

            mock_server = MagicMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_start_server.return_value = mock_server

            await app.start()
            await app.stop()

            mock_sched.stop.assert_called_once()
            mock_db.close.assert_called_once()


class TestCliHandlers:
    @pytest.fixture
    def app_mock(self):
        app = MagicMock()
        app.sm = MagicMock()
        app.sm.transition_1_submit = AsyncMock()
        app.sm.transition_1a_approve = AsyncMock()
        app.sm.transition_1b_reject = AsyncMock()
        app.sm.transition_6_start_execution = AsyncMock()
        app.sm.transition_25_fail_execution = AsyncMock()
        app.sm.now = MagicMock(return_value=1700000000)
        app.executor = MagicMock()
        app.executor.start_bundle = AsyncMock()
        app.executor._running_workers = {}
        app.runner = MagicMock()
        app.runner.kill_worker = AsyncMock()
        app.db = MagicMock()
        app.db.fetch_all = AsyncMock()
        app.db.fetch_one = AsyncMock()
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
    async def test_cli_submit_default_repo(self, app_mock):
        submission = {"bundle_input": {}, "task_dag": {"nodes": [], "edges": []}}
        result = await _cli_submit(app_mock, {"submission": submission})
        assert "bundle_id" in result
        # Verify default repo used
        call_args = app_mock.sm.transition_1_submit.call_args[0]
        assert call_args[1] == "control-plane"

    @pytest.mark.asyncio
    async def test_cli_approve_starts_execution(self, app_mock):
        result = await _cli_approve(app_mock, {"bundle_id": "01TEST"})
        assert result["approved"] is True
        app_mock.sm.transition_1a_approve.assert_called_once_with("01TEST", "cli")
        app_mock.sm.transition_6_start_execution.assert_called_once_with("01TEST")
        app_mock.executor.start_bundle.assert_called_once_with("01TEST")

    @pytest.mark.asyncio
    async def test_cli_reject(self, app_mock):
        result = await _cli_reject(app_mock, {"bundle_id": "01TEST", "reason": "not needed"})
        assert result["rejected"] is True
        app_mock.sm.transition_1b_reject.assert_called_once_with("01TEST", "cli", "not needed")

    @pytest.mark.asyncio
    async def test_cli_list(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01T", "state": "in_progress", "created_at": 1699999900, "proposal_json": '{"bundle_input":{"idea":"Build"}}'},
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
            "id": "01TEST", "state": "in_progress", "proposal_json": '{"bundle_input":{"idea":"Test idea"}}',
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01TEST:task-1", "node_id": "task-1", "kind": "worker", "state": "completed"},
        ])
        result = await _cli_show(app_mock, {"bundle_id": "01TEST"})
        assert result["bundle_id"] == "01TEST"
        assert result["state"] == "in_progress"
        assert len(result["nodes"]) == 1

    @pytest.mark.asyncio
    async def test_cli_show_missing(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Bundle MISSING not found"):
            await _cli_show(app_mock, {"bundle_id": "MISSING"})

    @pytest.mark.asyncio
    async def test_cli_show_worker(self, app_mock):
        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "w1", "bundle_id": "b1", "node_id": "n1",
            "state": "running", "current_phase": "writing-code",
            "last_heartbeat": 1699999990,
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"payload_json": '{"message":"Wrote main.py"}'},
        ])
        result = await _cli_show_worker(app_mock, {"worker_id": "w1"})
        assert result["worker_id"] == "w1"
        assert result["phase"] == "writing-code"

    @pytest.mark.asyncio
    async def test_cli_kill(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[{"id": "w1"}])
        result = await _cli_kill(app_mock, {"bundle_id": "01TEST"})
        assert result["workers_killed"] == 1
        app_mock.sm.transition_25_fail_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_cli_status(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01T", "state": "in_progress", "proposal_json": '{"bundle_input":{"idea":"Build"}}'},
        ])
        result = await _cli_status(app_mock, {})
        assert result["uptime"] == 0
        assert len(result["bundles"]) == 1

    @pytest.mark.asyncio
    async def test_cli_kill_no_workers(self, app_mock):
        app_mock.db.fetch_all = AsyncMock(return_value=[])
        result = await _cli_kill(app_mock, {"bundle_id": "01TEST"})
        assert result["workers_killed"] == 0


class TestConnectionRouting:
    @pytest.mark.asyncio
    async def test_handle_connection_routes_auth_to_worker(self):
        app = Orchestrator()
        app._serve_worker = AsyncMock()
        app._serve_cli = AsyncMock()
        app.db = MagicMock()
        app.dispatcher = MagicMock()

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "method": "auth", "params": {}, "id": 1,
            "token": "test-token",
        }).encode())

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        await app._handle_connection(reader, writer)
        app._serve_worker.assert_called_once()
        app._serve_cli.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_connection_routes_studio_to_cli(self):
        app = Orchestrator()
        app._serve_worker = AsyncMock()
        app._serve_cli = AsyncMock()
        app.db = MagicMock()
        app.dispatcher = MagicMock()

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "method": "studio.status", "params": {}, "id": 1,
        }).encode())

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        await app._handle_connection(reader, writer)
        app._serve_cli.assert_called_once()
        app._serve_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_connection_rejects_unknown_first_message(self):
        app = Orchestrator()
        app._serve_worker = AsyncMock()
        app._serve_cli = AsyncMock()

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "method": "some.thing", "params": {}, "id": 1,
        }).encode())

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        await app._handle_connection(reader, writer)
        app._serve_worker.assert_not_called()
        app._serve_cli.assert_not_called()
        # Should write an error response
        write_calls = [c for c in writer.write.call_args_list
                       if b"error" in c[0][0]]
        assert len(write_calls) >= 1

    @pytest.mark.asyncio
    async def test_handle_connection_parse_error(self):
        app = Orchestrator()

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=b"not json")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        await app._handle_connection(reader, writer)
        write_calls = [c for c in writer.write.call_args_list
                       if b"Parse error" in c[0][0]]
        assert len(write_calls) >= 1
