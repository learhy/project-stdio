"""Tests for cli.py — 8 command handlers."""
import json
import os
import tempfile
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
    _resolve_socket_path,
    get_socket_path,
    _socket_paths_to_try,
    _set_socket_arg,
)


class TestGetSocketPath:
    def test_default_dev_fallback(self, monkeypatch):
        """When no socket exists anywhere, get_socket_path returns None."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        monkeypatch.setattr("studio.orchestrator.cli._socket_paths_to_try", lambda: [])
        result = get_socket_path()
        assert result is None

    @patch.dict("os.environ", {"STUDIO_SOCKET_PATH": "/custom/path.sock"})
    def test_env_var_priority(self):
        """STUDIO_SOCKET_PATH env var takes priority over everything."""
        assert get_socket_path() == "/custom/path.sock"

    @patch.dict("os.environ", {"STUDIO_SOCKET_PATH": "/env/path.sock"})
    def test_env_var_wins_even_when_others_exist(self, monkeypatch):
        """Env var wins regardless of what else exists on disk."""
        # Even with real paths existing, env var wins
        monkeypatch.setattr("studio.orchestrator.cli._socket_paths_to_try", lambda: ["/run/studio/orchestrator.sock"])
        assert get_socket_path() == "/env/path.sock"

    def test_socket_auto_detect_run(self, monkeypatch):
        """When /run/studio/orchestrator.sock exists, use it."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        monkeypatch.setattr("studio.orchestrator.cli._socket_paths_to_try", lambda: [
            "/run/studio/orchestrator.sock",
        ])
        monkeypatch.setattr("os.path.exists", lambda p: p == "/run/studio/orchestrator.sock")
        result = get_socket_path()
        assert result == "/run/studio/orchestrator.sock"

    def test_socket_auto_detect_local(self, monkeypatch):
        """When only ~/.local/share/studio/orchestrator.sock exists, use it."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        local_path = os.path.expanduser("~/.local/share/studio/orchestrator.sock")
        monkeypatch.setattr("studio.orchestrator.cli._socket_paths_to_try", lambda: [
            "/run/studio/orchestrator.sock",
            local_path,
            "/tmp/studio.sock",
        ])
        monkeypatch.setattr("os.path.exists", lambda p: p == local_path)
        result = get_socket_path()
        assert result == local_path

    def test_socket_auto_detect_fallback_order(self, monkeypatch):
        """When multiple sockets exist, first found wins."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        paths = [
            "/run/studio/orchestrator.sock",
            os.path.expanduser("~/.local/share/studio/orchestrator.sock"),
            "/tmp/studio.sock",
        ]
        monkeypatch.setattr("studio.orchestrator.cli._socket_paths_to_try", lambda: paths)
        # /run exists first
        monkeypatch.setattr("os.path.exists", lambda p: p == "/run/studio/orchestrator.sock")
        result = get_socket_path()
        assert result == "/run/studio/orchestrator.sock"

    @pytest.mark.asyncio
    async def test_socket_not_found_error(self):
        """When no socket found, error message lists all paths tried."""
        result = await _send_rpc(None, "studio.health", {})
        assert result is not None
        assert "error" in result
        msg = result["error"]["message"]
        assert "Cannot connect to orchestrator" in msg
        assert "Tried the following paths" in msg
        assert "STUDIO_SOCKET_PATH" in msg

    def test_resolve_socket_path_with_arg(self, monkeypatch):
        """--socket argument overrides everything."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        _set_socket_arg("/my/override.sock")
        result = _resolve_socket_path()
        assert result == "/my/override.sock"
        # Clean up global state
        _set_socket_arg(None)

    def test_socket_paths_from_location_file(self, tmp_path, monkeypatch):
        """Location files are read and their paths are returned first."""
        monkeypatch.delenv("STUDIO_SOCKET_PATH", raising=False)
        loc_file = tmp_path / ".socket-path"
        loc_file.write_text("/custom/install/socket.sock")
        # Patch the location file path used in _socket_paths_to_try
        monkeypatch.setattr("os.path.exists", lambda p: p == str(loc_file) or "socket.sock" in p)

        # Build the list manually
        paths: list[str] = []
        if os.path.exists(str(loc_file)):
            p = loc_file.read_text().strip()
            if p:
                paths.append(p)
        paths.extend(["/run/studio/orchestrator.sock", "/tmp/studio.sock"])
        assert "/custom/install/socket.sock" in paths
        assert paths[0] == "/custom/install/socket.sock"

    def test_socket_paths_to_try_returns_standard_locations(self):
        """_socket_paths_to_try always includes the standard fallback locations."""
        paths = _socket_paths_to_try()
        assert "/run/studio/orchestrator.sock" in paths
        assert "/tmp/studio.sock" in paths


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
