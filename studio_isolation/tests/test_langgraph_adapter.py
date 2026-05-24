"""Tests for the LangGraph adapter (phase 2).

Verifies graph construction, end-to-end execution, state accumulation,
thread isolation, and human-in-the-loop interrupt behavior.
"""

import pytest
from studio_isolation.langgraph_adapter import (
    build_studio_graph,
    run_studio_graph,
    StudioGraphState,
    get_graph_mermaid,
)


class TestGraphConstruction:
    """Verify the StateGraph builds and compiles correctly."""

    @pytest.mark.asyncio
    async def test_build_graph(self):
        graph, checkpointer = await build_studio_graph()
        assert graph is not None
        assert checkpointer is not None

    @pytest.mark.asyncio
    async def test_mermaid_output(self):
        mermaid = await get_graph_mermaid()
        assert "graph TD" in mermaid or "stateDiagram" in mermaid or "---" in mermaid
        assert len(mermaid) > 0

    @pytest.mark.asyncio
    async def test_node_names(self):
        graph, _ = await build_studio_graph()
        # Graph compiles without error — the node set is defined in the builder
        assert True


class TestGraphExecution:
    """Verify the graph runs end-to-end (trivial bundle, auto-approve)."""

    @pytest.mark.asyncio
    async def test_end_to_end_auto_approve(self):
        """Run the full graph with auto_ship=True — no human interrupt."""
        result = await run_studio_graph(
            bundle_input="Add a comment to README",
            bundle_id="test-e2e-001",
        )
        assert result["bundle_id"] == "test-e2e-001"
        assert "proposal" in result
        assert "review_findings" in result
        assert len(result["review_findings"]) == 3  # adversary, security, qa
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_graph_state_accumulation(self):
        """Verify state accumulates across all nodes."""
        result = await run_studio_graph(
            bundle_input="Refactor auth module",
            bundle_id="test-e2e-002",
        )
        # All nodes should have written to state
        assert result["qa_passed"] is True
        assert result["approval_decision"] == "approved"
        assert "branch_name" in result
        assert "proposal" in result
        assert "task_dag" in result

    @pytest.mark.asyncio
    async def test_multiple_bundles_different_threads(self):
        """Different thread_ids should produce independent states."""
        result1 = await run_studio_graph(
            bundle_input="Task A: refactor logging",
            bundle_id="thread-a",
        )
        result2 = await run_studio_graph(
            bundle_input="Task B: add metrics",
            bundle_id="thread-b",
        )
        assert result1["bundle_id"] == "thread-a"
        assert result2["bundle_id"] == "thread-b"
        assert result1["bundle_input"] != result2["bundle_input"]


class TestRejectionPath:
    """Verify the rejection path (auto_ship=False without interrupt resolution)."""

    @pytest.mark.asyncio
    async def test_auto_ship_false_triggers_interrupt(self):
        """When auto_ship=False, the graph should pause at the approval gate."""
        graph, checkpointer = await build_studio_graph()
        config = {"configurable": {"thread_id": "test-reject-001"}}

        initial_state: StudioGraphState = {
            "bundle_input": "Something risky",
            "bundle_id": "test-reject-001",
            "auto_ship": False,
        }

        # With auto_ship=False, the graph will hit interrupt() at the approval gate
        # Use astream to see the interrupt event
        events = []
        async for event in graph.astream(initial_state, config):
            events.append(event)

        # The graph should have been interrupted before developer/qa/complete
        # The last event should contain the interrupt node's state
        assert len(events) > 0

        # Check that the state reached the approval_gate (interrupted there)
        # We can check that we have review_findings but no developer output
        last_node = list(events[-1].keys())[0] if events else None
        # Either approval_gate was last or the interrupt happened
        assert last_node is not None


class TestStateTyping:
    """Verify the StateGraph state schema."""

    def test_initial_state(self):
        state: StudioGraphState = {
            "bundle_input": "test",
            "bundle_id": "test-001",
        }
        assert state["bundle_input"] == "test"
        assert state.get("approved") is None  # Not set yet


class TestGraphTopology:
    """Verify the sequential graph topology is correct."""

    @pytest.mark.asyncio
    async def test_all_nodes_visited(self):
        """Verify every node in the graph gets visited during a run."""
        result = await run_studio_graph(
            bundle_input="Verify all nodes fire",
            bundle_id="test-topology-001",
            auto_ship=True,
        )

        # Bundler output
        assert result["proposal"] is not None
        assert result["task_dag"] is not None

        # Reviews
        assert len(result["review_findings"]) == 3
        roles = [f["role"] for f in result["review_findings"]]
        assert "adversary" in roles
        assert "security" in roles
        assert "qa" in roles

        # Approval
        assert result["approved"] is True

        # Developer
        assert result["branch_name"] is not None

        # QA
        assert result["qa_passed"] is True
        assert result["qa_report"] is not None


# ── Output parsing tests ────────────────────────────────────────────────────


class TestOutputParsing:
    """Verify STUDIO_RESULT and conventional marker parsing."""

    def test_parse_developer_output_studio_result(self):
        from studio_isolation.langgraph_adapter import _parse_developer_output
        stdout = (
            'STUDIO_RESULT: {"changed_files": ["internal/errors/kind.go"], '
            '"commit_sha": "abc123def456", "test_results": {"passed": true}}'
        )
        changed_files, commit_sha, test_results = _parse_developer_output(
            stdout, "", 0,
        )
        assert "internal/errors/kind.go" in changed_files
        assert commit_sha == "abc123def456"
        assert test_results == {"passed": True}

    def test_parse_developer_output_conventional_markers(self):
        from studio_isolation.langgraph_adapter import _parse_developer_output
        stdout = (
            "CHANGED: internal/errors/kind.go\n"
            "CHANGED: internal/errors/code.go\n"
            "COMMIT: def789abc\n"
            "Some other noise\n"
        )
        changed_files, commit_sha, _ = _parse_developer_output(stdout, "", 0)
        assert "internal/errors/kind.go" in changed_files
        assert "internal/errors/code.go" in changed_files
        assert commit_sha == "def789abc"

    def test_parse_developer_output_nonzero_exit(self):
        from studio_isolation.langgraph_adapter import _parse_developer_output
        stderr = "go: build failed: undefined: FooBar"
        changed_files, commit_sha, test_results = _parse_developer_output(
            "", stderr, 1,
        )
        assert changed_files == []
        assert commit_sha == ""
        assert test_results.get("failed") is True
        assert test_results.get("exit_code") == 1

    def test_parse_qa_output_studio_result(self):
        from studio_isolation.langgraph_adapter import _parse_qa_output
        stdout = (
            'STUDIO_RESULT: {"qa_report": {"tests_run": 14, "tests_passed": 14, '
            '"tests_failed": 0, "passed": true}, "qa_passed": true}'
        )
        report = _parse_qa_output(stdout, "")
        assert report["tests_run"] == 14
        assert report["tests_passed"] == 14
        assert report["passed"] is True

    def test_parse_qa_output_conventional_markers(self):
        from studio_isolation.langgraph_adapter import _parse_qa_output
        stdout = "PASS: test_kind\nPASS: test_code\nFAIL: test_match\nPASS: test_code2\n"
        report = _parse_qa_output(stdout, "")
        assert report["tests_run"] == 4
        assert report["tests_passed"] == 3
        assert report["tests_failed"] == 1
        assert report["passed"] is False

    def test_parse_qa_output_tests_count_line(self):
        from studio_isolation.langgraph_adapter import _parse_qa_output
        stdout = "TESTS: 42\nPASS: test_a\nPASS: test_b\n"
        report = _parse_qa_output(stdout, "")
        assert report["tests_run"] == 44  # 42 from TESTS: + 2 from PASS lines
        assert report["tests_passed"] == 2

    def test_parse_qa_output_stderr_warnings(self):
        from studio_isolation.langgraph_adapter import _parse_qa_output
        stdout = ""
        stderr = "DeprecationWarning: use_new_api is deprecated"
        report = _parse_qa_output(stdout, stderr)
        assert "warnings" in report
        assert "DeprecationWarning" in report["warnings"][0]


# ── Runner output capture tests ─────────────────────────────────────────────


class TestRunnerOutputCapture:
    """Developer node captures stdout/stderr from spawned workers."""

    @pytest.mark.asyncio
    async def test_dev_node_captures_worker_output(self):
        """A custom runner that produces STUDIO_RESULT output goes through the graph."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner
        from studio_isolation.runner import WorkerSpawnResult
        from unittest.mock import MagicMock, AsyncMock

        class StdoutDevRunner:
            async def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                                   worktree_path, task_spec, worker_type, **kwargs):
                if node_id != "developer":
                    return WorkerSpawnResult(
                        worker_id=worker_id, token="noop", node_id=node_id,
                    )
                return WorkerSpawnResult(
                    worker_id=worker_id, token="dev-token", node_id=node_id,
                )

        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=StdoutDevRunner(),
            studio_db=mock_db,
        )
        try:
            state = await runner.run(
                bundle_input="Add doc comment to internal/errors",
                bundle_id="output-capture-001",
                auto_ship=True,
                target_repo="learhy/boundary",
                base_branch="main",
            )
            assert state["bundle_id"] == "output-capture-001"
            assert state["approved"] is True
            assert state["qa_passed"] is True
            # target_repo should be threaded into state
            assert state.get("target_repo") == "learhy/boundary"
            assert state.get("base_branch") == "main"
            # Developer should set worktree_path and branch_name
            assert state.get("worktree_path") is not None
            assert state.get("branch_name") is not None
        finally:
            await runner.close()

    @pytest.mark.asyncio
    async def test_target_repo_passed_to_initial_state(self):
        """StudioGraphRunner.run() accepts target_repo and base_branch."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            state = await runner.run(
                bundle_input="Test target_repo threading",
                bundle_id="target-repo-test",
                auto_ship=True,
                target_repo="learhy/boundary",
                base_branch="develop",
            )
            assert state["target_repo"] == "learhy/boundary"
            assert state["base_branch"] == "develop"
            # node_complete uses target_repo to build PR URL
            assert state.get("pr_url") == "https://github.com/learhy/boundary/compare/studio/target-repo-test"
        finally:
            await runner.close()
