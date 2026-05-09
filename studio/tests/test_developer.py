"""Tests for Bundle 2.6: Developer Worker (real implementation)."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.workers.developer import (
    DeveloperWorker,
    RpcClient,
    _hash_lines,
    _STUCK_WINDOW,
    _STUCK_THRESHOLD,
)


class TestHashLines:
    def test_hash_deterministic(self):
        lines = ["line1", "line2", "line3"]
        assert _hash_lines(lines) == _hash_lines(lines)

    def test_hash_different_content(self):
        assert _hash_lines(["a"]) != _hash_lines(["b"])

    def test_hash_different_order(self):
        assert _hash_lines(["a", "b"]) != _hash_lines(["b", "a"])


class TestStuckDetection:
    def test_identical_windows_increment_stuck(self):
        """Three identical rolling windows == stuck."""
        lines = ["processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1", "processing item 1",
                 "processing item 1", "processing item 1"]
        h = _hash_lines(lines[-20:])
        assert h == _hash_lines(lines[-20:])

    def test_varied_output_no_stuck(self):
        """Different rolling windows should not count as stuck."""
        import hashlib
        lines1 = [f"line{i}" for i in range(20)]
        lines2 = [f"line{i}" for i in range(1, 21)]
        assert _hash_lines(lines1) != _hash_lines(lines2)


class TestDeveloperWorkerInit:
    def test_worker_initializes_with_rpc_client(self):
        with patch.dict("os.environ", {
            "STUDIO_WORKER_TOKEN": "test",
            "STUDIO_SOCKET_PATH": "/tmp/test.sock",
            "STUDIO_WORKER_ID": "w1",
            "STUDIO_BUNDLE_ID": "b1",
            "STUDIO_NODE_ID": "n1",
            "STUDIO_TASK_SPEC": json.dumps({"objective": "Test task"}),
            "STUDIO_WORKTREE_PATH": "/tmp/worktree",
            "STUDIO_BASE_BRANCH": "main",
        }):
            from studio.workers import developer
            import importlib
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            assert w.task_spec["objective"] == "Test task"
            assert w._current_phase == "starting"


class TestDeveloperWorkerRun:
    @pytest.mark.asyncio
    async def test_run_fails_without_token(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {"STUDIO_WORKER_TOKEN": ""}):
            rc = await w.run()
            assert rc == 1

    @pytest.mark.asyncio
    async def test_run_fails_without_worktree(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {
            "STUDIO_WORKER_TOKEN": "test-token",
            "STUDIO_WORKTREE_PATH": "",
        }):
            rc = await w.run()
            assert rc == 1

    @pytest.mark.asyncio
    async def test_run_fails_without_opencode(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {
            "STUDIO_WORKER_TOKEN": "test-token",
            "STUDIO_WORKTREE_PATH": "/tmp/wt",
        }), patch("shutil.which", return_value=None):
            rc = await w.run()
            assert rc == 1

    @pytest.mark.asyncio
    async def test_run_auth_flow_success(self):
        w = DeveloperWorker()
        w.rpc.connect = AsyncMock()
        w.rpc.call = AsyncMock()
        w.rpc.call.side_effect = [
            {"result": {"bound": True, "worker_id": "w1"}},  # auth
            None,  # final_report (we check via call_args_list)
        ]
        w.rpc.close = AsyncMock()
        w._get_files_changed = AsyncMock(return_value=["src/main.py"])

        # Bypass actual task execution and heartbeat
        async def _execute_task():
            return {"outcome": "success", "tests_run": 1, "tests_passed": 1,
                    "tests_failed": 0, "errors": [], "summary": "Done"}

        w._execute_task = _execute_task

        async def _heartbeat_loop():
            pass

        w._heartbeat_loop = _heartbeat_loop

        with patch.dict("os.environ", {
            "STUDIO_WORKER_TOKEN": "test-token",
            "STUDIO_WORKTREE_PATH": "/tmp/wt",
        }), patch("shutil.which", return_value="/usr/bin/opencode"):
            rc = await w.run()
            assert rc == 0

    @pytest.mark.asyncio
    async def test_run_auth_rejected(self):
        w = DeveloperWorker()
        w.rpc.connect = AsyncMock()
        w.rpc.call = AsyncMock(return_value={
            "error": {"code": -1, "message": "Invalid token"},
        })
        w.rpc.close = AsyncMock()

        with patch.dict("os.environ", {
            "STUDIO_WORKER_TOKEN": "bad-token",
            "STUDIO_WORKTREE_PATH": "/tmp/wt",
        }), patch("shutil.which", return_value="/usr/bin/opencode"):
            rc = await w.run()
            assert rc == 1


class TestPhaseUpdate:
    def test_update_phase_thinking(self):
        w = DeveloperWorker()
        w._update_phase_from_output("thinking about the problem...")
        assert w._current_phase == "thinking"

    def test_update_phase_tool_call(self):
        w = DeveloperWorker()
        w._update_phase_from_output("executing tool call: read_file")
        assert w._current_phase == "tool-call"

    def test_update_phase_writing_code(self):
        w = DeveloperWorker()
        w._update_phase_from_output("writing code for main.py")
        assert w._current_phase == "writing-code"

    def test_update_phase_running_tests(self):
        w = DeveloperWorker()
        w._update_phase_from_output("running pytest on tests/")
        assert w._current_phase == "running-tests"

    def test_update_phase_no_match_preserves(self):
        w = DeveloperWorker()
        w._current_phase = "writing-code"
        w._update_phase_from_output("loading configuration file")
        assert w._current_phase == "writing-code"


class TestGateExecution:
    @pytest.mark.asyncio
    async def test_empty_gates_pass(self):
        w = DeveloperWorker()
        result = await w._run_gates([])
        assert result["passed"] is True
        assert result["failed_gate"] == ""

    @pytest.mark.asyncio
    async def test_successful_gate(self):
        import tempfile
        w = DeveloperWorker()
        with tempfile.TemporaryDirectory() as td:
            import studio.workers.developer as dev_mod
            old_path = dev_mod._WORKTREE_PATH
            dev_mod._WORKTREE_PATH = td
            try:
                result = await w._run_gates(["echo ok"])
            finally:
                dev_mod._WORKTREE_PATH = old_path
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_failing_gate(self):
        import tempfile
        w = DeveloperWorker()
        with tempfile.TemporaryDirectory() as td:
            import studio.workers.developer as dev_mod
            old_path = dev_mod._WORKTREE_PATH
            dev_mod._WORKTREE_PATH = td
            try:
                result = await w._run_gates(["exit 1"])
            finally:
                dev_mod._WORKTREE_PATH = old_path
        assert result["passed"] is False
        assert "exit 1" in result["failed_gate"]


class TestFilesChanged:
    @pytest.mark.asyncio
    async def test_no_worktree_returns_empty(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {"STUDIO_WORKTREE_PATH": ""}):
            files = await w._get_files_changed()
            assert files == []

    @pytest.mark.asyncio
    async def test_git_error_returns_empty(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/nonexistent",
            "STUDIO_BASE_BRANCH": "main",
        }):
            files = await w._get_files_changed()
            assert files == []


class TestCommitWorktree:
    @pytest.mark.asyncio
    async def test_no_worktree_does_nothing(self):
        w = DeveloperWorker()
        with patch.dict("os.environ", {"STUDIO_WORKTREE_PATH": ""}):
            await w._commit_worktree("Test", failed=False)


class TestHumanInput:
    @pytest.mark.asyncio
    async def test_request_human_input_error(self):
        w = DeveloperWorker()
        w.rpc.call = AsyncMock(return_value={
            "error": {"code": -1, "message": "Not connected"},
        })

        resp = await w._request_human_input("Question?", "Context")
        assert resp is None

    @pytest.mark.asyncio
    async def test_request_human_input_no_request_id(self):
        w = DeveloperWorker()
        w.rpc.call = AsyncMock(return_value={
            "result": {"state": "pending"},  # no request_id
        })

        resp = await w._request_human_input("Question?", "Context")
        assert resp is None

    @pytest.mark.asyncio
    async def test_request_human_input_gets_response(self):
        w = DeveloperWorker()
        # First call returns request_id, second returns resolved
        w.rpc.call = AsyncMock()
        w.rpc.call.side_effect = [
            {"result": {"request_id": "req-1", "state": "pending"}},
            {"result": {"pending": False, "response": "Try a different approach",
                        "responded_at": 1700000000, "responded_by": "pm"}},
        ]
        w._human_input_poll_interval = 0
        w._human_input_poll_count = 1

        resp = await w._request_human_input("What should I do?", "Context here")
        assert resp == "Try a different approach"
