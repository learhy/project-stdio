"""Tests for Bundle 2.3: Bundler Agent — submit path and worker logic."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.models import BundleProposal


class TestBundleProposal:
    def test_default_proposal(self):
        p = BundleProposal()
        assert p.complexity_score == 0
        assert p.risk_score == 0
        assert p.target == "control-plane"
        assert p.concerns == []
        assert p.task_dag == {}

    def test_valid_proposal_scores_in_range(self):
        p = BundleProposal(complexity_score=5, risk_score=7)
        assert 0 <= p.complexity_score <= 10
        assert 0 <= p.risk_score <= 10

    def test_scores_enforced_at_validation(self):
        """Pydantic v2 enforces Field(ge=0, le=10) at construction time."""
        with pytest.raises(Exception):
            BundleProposal(complexity_score=15, risk_score=-3)

    def test_proposal_with_dag(self):
        dag = {
            "nodes": [
                {"id": "task-1", "kind": "worker", "spec": {"objective": "Build X"}},
                {"id": "task-2", "kind": "gate", "spec": {"predicate": {"kind": "human_approval"}}},
            ],
            "edges": [
                {"from": "task-1", "to": "task-2", "condition": {"kind": "on_success"}},
            ],
        }
        p = BundleProposal(
            complexity_score=3,
            risk_score=2,
            concerns=["Test concern"],
            task_dag=dag,
        )
        assert len(p.task_dag["nodes"]) == 2
        assert p.task_dag["nodes"][0]["id"] == "task-1"
        assert p.concerns[0] == "Test concern"

    def test_proposal_factor_breakdowns(self):
        p = BundleProposal(
            complexity_factors={"loc": 3, "components_touched": 2},
            risk_factors={"reversibility": 1, "security_sensitive_paths": 0},
        )
        assert p.complexity_factors["loc"] == 3
        assert p.risk_factors["reversibility"] == 1

    def test_proposal_serialization(self):
        p = BundleProposal(
            complexity_score=4,
            risk_score=1,
            target="new-repo",
            target_rationale="Creates a new deployable unit",
            concerns=["Missing rollback plan"],
        )
        dumped = p.model_dump()
        assert dumped["complexity_score"] == 4
        assert dumped["target"] == "new-repo"
        assert dumped["concerns"] == ["Missing rollback plan"]


class TestBundlerSubmitPath:
    """Integration-level tests: submit with just bundle_input, verify bundler flow."""

    @pytest.mark.asyncio
    async def test_submit_idea_only_returns_bundle_id(self):
        """Submitting without a task_dag should return a bundle_id with mode=planning."""
        from studio.orchestrator.main import _cli_submit
        from studio.orchestrator.db import Database

        app = MagicMock()
        app.sm = MagicMock()
        app.sm.transition_1_submit = AsyncMock()
        app.sm.transition_1_submit_idea = AsyncMock()
        app.db = MagicMock()
        app.db.execute = AsyncMock()
        app.db.fetch_one = AsyncMock(return_value=None)
        app.db.conn = MagicMock()
        app.db.conn.commit = AsyncMock()
        app.settings = MagicMock()
        app.settings.orchestrator = MagicMock()
        app.settings.orchestrator.socket_path = "/tmp/test.sock"
        app.settings.ollama_cloud = MagicMock()
        app.settings.ollama_cloud.base_url = "https://ollama.com/api"
        app.db.transaction = MagicMock()
        app.db.transaction.return_value.__aenter__ = AsyncMock()
        app.db.transaction.return_value.__aexit__ = AsyncMock()

        params = {
            "submission": {
                "bundle_input": {"idea": "Add a health check endpoint"},
            }
        }

        with patch("studio.orchestrator.main._spawn_bundler", new_callable=AsyncMock) as mock_spawn:
            result = await _cli_submit(app, params)
            assert "bundle_id" in result
            assert result["mode"] == "planning"
            app.sm.transition_1_submit_idea.assert_called_once()
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_with_dag_uses_kernel_direct_path(self):
        """Submitting with a task_dag should use the existing kernel-direct path."""
        from studio.orchestrator.main import _cli_submit

        app = MagicMock()
        app.sm = MagicMock()
        app.sm.transition_1_submit = AsyncMock()
        app.sm.transition_1_submit_idea = AsyncMock()
        app.db = MagicMock()
        app.db.execute = AsyncMock()
        app.db.fetch_one = AsyncMock(return_value=None)
        app.db.conn = MagicMock()
        app.db.conn.commit = AsyncMock()
        app.settings = MagicMock()
        app.settings.orchestrator = MagicMock()
        app.settings.orchestrator.socket_path = "/tmp/test.sock"
        app.settings.ollama_cloud = MagicMock()
        app.settings.ollama_cloud.base_url = "https://ollama.com/api"
        app.db.transaction = MagicMock()
        app.db.transaction.return_value.__aenter__ = AsyncMock()
        app.db.transaction.return_value.__aexit__ = AsyncMock()

        params = {
            "submission": {
                "bundle_input": {"idea": "Test"},
                "task_dag": {
                    "nodes": [{"id": "task-1", "kind": "worker", "spec": {}}],
                    "edges": [],
                },
            }
        }

        with patch("studio.orchestrator.main._spawn_bundler", new_callable=AsyncMock) as mock_spawn:
            result = await _cli_submit(app, params)
            assert "bundle_id" in result
            assert "mode" not in result  # No planning mode for kernel-direct
            app.sm.transition_1_submit.assert_called_once()
            app.sm.transition_1_submit_idea.assert_not_called()
            mock_spawn.assert_not_called()


class TestBundlerFinalReport:
    """Tests for the bundler's final_report RPC handling."""

    @pytest.mark.asyncio
    async def test_bundler_final_report_calls_callback(self):
        """Bundler final_report should call on_bundler_report with the proposal."""
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        bundler_cb = AsyncMock()
        handlers.set_on_bundler_report(bundler_cb)

        binding = WorkerBinding(
            worker_id="bundler_01TEST",
            bundle_id="01TEST",
            node_id="bundler",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        proposal = {
            "complexity_score": 3,
            "risk_score": 2,
            "concerns": ["Test concern"],
            "task_dag": {
                "nodes": [{"id": "task-1", "kind": "worker", "spec": {}}],
                "edges": [],
            },
        }

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "Planned",
            "proposal": proposal,
        }, 1)

        assert result["accepted"] is True
        assert result["bundler"] is True
        bundler_cb.assert_called_once_with("01TEST", proposal)

    @pytest.mark.asyncio
    async def test_bundler_final_report_without_proposal(self):
        """Bundler final_report with failure outcome should still update worker state."""
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        bundler_cb = AsyncMock()
        handlers.set_on_bundler_report(bundler_cb)

        binding = WorkerBinding(
            worker_id="bundler_fail",
            bundle_id="01FAIL",
            node_id="bundler",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        result = await handlers.handle_final_report(binding, {
            "outcome": "failure",
            "summary": "LLM API unreachable",
            "errors": ["Connection refused"],
        }, 1)

        assert result["accepted"] is True
        assert result["bundler"] is True
        bundler_cb.assert_not_called()  # No callback on failure


class TestBundlerStateMachine:
    """Tests for the new state machine transitions."""

    @pytest.mark.asyncio
    async def test_transition_1_submit_idea_creates_bundle(self):
        from studio.orchestrator.state_machine import BundleStateMachine

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = AsyncMock()
        db.conn.commit = AsyncMock()
        db.transaction = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock()

        sm = BundleStateMachine(db, kernel_mode=False)
        await sm.transition_1_submit_idea("01IDEA", {"idea": "Build X"})

        # Verify INSERT was called
        insert_call = db.execute.call_args_list[0]
        assert insert_call[0][0].startswith("INSERT INTO bundles")
        assert insert_call[0][1][0] == "01IDEA"

    @pytest.mark.asyncio
    async def test_transition_complete_bundler_planning(self):
        from studio.orchestrator.state_machine import BundleStateMachine, BundleState

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value={
            "state": BundleState.PROPOSED,
            "proposal_json": json.dumps({"bundle_input": {"idea": "Build X"}}),
        })
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        db.transaction = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock()

        sm = BundleStateMachine(db, kernel_mode=False)

        proposal = {
            "complexity_score": 2,
            "risk_score": 1,
            "concerns": ["Minimal risk"],
            "task_dag": {
                "nodes": [
                    {"id": "n1", "kind": "worker", "spec": {"objective": "Implement"}},
                ],
                "edges": [
                    {"from": "n1", "to": "n2", "condition": {"kind": "on_success"}},
                ],
            },
        }

        await sm.transition_complete_bundler_planning("01IDEA", proposal)

        # Should update bundles.proposal_json and state
        update_calls = [c for c in db.execute.call_args_list
                       if "UPDATE bundles" in str(c[0][0])]
        assert len(update_calls) >= 2  # proposal_json update + state update

        # Should insert DAG nodes
        insert_calls = [c for c in db.execute.call_args_list
                       if "INSERT INTO dag_nodes" in str(c[0][0])]
        assert len(insert_calls) == 1

        # Should insert DAG edges
        edge_calls = [c for c in db.execute.call_args_list
                     if "INSERT INTO dag_edges" in str(c[0][0])]
        assert len(edge_calls) == 1

    @pytest.mark.asyncio
    async def test_transition_bundler_failed(self):
        from studio.orchestrator.state_machine import BundleStateMachine, BundleState

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value={"state": BundleState.PROPOSED})
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        db.transaction = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock()

        sm = BundleStateMachine(db, kernel_mode=False)
        await sm.transition_bundler_failed("01STUCK", "LLM parse failure")

        update_calls = [c for c in db.execute.call_args_list
                       if "UPDATE bundles" in str(c[0][0])]
        assert len(update_calls) == 1
        assert update_calls[0][0][1][0] == BundleState.FAILED
        assert update_calls[0][0][1][1] == "01STUCK"


class TestBundlerFailureCallback:
    """Tests for bundler failure detection in handle_final_report."""

    @pytest.mark.asyncio
    async def test_bundler_failure_triggers_callback(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        failure_cb = AsyncMock()
        handlers.set_on_bundler_failure(failure_cb)
        handlers.set_on_bundler_report(AsyncMock())

        binding = WorkerBinding(
            worker_id="bundler_01FAIL",
            bundle_id="01FAIL",
            node_id="bundler",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        result = await handlers.handle_final_report(binding, {
            "outcome": "failure",
            "summary": "Failed to parse LLM response as structured JSON proposal",
            "errors": ["LLM response could not be parsed as JSON"],
        }, 1)

        assert result["accepted"] is True
        assert result["bundler"] is True
        failure_cb.assert_called_once_with("01FAIL", "Failed to parse LLM response as structured JSON proposal")

    @pytest.mark.asyncio
    async def test_bundler_success_does_not_trigger_failure_callback(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        success_cb = AsyncMock()
        failure_cb = AsyncMock()
        handlers.set_on_bundler_report(success_cb)
        handlers.set_on_bundler_failure(failure_cb)

        binding = WorkerBinding(
            worker_id="bundler_01OK",
            bundle_id="01OK",
            node_id="bundler",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "Planned successfully",
            "proposal": {
                "complexity_score": 3,
                "risk_score": 2,
                "concerns": ["Test concern"],
            },
        }, 1)

        assert result["accepted"] is True
        assert result["bundler"] is True
        success_cb.assert_called_once()
        failure_cb.assert_not_called()


class TestBundlerWorker:
    """Unit tests for BundlerWorker execution logic."""

    def test_system_prompt_contains_required_sections(self):
        """The system prompt must include scoring, target, and concerns sections."""
        from studio.workers.bundler import _BUNDLER_SYSTEM_PROMPT

        assert "complexity_score" in _BUNDLER_SYSTEM_PROMPT
        assert "risk_score" in _BUNDLER_SYSTEM_PROMPT
        assert "complexity_factors" in _BUNDLER_SYSTEM_PROMPT
        assert "risk_factors" in _BUNDLER_SYSTEM_PROMPT
        assert "concerns" in _BUNDLER_SYSTEM_PROMPT
        assert "target" in _BUNDLER_SYSTEM_PROMPT
        assert "task_dag" in _BUNDLER_SYSTEM_PROMPT
        assert "non-empty" in _BUNDLER_SYSTEM_PROMPT.lower()
        assert "calibration signal" in _BUNDLER_SYSTEM_PROMPT

    def test_system_prompt_has_required_constraints(self):
        """System prompt must enforce score ranges and concerns rules."""
        from studio.workers.bundler import _BUNDLER_SYSTEM_PROMPT

        assert "0-10" in _BUNDLER_SYSTEM_PROMPT
        assert "reversibility" in _BUNDLER_SYSTEM_PROMPT
        assert "production_proximity" in _BUNDLER_SYSTEM_PROMPT
        assert "0 in v1" in _BUNDLER_SYSTEM_PROMPT

    def test_extract_json_from_markdown_fence(self):
        from studio.workers.bundler import _extract_json

        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_extract_json_plain(self):
        from studio.workers.bundler import _extract_json

        result = _extract_json('{"a": 1}')
        assert result == {"a": 1}

    def test_extract_json_brace_extraction(self):
        from studio.workers.bundler import _extract_json

        text = 'Some preamble text... {"complexity_score": 5, "risk_score": 3} trailing text'
        result = _extract_json(text)
        assert result == {"complexity_score": 5, "risk_score": 3}

    def test_extract_json_fallback(self):
        from studio.workers.bundler import _extract_json

        result = _extract_json("not json at all")
        assert result.get("parse_error") is True

    def test_memory_reader_returns_none_for_missing_file(self):
        with patch("studio.workers.bundler.os.path.exists", return_value=False):
            from studio.workers.bundler import _read_file
            assert _read_file("nonexistent.md") is None
