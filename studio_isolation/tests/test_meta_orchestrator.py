"""Tests for the Hermes MetaOrchestrator (phase 3)."""

import pytest
from studio_isolation.meta_orchestrator import (
    MetaOrchestrator,
    DecomposedIntent,
    ExecutionResult,
)


# ── Relay helpers for interrupt tests ──────────────────────────────────────


def _make_approve_relay():
    """Return a relay that always approves."""

    async def _approve(message: str) -> str:
        return "approve"

    return _approve


def _make_reject_relay(reason: str):
    """Return a relay that always rejects with the given reason."""

    async def _reject(message: str) -> str:
        return f"reject: {reason}"

    return _reject


class TestIntentDecomposition:
    """Verify intent → DecomposedIntent conversion."""

    def test_simple_intent(self):
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Add a comment to README",
            bundle_id="test-001",
        )
        assert result.bundle_id == "test-001"
        assert result.bundle_input == "Add a comment to README"
        assert result.proposal["complexity_score"] == 2  # low complexity keywords
        assert result.proposal["risk_score"] == 2  # no risky keywords
        assert result.auto_ship is True  # auto tier
        assert result.approval_tier == "auto"

    def test_medium_complexity_intent(self):
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Refactor the auth module to use new encryption library",
            bundle_id="test-002",
        )
        assert result.proposal["complexity_score"] == 6  # "refactor", "auth", "encrypt"
        assert result.proposal["risk_score"] == 4  # auth, encrypt
        assert result.auto_ship is False  # full_review_cooldown
        assert "auth" in result.tags
        assert "middleware" not in result.tags

    def test_middleware_intent_adds_tags(self):
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Add a RateLimiter middleware to gRPC interceptor",
            bundle_id="test-003",
        )
        assert "rate-limiting" in result.tags
        assert "middleware" in result.tags
        assert result.approval_tier == "full_review"

    def test_task_dag_structure(self):
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Fix typo in docs",
            bundle_id="test-004",
        )
        dag = result.task_dag
        assert "nodes" in dag
        node_ids = {n["id"] for n in dag["nodes"]}
        assert "clone" in node_ids
        assert "research" in node_ids
        assert "implement" in node_ids
        assert "test" in node_ids
        assert "pr" in node_ids

    def test_to_initial_state(self):
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Add docs",
            bundle_id="test-005",
        )
        state = result.to_initial_state()
        assert state["bundle_id"] == "test-005"
        assert state["bundle_input"] == "Add docs"
        assert state["auto_ship"] is True
        assert "branch_name" in state
        assert state["review_findings"] == []


class TestApprovalResponseParsing:
    """Verify human approval response parsing."""

    def test_approve(self):
        result = MetaOrchestrator._parse_approval_response("approve")
        assert result["approved"] is True
        assert result["decision"] == "approved"
        assert result["reason"] == ""

    def test_approve_case_insensitive(self):
        result = MetaOrchestrator._parse_approval_response("APPROVE")
        assert result["approved"] is True
        assert result["decision"] == "approved"

    def test_reject_with_reason(self):
        result = MetaOrchestrator._parse_approval_response("reject: too risky")
        assert result["approved"] is False
        assert result["decision"] == "rejected"
        assert result["reason"] == "too risky"

    def test_reject_without_reason(self):
        result = MetaOrchestrator._parse_approval_response("reject")
        assert result["approved"] is False
        assert result["decision"] == "rejected"
        assert result["reason"] == ""

    def test_modify_with_instructions(self):
        result = MetaOrchestrator._parse_approval_response("modify: add tests first")
        assert result["approved"] is True
        assert result["decision"] == "modify"
        assert result["reason"] == "add tests first"

    def test_default_approval(self):
        """Any unrecognized response defaults to approve."""
        result = MetaOrchestrator._parse_approval_response("lgtm ship it")
        assert result["approved"] is True
        assert result["decision"] == "approved"


class TestDecomposedIntentDataclass:
    """Verify DecomposedIntent construction and conversion."""

    def test_defaults(self):
        di = DecomposedIntent(bundle_id="b-1", bundle_input="test")
        assert di.target_repo == ""
        assert di.proposal == {}
        assert di.auto_ship is False

    def test_full_construction(self):
        di = DecomposedIntent(
            bundle_id="b-2",
            bundle_input="Add feature X",
            target_repo="learhy/boundary",
            auto_ship=True,
            approval_tier="auto",
        )
        state = di.to_initial_state()
        assert state["bundle_id"] == "b-2"
        assert state["auto_ship"] is True
        assert state["approval_tier"] == "auto"
        assert state["approved"] is True


class TestExecutionResult:
    """Verify ExecutionResult construction."""

    def test_success_result(self):
        result = ExecutionResult(
            bundle_id="b-1",
            success=True,
            state={},
            pr_url="https://github.com/learhy/boundary/pull/1",
            commit_sha="abc123",
        )
        assert result.success is True
        assert result.pr_url is not None
        assert result.error == ""

    def test_error_result(self):
        result = ExecutionResult(
            bundle_id="b-2",
            success=False,
            state={},
            error="something went wrong",
        )
        assert result.success is False
        assert result.error == "something went wrong"
        assert result.pr_url == ""


class TestMetaOrchestratorLifecycle:
    """Verify MetaOrchestrator create/close lifecycle."""

    @pytest.mark.asyncio
    async def test_create_and_close(self):
        orch = await MetaOrchestrator.create(db_path=":memory:")
        assert orch.runner is not None
        await orch.close()

    @pytest.mark.asyncio
    async def test_create_with_relay(self):
        captured_messages: list[str] = []

        async def fake_relay(message: str) -> str:
            captured_messages.append(message)
            return "approve"

        orch = await MetaOrchestrator.create(db_path=":memory:", relay=fake_relay)
        assert orch.relay is fake_relay
        await orch.close()

    @pytest.mark.asyncio
    async def test_execute_auto_ship(self):
        """Execute an auto-approve bundle end-to-end."""
        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            result = await orch.execute(
                intent="Add a comment to README",
                bundle_id="test-auto-exec",
                auto_ship=True,
            )
            assert result.success is True
            assert result.bundle_id == "test-auto-exec"
            # State should have traversed all nodes
            assert "proposal" in result.state
            assert "review_findings" in result.state
            assert result.state["approved"] is True
            assert result.state["qa_passed"] is True
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_execute_different_intents(self):
        """Different intents produce different results."""
        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            r1 = await orch.execute(
                intent="Add logging to auth module",
                bundle_id="intent-a",
                auto_ship=True,
            )
            r2 = await orch.execute(
                intent="Fix typo in README",
                bundle_id="intent-b",
                auto_ship=True,
            )
            assert r1.bundle_id == "intent-a"
            assert r2.bundle_id == "intent-b"
            assert r1.state["bundle_input"] != r2.state["bundle_input"]
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_interrupt_resume_with_relay(self):
        """Full interrupt→resume cycle: graph pauses, relay fires, resume completes.

        This exercises the checkpoint retrieval path (_get_checkpointed_state),
        the relay message formatting, and the Command(resume=...) path through
        LangGraph's checkpointer. The checkpointed state at interrupt time
        contains bundler output and review findings — not just the pre-graph
        decomposition.
        """
        orch = await MetaOrchestrator.create(
            db_path=":memory:",
            relay=_make_approve_relay(),
        )
        try:
            # auto_ship=False forces the interrupt at approval_gate
            result = await orch.execute(
                intent="Add structured logging middleware to gRPC interceptor chain",
                bundle_id="test-interrupt-resume",
                auto_ship=False,
            )
            assert result.success is True
            assert result.was_interrupted is True
            assert result.human_decision == "approved"
            # After resume, the graph completed all nodes
            assert result.state["approved"] is True
            assert result.state["qa_passed"] is True
            assert result.state["bundle_id"] == "test-interrupt-resume"
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_interrupt_reject_stops_graph(self):
        """Rejecting at the interrupt gate halts execution before developer."""
        orch = await MetaOrchestrator.create(
            db_path=":memory:",
            relay=_make_reject_relay(reason="too risky"),
        )
        try:
            result = await orch.execute(
                intent="Rewrite the entire auth module with a new encryption scheme",
                bundle_id="test-interrupt-reject",
                auto_ship=False,
            )
            assert result.success is False
            assert result.was_interrupted is True
            assert result.human_decision == "rejected"
            # The graph stopped before developer/qa
            assert result.state.get("qa_passed") is not True
        finally:
            await orch.close()
