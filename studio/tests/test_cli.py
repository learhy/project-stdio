"""Tests for cli.py — 8 command handlers."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from studio.orchestrator.cli import (
    cmd_submit,
    cmd_approve,
    cmd_reject,
    cmd_list,
    cmd_show,
    cmd_show_worker,
    cmd_kill,
    cmd_status,
    _send_rpc,
    _get_socket_path,
)


class TestGetSocketPath:
    def test_default(self):
        assert _get_socket_path() == "/tmp/studio.sock"

    @patch.dict("os.environ", {"STUDIO_SOCKET_PATH": "/custom/path.sock"})
    def test_from_env(self):
        assert _get_socket_path() == "/custom/path.sock"


class TestCmdSubmit:
    @pytest.mark.asyncio
    async def test_success(self):
        submission = {"bundle_input": {"idea": "test"}}
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"bundle_id": "01TEST"}}
            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.read.return_value = json.dumps(submission)
                exit_code = await cmd_submit("test.json")
                assert exit_code == 0
                mock_rpc.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        exit_code = await cmd_submit("nonexistent.json")
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_rpc_error(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"error": {"code": -32001, "message": "invalid"}}
            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.read.return_value = json.dumps({"test": True})
                exit_code = await cmd_submit("test.json")
                assert exit_code == 1


class TestCmdApprove:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"approved": True}}
            exit_code = await cmd_approve("01TEST")
            assert exit_code == 0

    @pytest.mark.asyncio
    async def test_error(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"error": {"code": -32001, "message": "illegal"}}
            exit_code = await cmd_approve("01TEST")
            assert exit_code == 1


class TestCmdReject:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"rejected": True}}
            exit_code = await cmd_reject("01TEST", "not needed")
            assert exit_code == 0
            # _send_rpc(socket_path, method, params)
            # call_args[0] = positional args tuple
            params = mock_rpc.call_args[0][2]  # third positional arg
            assert params.get("reason") == "not needed"

    @pytest.mark.asyncio
    async def test_default_reason(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"rejected": True}}
            exit_code = await cmd_reject("01TEST")
            assert exit_code == 0
            params = mock_rpc.call_args[0][2]
            assert "rejected via CLI" in params.get("reason", "")


class TestCmdList:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"bundles": [
                {"id": "01T", "state": "in_progress", "age": "4m", "idea": "Test idea"}
            ]}}
            exit_code = await cmd_list()
            assert exit_code == 0

    @pytest.mark.asyncio
    async def test_json_output(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"bundles": []}}
            exit_code = await cmd_list(json_output=True)
            assert exit_code == 0

    @pytest.mark.asyncio
    async def test_with_state_filter(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"bundles": []}}
            exit_code = await cmd_list(state="proposed")
            assert exit_code == 0
            params = mock_rpc.call_args[0][2]
            assert params.get("state") == "proposed"


class TestCmdShow:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {
                "bundle_id": "01TEST", "state": "in_progress",
                "idea": "Build server", "nodes": [
                    {"state": "completed"}, {"state": "running"}, {"state": "pending"}
                ]
            }}
            exit_code = await cmd_show("01TEST")
            assert exit_code == 0


class TestCmdShowWorker:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {
                "worker_id": "w1", "bundle_id": "b1", "state": "running",
                "phase": "writing-code", "last_heartbeat_ago": "23s",
                "recent_logs": [{"level": "info", "message": "Wrote src/main.py"}]
            }}
            exit_code = await cmd_show_worker("w1")
            assert exit_code == 0


class TestCmdKill:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"workers_killed": 1}}
            exit_code = await cmd_kill("01TEST")
            assert exit_code == 0


class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("studio.orchestrator.cli._send_rpc") as mock_rpc:
            mock_rpc.return_value = {"result": {"uptime": 3600, "bundles": []}}
            exit_code = await cmd_status()
            assert exit_code == 0
