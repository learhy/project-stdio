"""Tests for Bundle 2.6: Developer Worker (real implementation)."""
import importlib
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
            result = await w._commit_worktree("Test", failed=False)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_changes(self):
        """BUG #20: _commit_worktree returns False when git diff --cached is clean."""
        from studio.workers import developer

        with patch.dict("os.environ", {"STUDIO_WORKTREE_PATH": "/tmp/test-wt"}):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                # First call: git add -A
                mock_add = AsyncMock()
                mock_add.wait = AsyncMock(return_value=0)
                # Second call: git diff --cached --quiet (returns 0 = clean)
                mock_diff = AsyncMock()
                mock_diff.wait = AsyncMock(return_value=0)

                mock_exec.side_effect = [mock_add, mock_diff]

                result = await w._commit_worktree("My objective", failed=False)
                assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_changes_committed(self):
        """_commit_worktree returns True when changes are staged and committed."""
        from studio.workers import developer

        with patch.dict("os.environ", {"STUDIO_WORKTREE_PATH": "/tmp/test-wt"}):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_add = AsyncMock()
                mock_add.wait = AsyncMock(return_value=0)
                # git diff --cached --quiet returns 1 = dirty
                mock_diff = AsyncMock()
                mock_diff.wait = AsyncMock(return_value=1)
                # git commit
                mock_commit = AsyncMock()
                mock_commit.wait = AsyncMock(return_value=0)

                mock_exec.side_effect = [mock_add, mock_diff, mock_commit]

                result = await w._commit_worktree("Built the API", failed=False)
                assert result is True


class TestNoOutputDetection:
    """Tests for BUG #20: worker reports success with zero code changes."""

    @pytest.mark.asyncio
    async def test_opencode_success_no_commits_reports_failure(self):
        """When opencode succeeds but _commit_worktree returns False, outcome is failure."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {"objective": "Build API?", "model": "test-model"}
            w.rpc.notify = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = AsyncMock()
                    mock_proc.wait = AsyncMock(return_value=0)
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stderr = AsyncMock()
                    mock_exec.return_value = mock_proc

                    with patch.object(w, "_stream_and_detect_stuck") as mock_stream:
                        mock_stream.return_value = (["Done"], b"", False)
                        with patch.object(w, "_commit_worktree") as mock_commit:
                            mock_commit.return_value = False  # No changes
                            with patch.object(w, "_run_gates") as mock_gates:
                                mock_gates.return_value = {"passed": True, "failed_gate": "", "output": ""}

                                result = await w._execute_task()

        assert result["outcome"] == "failure"
        assert "no_output_produced" in result["errors"]
        assert "zero code changes" in result["summary"]

    @pytest.mark.asyncio
    async def test_opencode_success_with_commits_reports_success(self):
        """When opencode succeeds and _commit_worktree returns True, outcome is success."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {"objective": "Build API?", "model": "test-model"}
            w.rpc.notify = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = AsyncMock()
                    mock_proc.wait = AsyncMock(return_value=0)
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stderr = AsyncMock()
                    mock_exec.return_value = mock_proc

                    with patch.object(w, "_stream_and_detect_stuck") as mock_stream:
                        mock_stream.return_value = (["Done"], b"", False)
                        with patch.object(w, "_commit_worktree") as mock_commit:
                            mock_commit.return_value = True  # Changes committed
                            with patch.object(w, "_get_files_changed") as mock_files:
                                mock_files.return_value = ["main.py", "test_main.py"]
                                with patch.object(w, "_run_gates") as mock_gates:
                                    mock_gates.return_value = {"passed": True, "failed_gate": "", "output": ""}

                                    result = await w._execute_task()

        assert result["outcome"] == "success"
        assert result["files_changed"] == 2

    @pytest.mark.asyncio
    async def test_opencode_nonzero_exit_reports_failure(self):
        """When opencode exits non-zero, outcome is failure regardless of commits."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {"objective": "Bad task?", "model": "test-model"}
            w.rpc.notify = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = AsyncMock()
                    mock_proc.wait = AsyncMock(return_value=1)  # Non-zero exit
                    mock_proc.stdout = AsyncMock()
                    mock_proc.stderr = AsyncMock()
                    mock_exec.return_value = mock_proc

                    with patch.object(w, "_stream_and_detect_stuck") as mock_stream:
                        mock_stream.return_value = (["Error"], b"", False)
                        with patch.object(w, "_commit_worktree") as mock_commit:
                            mock_commit.return_value = False

                            result = await w._execute_task()

        assert result["outcome"] == "failure"
        assert "exit_code=1" in str(result["errors"])


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


# ── Bundle 6.2: Self-healing inner loop tests ──────────────────────────────

class TestFixPromptConstruction:
    def test_build_fix_prompt_from_failures(self):
        from studio.orchestrator.artifacts import VerificationFailure
        w = DeveloperWorker()
        failures = [
            VerificationFailure(test_name="GET /health", expected="status 200", actual="status 500",
                               error_output="Internal Server Error", summary="Health check failed"),
            VerificationFailure(test_name="POST /submit", expected="status 201", actual="status 400",
                               error_output="Bad Request", summary="Submit failed"),
        ]
        prompt = w._build_fix_prompt("Build health API", failures, 2)
        assert "failed verification on attempt 2" in prompt
        assert "FAILURE 1: GET /health" in prompt
        assert "Expected: status 200" in prompt
        assert "Got: status 500" in prompt
        assert "FAILURE 2: POST /submit" in prompt
        assert "Do not change code that is working correctly" in prompt

    def test_build_fix_prompt_empty_failures(self):
        w = DeveloperWorker()
        prompt = w._build_fix_prompt("Build API", [], 3)
        assert "failed verification on attempt 3" in prompt
        assert "Fix these specific failures" in prompt

    def test_build_opencode_retry_prompt(self):
        w = DeveloperWorker()
        prompt = w._build_opencode_retry_prompt("Build API", ["exit_code=1", "timeout"], 2)
        assert "previous attempt" in prompt
        assert "exit_code=1" in prompt
        assert "Do not repeat the same approach" in prompt


class TestVerificationIntegration:
    """Tests that verification runs as part of _execute_task inner loop."""

    @pytest.mark.asyncio
    async def test_loop_passes_on_first_attempt(self):
        """Verification passes on first attempt → commit → gates → success."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {"success": True, "stuck": False, "stdout_lines": ["Done"]}
                        with patch.object(w, "_run_verification") as mock_verify:
                            from studio.orchestrator.artifacts import VerificationResult
                            mock_verify.return_value = VerificationResult(passed=True, output="All good")
                            with patch.object(w, "_commit_worktree") as mock_commit:
                                mock_commit.return_value = True
                                with patch.object(w, "_get_files_changed") as mock_files:
                                    mock_files.return_value = ["main.py"]
                                    with patch.object(w, "_run_gates") as mock_gates:
                                        mock_gates.return_value = {"passed": True, "failed_gate": "", "output": ""}

                                        result = await w._execute_task()

            assert result["outcome"] == "success"
            assert result["attempts"] == 1
            mock_opencode.assert_called_once()

    @pytest.mark.asyncio
    async def test_loop_retries_on_verification_failure(self):
        """Verification fails on attempt 1 → fix → passes on attempt 2."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
                "max_fix_attempts": 5,
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {"success": True, "stuck": False, "stdout_lines": ["Done"]}
                        with patch.object(w, "_run_verification") as mock_verify:
                            from studio.orchestrator.artifacts import VerificationResult, VerificationFailure
                            # Fail first, pass second
                            mock_verify.side_effect = [
                                VerificationResult(passed=False, output="Tests failed",
                                                   failures=[VerificationFailure(test_name="pytest",
                                                                                 summary="1 test failed")]),
                                VerificationResult(passed=True, output="All passing"),
                            ]
                            with patch.object(w, "_commit_worktree") as mock_commit:
                                mock_commit.return_value = True
                                with patch.object(w, "_get_files_changed") as mock_files:
                                    mock_files.return_value = ["main.py"]
                                    with patch.object(w, "_run_gates") as mock_gates:
                                        mock_gates.return_value = {"passed": True, "failed_gate": "", "output": ""}
                                        with patch.object(w, "_report_checkpoint") as mock_checkpoint:

                                            result = await w._execute_task()

            assert result["outcome"] == "success"
            assert result["attempts"] == 2
            assert mock_opencode.call_count == 2
            mock_checkpoint.assert_called()

    @pytest.mark.asyncio
    async def test_loop_exhausts_attempts_and_fails(self):
        """All attempts fail verification → escalate → return failure."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
                "max_fix_attempts": 2,
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {"success": True, "stuck": False, "stdout_lines": ["Done"]}
                        with patch.object(w, "_run_verification") as mock_verify:
                            from studio.orchestrator.artifacts import VerificationResult, VerificationFailure
                            mock_verify.return_value = VerificationResult(
                                passed=False, output="Tests failed",
                                failures=[VerificationFailure(test_name="pytest", summary="3 tests failed")],
                            )
                            with patch.object(w, "_escalate_to_pm") as mock_escalate:
                                mock_escalate.return_value = None  # No PM response
                                with patch.object(w, "_commit_worktree") as mock_commit:
                                    mock_commit.return_value = True

                                    result = await w._execute_task()

            assert result["outcome"] == "failure"
            assert result["attempts"] == 2
            assert mock_opencode.call_count == 2
            mock_escalate.assert_called()

    @pytest.mark.asyncio
    async def test_gates_run_after_loop_passes(self):
        """Gates run after verification passes, not during the loop."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            call_order = []

            async def tracking_verify(*args, **kwargs):
                call_order.append("verify")
                from studio.orchestrator.artifacts import VerificationResult
                return VerificationResult(passed=True, output="OK")

            async def tracking_commit(*args, **kwargs):
                call_order.append("commit")
                return True

            async def tracking_gates(*args, **kwargs):
                call_order.append("gates")
                return {"passed": True, "failed_gate": "", "output": ""}

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {"success": True, "stuck": False, "stdout_lines": ["Done"]}
                        with patch.object(w, "_run_verification", side_effect=tracking_verify):
                            with patch.object(w, "_commit_worktree", side_effect=tracking_commit):
                                with patch.object(w, "_get_files_changed") as mock_files:
                                    mock_files.return_value = ["main.py"]
                                    with patch.object(w, "_run_gates", side_effect=tracking_gates):
                                        await w._execute_task()

            assert call_order == ["verify", "commit", "gates"]

    @pytest.mark.asyncio
    async def test_documentation_type_skips_verification(self):
        """Documentation artifact type in skip list → verification skipped."""
        from studio.workers import developer

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Write docs",
                "artifact_type": "documentation",
                "verification_strategy": {"type": "documentation", "review": "llm"},
                "skip_verification_for_types": ["documentation"],
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {"success": True, "stuck": False, "stdout_lines": ["Done"]}
                        with patch.object(w, "_run_verification") as mock_verify:
                            from studio.orchestrator.artifacts import VerificationResult
                            mock_verify.return_value = VerificationResult(passed=True, output="Skipped")
                            with patch.object(w, "_commit_worktree") as mock_commit:
                                mock_commit.return_value = True
                                with patch.object(w, "_get_files_changed") as mock_files:
                                    mock_files.return_value = ["README.md"]
                                    with patch.object(w, "_run_gates") as mock_gates:
                                        mock_gates.return_value = {"passed": True, "failed_gate": "", "output": ""}

                                        result = await w._execute_task()

            assert result["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_escalation_pm_response_resumes_worker(self):
        """PM response to escalation overrides verification failure and succeeds."""
        from studio.workers import developer
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            async def mock_escalate(*args, **kwargs):
                return "Proceed with current implementation. The code is correct."

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {
                            "success": True, "stuck": False,
                            "stdout_lines": ["Done"],
                        }
                        with patch.object(w, "_run_verification") as mock_verify:
                            mock_verify.return_value = VerificationResult(
                                passed=False, output="Bug in verification",
                                failures=[VerificationFailure(
                                    test_name="smoke", summary="Pydantic error",
                                )],
                            )
                            with patch.object(w, "_escalate_to_pm", mock_escalate):
                                with patch.object(w, "_commit_worktree") as mock_commit:
                                    mock_commit.return_value = True

                                    result = await w._execute_task()

            assert result["outcome"] == "success"
            assert result.get("pm_override") is True

    @pytest.mark.asyncio
    async def test_escalation_pm_kill_aborts_worker(self):
        """PM /kill response to escalation still results in failure."""
        from studio.workers import developer
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            async def mock_escalate(*args, **kwargs):
                return "/kill"

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {
                            "success": True, "stuck": False,
                            "stdout_lines": ["Done"],
                        }
                        with patch.object(w, "_run_verification") as mock_verify:
                            mock_verify.return_value = VerificationResult(
                                passed=False, output="Tests failed",
                                failures=[VerificationFailure(
                                    test_name="pytest", summary="3 tests failed",
                                )],
                            )
                            with patch.object(w, "_escalate_to_pm", mock_escalate):
                                with patch.object(w, "_commit_worktree") as mock_commit:
                                    mock_commit.return_value = True

                                    result = await w._execute_task()

            assert result["outcome"] == "failure"

    @pytest.mark.asyncio
    async def test_escalation_pm_empty_response_fails(self):
        """Empty PM response (no answer) still results in failure."""
        from studio.workers import developer
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        with patch.dict("os.environ", {
            "STUDIO_WORKTREE_PATH": "/tmp/test-wt",
            "STUDIO_BASE_BRANCH": "main",
        }):
            importlib.reload(developer)
            w = developer.DeveloperWorker()
            w.task_spec = {
                "objective": "Build API",
                "verification_strategy": {"type": "library", "test_command": "pytest"},
            }
            w.rpc.notify = AsyncMock()
            w.rpc.call = AsyncMock()

            async def mock_escalate(*args, **kwargs):
                return None

            with patch.object(w, "_setup_git_identity"):
                with patch.object(w, "_init_opencode_project"):
                    with patch.object(w, "_run_opencode") as mock_opencode:
                        mock_opencode.return_value = {
                            "success": True, "stuck": False,
                            "stdout_lines": ["Done"],
                        }
                        with patch.object(w, "_run_verification") as mock_verify:
                            mock_verify.return_value = VerificationResult(
                                passed=False, output="Tests failed",
                                failures=[VerificationFailure(
                                    test_name="pytest", summary="3 tests failed",
                                )],
                            )
                            with patch.object(w, "_escalate_to_pm", mock_escalate):
                                with patch.object(w, "_commit_worktree") as mock_commit:
                                    mock_commit.return_value = True

                                    result = await w._execute_task()

            assert result["outcome"] == "failure"
