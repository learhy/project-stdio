"""Tests for worker.py — Phase 1 developer worker."""
import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.workers.worker import Worker
from studio.workers.client import RpcClient

TEST_SOCKET = "/tmp/test.sock"


class TestRpcClient:
    @pytest.fixture(autouse=True)
    def _set_socket_path(self, monkeypatch):
        monkeypatch.setenv("STUDIO_SOCKET_PATH", TEST_SOCKET)
        monkeypatch.delenv("STUDIO_ORCHESTRATOR_ADDR", raising=False)

    @pytest.mark.asyncio
    async def test_call_sends_jsonrpc_message(self):
        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "result": {"bound": True}, "id": 1,
        }).encode())

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        client = RpcClient()
        client.reader = reader
        client.writer = writer

        resp = await client.call("auth", {"token": "test"})
        assert resp["result"]["bound"] is True
        writer.write.assert_called_once()
        sent = writer.write.call_args[0][0]
        assert b"auth" in sent
        assert b"test" in sent

    @pytest.mark.asyncio
    async def test_notify_sends_no_id(self):
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        client = RpcClient()
        client.reader = AsyncMock()
        client.writer = writer

        await client.notify("worker.log", {"level": "info", "message": "hello"})
        sent = json.loads(writer.write.call_args[0][0].decode())
        assert "id" not in sent
        assert sent["method"] == "worker.log"

    @pytest.mark.asyncio
    async def test_connect_opens_unix_socket(self):
        with patch("studio.workers.client.asyncio.open_unix_connection") as mock_conn:
            mock_conn.return_value = (AsyncMock(), MagicMock())
            client = RpcClient()
            await client.connect()
            mock_conn.assert_called_once_with(TEST_SOCKET)

    @pytest.mark.asyncio
    async def test_connect_tcp(self, monkeypatch):
        monkeypatch.setenv("STUDIO_ORCHESTRATOR_ADDR", "tcp://127.0.0.1:7811")
        with patch("studio.workers.client.asyncio.open_connection") as mock_conn:
            mock_conn.return_value = (AsyncMock(), MagicMock())
            client = RpcClient()
            await client.connect()
            mock_conn.assert_called_once()
            args = mock_conn.call_args
            assert args[0][0] == "127.0.0.1"
            assert args[0][1] == 7811

    @pytest.mark.asyncio
    async def test_connect_tcp_default_port(self, monkeypatch):
        monkeypatch.setenv("STUDIO_ORCHESTRATOR_ADDR", "tcp://10.0.0.1")
        with patch("studio.workers.client.asyncio.open_connection") as mock_conn:
            mock_conn.return_value = (AsyncMock(), MagicMock())
            client = RpcClient()
            await client.connect()
            args = mock_conn.call_args
            assert args[0][1] == 7811

    @pytest.mark.asyncio
    async def test_close(self):
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        client = RpcClient()
        client.writer = writer
        await client.close()
        writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_empty_response(self):
        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=b"")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        client = RpcClient()
        client.reader = reader
        client.writer = writer

        resp = await client.call("test.method", {})
        assert "error" in resp
        assert resp["error"]["code"] == -1


class TestWorker:
    @pytest.fixture
    def mock_rpc(self):
        rpc = MagicMock()
        rpc.connect = AsyncMock()
        rpc.close = AsyncMock()
        rpc.call = AsyncMock()
        rpc.notify = AsyncMock()
        return rpc

    @staticmethod
    def _env(task_spec=None, token="test-token"):
        task_json = json.dumps(task_spec or {})
        return {
            "STUDIO_WORKER_TOKEN": token,
            "STUDIO_SOCKET_PATH": "/tmp/test.sock",
            "STUDIO_WORKER_ID": "w1",
            "STUDIO_BUNDLE_ID": "b1",
            "STUDIO_NODE_ID": "n1",
            "STUDIO_TASK_SPEC": task_json,
        }

    @pytest.mark.asyncio
    async def test_run_auth_flow_success(self, mock_rpc):
        mock_rpc.call.side_effect = [
            {"result": {"bound": True, "worker_id": "w1"}},      # auth
            {"result": {"accepted": True, "node_state": "completed"}},  # final_report
        ]
        mock_rpc.notify = AsyncMock()

        with patch.dict("studio.workers.worker.os.environ", self._env()), \
             patch("studio.workers.worker._TOKEN", "test-token"):
            w = Worker()
            w.rpc = mock_rpc

            with patch.object(w, "_heartbeat_loop", AsyncMock()), \
                 patch.object(w, "_execute_task", AsyncMock(return_value={
                     "outcome": "success", "summary": "done", "files_changed": [],
                     "errors": [], "tests_run": 0, "tests_passed": 0, "tests_failed": 0,
                 })):
                exit_code = await w.run()
                assert exit_code == 0
                # Auth call
                assert mock_rpc.call.call_args_list[0][0] == ("auth", {"token": "test-token"})
                # Final report call (after task)
                final_call = mock_rpc.call.call_args_list[-1]
                assert final_call[0][0] == "worker.final_report"

    @pytest.mark.asyncio
    async def test_run_auth_rejected(self, mock_rpc):
        mock_rpc.call.return_value = {
            "error": {"code": -32001, "message": "Invalid token"}
        }

        with patch.dict("studio.workers.worker.os.environ", self._env()), \
             patch("studio.workers.worker._TOKEN", "test-token"):
            w = Worker()
            w.rpc = mock_rpc
            exit_code = await w.run()
            assert exit_code == 1

    @pytest.mark.asyncio
    async def test_run_connect_failure(self, mock_rpc):
        mock_rpc.connect.side_effect = ConnectionRefusedError("no socket")

        with patch.dict("studio.workers.worker.os.environ", self._env()), \
             patch("studio.workers.worker._TOKEN", "test-token"):
            w = Worker()
            w.rpc = mock_rpc
            exit_code = await w.run()
            assert exit_code == 1

    @pytest.mark.asyncio
    async def test_run_no_token(self):
        with patch.dict("studio.workers.worker.os.environ", self._env(token="")), \
             patch("studio.workers.worker._TOKEN", ""):
            w = Worker()
            exit_code = await w.run()
            assert exit_code == 1

    @pytest.mark.asyncio
    async def test_execute_task_calls_agent(self, mock_rpc):
        with patch.dict("studio.workers.worker.os.environ", self._env()):
            w = Worker()
            w.rpc = mock_rpc

            with patch("studio.workers.worker.asyncio.create_subprocess_exec") as mock_exec:
                proc = MagicMock()
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"output line", b""))
                mock_exec.return_value = proc

                outcome = await w._execute_task("test objective")
                assert outcome["outcome"] == "success"
                mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_task_agent_failure(self, mock_rpc):
        with patch.dict("studio.workers.worker.os.environ", self._env()):
            w = Worker()
            w.rpc = mock_rpc

            with patch("studio.workers.worker.asyncio.create_subprocess_exec") as mock_exec:
                proc = MagicMock()
                proc.returncode = 1
                proc.communicate = AsyncMock(return_value=(b"", b"error output"))
                mock_exec.return_value = proc

                outcome = await w._execute_task("test objective")
                assert outcome["outcome"] == "failure"

    @pytest.mark.asyncio
    async def test_execute_task_timeout(self, mock_rpc):
        with patch.dict("studio.workers.worker.os.environ", self._env()):
            w = Worker()
            w.rpc = mock_rpc

            with patch("studio.workers.worker.asyncio.create_subprocess_exec") as mock_exec:
                proc = MagicMock()
                proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
                proc.kill = MagicMock()
                proc.wait = AsyncMock()
                mock_exec.return_value = proc

                outcome = await w._execute_task("test objective")
                assert outcome["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_heartbeat_loop_sends_heartbeats(self, mock_rpc):
        with patch.dict("studio.workers.worker.os.environ", self._env()):
            w = Worker()
            w.rpc = mock_rpc
            w._running = True

            mock_rpc.call.return_value = {"result": {"accepted": True, "phase": "writing-code"}}

            # Run for a couple of beats then stop
            async def stop_after_delay():
                await asyncio.sleep(0.05)
                w._running = False

            with patch("studio.workers.worker._HEARTBEAT_INTERVAL", 0.01):
                task = asyncio.create_task(w._heartbeat_loop())
                stopper = asyncio.create_task(stop_after_delay())
                await asyncio.gather(task, stopper)

            assert mock_rpc.call.call_count >= 1
            heartbeat_calls = [c for c in mock_rpc.call.call_args_list
                              if c[0][0] == "worker.heartbeat"]
            assert len(heartbeat_calls) >= 1

    @pytest.mark.asyncio
    async def test_execute_task_sends_progress_reports(self, mock_rpc):
        with patch.dict("studio.workers.worker.os.environ", self._env()):
            w = Worker()
            w.rpc = mock_rpc

            with patch("studio.workers.worker.asyncio.create_subprocess_exec") as mock_exec:
                proc = MagicMock()
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"output", b""))
                mock_exec.return_value = proc

                await w._execute_task("test objective")
                progress_calls = [c for c in mock_rpc.notify.call_args_list
                                if c[0][0] == "worker.progress_report"]
                assert len(progress_calls) >= 2  # starting + running

    @pytest.mark.asyncio
    async def test_run_failure_reports_failed(self, mock_rpc):
        mock_rpc.call.side_effect = [
            {"result": {"bound": True}},                              # auth
            {"result": {"accepted": True, "node_state": "failed"}},  # final_report
        ]

        with patch.dict("studio.workers.worker.os.environ", self._env()), \
             patch("studio.workers.worker._TOKEN", "test-token"):
            w = Worker()
            w.rpc = mock_rpc

            with patch.object(w, "_heartbeat_loop", AsyncMock()), \
                 patch.object(w, "_execute_task", AsyncMock(return_value={
                     "outcome": "failure", "summary": "error", "files_changed": [],
                     "errors": ["something broke"], "tests_run": 0, "tests_passed": 0,
                     "tests_failed": 0,
                 })):
                exit_code = await w.run()
                assert exit_code == 1
