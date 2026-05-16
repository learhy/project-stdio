"""Tests for Bundle 2.4: Pre-execution Review Tracks."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.models import (
    Severity,
    FindingStatus,
    ReviewRole,
    ReviewFinding,
    ThreatModel,
    RollbackPlan,
    VerificationPlan,
    ReviewTrackOutput,
)


class TestSeverityEnum:
    def test_all_severity_values(self):
        assert Severity.INFO == "info"
        assert Severity.LOW == "low"
        assert Severity.MEDIUM == "medium"
        assert Severity.HIGH == "high"
        assert Severity.CRITICAL == "critical"

    def test_unified_enum_covers_both_adversarial_and_security(self):
        """Adversarial uses low/medium/high, security uses info/low/medium/high/critical."""
        adversarial_set = {Severity.LOW, Severity.MEDIUM, Severity.HIGH}
        security_set = {Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL}
        assert adversarial_set.issubset(security_set)


class TestReviewFinding:
    def test_default_finding(self):
        f = ReviewFinding()
        assert f.severity == Severity.LOW
        assert f.status == FindingStatus.UNRESOLVED
        assert f.category == ""
        assert f.finding == ""

    def test_finding_with_all_fields(self):
        f = ReviewFinding(
            severity=Severity.HIGH,
            status=FindingStatus.UNRESOLVED,
            category="auth",
            finding="Missing authentication on admin endpoint",
            recommendation="Add JWT middleware to /admin routes",
            rationale="Unauthenticated access to admin functions enables privilege escalation",
        )
        assert f.severity == Severity.HIGH
        assert f.category == "auth"
        assert "JWT" in f.recommendation

    def test_finding_serialization(self):
        f = ReviewFinding(
            severity=Severity.CRITICAL,
            status=FindingStatus.UNRESOLVED,
            category="secret-leak",
            finding="API key in log output",
            recommendation="Redact secrets in logging middleware",
            rationale="Logs are readable by all operators",
        )
        d = f.model_dump()
        assert d["severity"] == "critical"
        assert d["status"] == "unresolved"


class TestVerificationPlan:
    def test_default_plan(self):
        p = VerificationPlan()
        assert p.acceptance_criteria == []
        assert p.rollback_plan.machine_executable is False

    def test_full_plan(self):
        p = VerificationPlan(
            acceptance_criteria=[
                "GET /health returns 200 with {status: ok}",
                "Response time < 10ms at p99",
            ],
            test_surface={"unit": "test_health.py", "integration": "test client"},
            pre_merge_gates=["CI pass", "Coverage >= 80%"],
            post_ship_verification={"metrics": ["latency_p99"], "time_window_hours": 24},
            rollback_plan=RollbackPlan(
                machine_executable=True,
                auto_rollback_eligible=True,
                steps=["git revert <commit>", "redeploy"],
                recovery_time_estimate_seconds=120,
            ),
        )
        assert len(p.acceptance_criteria) == 2
        assert p.rollback_plan.machine_executable is True
        assert p.rollback_plan.recovery_time_estimate_seconds == 120

    def test_serialization(self):
        p = VerificationPlan(
            acceptance_criteria=["C1", "C2"],
            rollback_plan=RollbackPlan(machine_executable=False, steps=["Manual restore from backup"]),
        )
        d = p.model_dump()
        assert len(d["acceptance_criteria"]) == 2
        assert d["rollback_plan"]["machine_executable"] is False


class TestReviewTrackOutput:
    def test_adversarial_output(self):
        o = ReviewTrackOutput(
            role=ReviewRole.ADVERSARIAL,
            bundle_id="01TEST",
            findings=[
                ReviewFinding(severity=Severity.MEDIUM, category="scope-creep",
                              finding="Scope creep in requirements",
                              recommendation="Narrow scope", rationale="Too broad"),
            ],
            summary="One medium finding",
        )
        assert o.role == ReviewRole.ADVERSARIAL
        assert len(o.findings) == 1
        assert o.blocking_issue is False

    def test_security_output_with_threat_model(self):
        o = ReviewTrackOutput(
            role=ReviewRole.SECURITY,
            bundle_id="01SEC",
            findings=[],
            threat_model=ThreatModel(
                summary="Low risk",
                assets=["user data"],
                threats=["None identified"],
                mitigations=["HTTPS everywhere"],
                open_risks=[],
            ),
            summary="Clean",
        )
        assert o.threat_model is not None
        assert o.threat_model.summary == "Low risk"

    def test_qa_output_with_verification_plan(self):
        o = ReviewTrackOutput(
            role=ReviewRole.QA,
            bundle_id="01QA",
            findings=[],
            verification_plan=VerificationPlan(
                acceptance_criteria=["AC1", "AC2", "AC3"],
                pre_merge_gates=["CI", "Lint"],
            ),
            summary="Plan ready",
        )
        assert o.verification_plan is not None
        assert len(o.verification_plan.acceptance_criteria) == 3

    def test_blocking_output(self):
        o = ReviewTrackOutput(
            role=ReviewRole.ADVERSARIAL,
            bundle_id="01BLOCK",
            findings=[],
            blocking_issue=True,
            blocking_reason="Proposal is internally inconsistent",
            summary="Must re-plan",
        )
        assert o.blocking_issue is True
        assert "inconsistent" in o.blocking_reason


class TestRpcReviewHandling:
    """Tests for review track final_report handling in RpcHandlers."""

    @pytest.mark.asyncio
    async def test_handle_final_report_review_adversarial_success(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        complete_cb = AsyncMock()
        blocking_cb = AsyncMock()
        handlers.set_on_review_complete(complete_cb)
        handlers.set_on_review_blocking(blocking_cb)

        binding = WorkerBinding(
            worker_id="review_adversarial_01",
            bundle_id="01TEST",
            node_id="adversarial",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        findings = [
            {"severity": "medium", "status": "unresolved", "category": "scope-creep",
             "finding": "Scope too broad", "recommendation": "Narrow scope", "rationale": "Risk of churn"},
        ]

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "One medium finding",
            "findings": findings,
            "blocking_issue": False,
        }, 1)

        assert result["accepted"] is True
        assert result["review"] is True
        assert result["role"] == "adversarial"
        complete_cb.assert_called_once_with("01TEST", "adversarial", findings)
        blocking_cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_final_report_review_blocking_issue(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        complete_cb = AsyncMock()
        blocking_cb = AsyncMock()
        handlers.set_on_review_complete(complete_cb)
        handlers.set_on_review_blocking(blocking_cb)

        binding = WorkerBinding(
            worker_id="review_security_01",
            bundle_id="01BLOCK",
            node_id="security",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "Critical finding blocks bundle",
            "findings": [
                {"severity": "critical", "status": "unresolved", "category": "secret-leak",
                 "finding": "API key exposed", "recommendation": "Use env vars",
                 "rationale": "Key is in source code"},
            ],
            "blocking_issue": True,
            "blocking_reason": "Critical secret leak must be fixed before review",
        }, 1)

        assert result["accepted"] is True
        assert result["review"] is True
        blocking_cb.assert_called_once_with("01BLOCK", "Critical secret leak must be fixed before review")

    @pytest.mark.asyncio
    async def test_handle_final_report_review_security_with_threat_model(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        handlers.set_on_review_complete(AsyncMock())
        handlers.set_on_review_blocking(AsyncMock())

        binding = WorkerBinding(
            worker_id="review_security_02",
            bundle_id="01SEC",
            node_id="security",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        threat_model = {
            "summary": "Low risk bundle",
            "assets": ["user sessions"],
            "threats": ["session fixation"],
            "mitigations": ["Rotate session on login"],
            "open_risks": [],
        }

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "Security review complete",
            "findings": [],
            "threat_model": threat_model,
            "blocking_issue": False,
        }, 1)

        assert result["accepted"] is True
        assert result["role"] == "security"

    @pytest.mark.asyncio
    async def test_handle_final_report_review_qa_with_plan(self):
        from studio.orchestrator.rpc import RpcHandlers, WorkerBinding

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        handlers = RpcHandlers(db)
        handlers.set_on_review_complete(AsyncMock())
        handlers.set_on_review_blocking(AsyncMock())

        binding = WorkerBinding(
            worker_id="review_qa_01",
            bundle_id="01QA",
            node_id="qa",
            rpc_methods=["worker.*"],
            reader=MagicMock(),
            writer=MagicMock(),
        )

        verification_plan = {
            "acceptance_criteria": ["AC1", "AC2", "AC3"],
            "test_surface": {"unit": "yes"},
            "pre_merge_gates": ["CI", "Lint"],
            "post_ship_verification": {},
            "rollback_plan": {
                "machine_executable": False,
                "auto_rollback_eligible": False,
                "steps": ["Manual restore"],
                "recovery_time_estimate_seconds": 300,
            },
        }

        result = await handlers.handle_final_report(binding, {
            "outcome": "success",
            "summary": "QA plan ready",
            "findings": [],
            "verification_plan": verification_plan,
            "blocking_issue": False,
        }, 1)

        assert result["accepted"] is True
        assert result["role"] == "qa"


class TestStateMachineBundlerInjection:
    """Tests for review track node injection during bundler planning."""

    @pytest.mark.asyncio
    async def test_transition_complete_bundler_planning_injects_review_nodes(self):
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
            "concerns": ["Minimal"],
            "task_dag": {
                "nodes": [
                    {"id": "n1", "kind": "worker", "spec": {"objective": "Implement"}},
                    {"id": "n2", "kind": "worker", "spec": {"objective": "Test"}},
                ],
                "edges": [
                    {"from": "n1", "to": "n2", "condition": {"kind": "on_success"}},
                ],
                "entry_nodes": ["n1"],
            },
        }

        await sm.transition_complete_bundler_planning("01INJECT", proposal)

        # Check review track nodes were inserted
        insert_calls = [c for c in db.execute.call_args_list
                       if "INSERT INTO dag_nodes" in str(c[0][0])]
        # 3 review nodes + 1 aggregator + 2 work nodes = 6 inserts
        assert len(insert_calls) == 6

        # Check review track edges: adversarial -> aggregator, security -> aggregator, qa -> aggregator
        edge_calls = [c for c in db.execute.call_args_list
                     if "INSERT INTO dag_edges" in str(c[0][0])]
        # 3 (review -> aggregator) + 1 (original edge) + 1 (aggregator -> n1) = 5
        assert len(edge_calls) == 5

        # Verify one of the injected edges: aggregator -> entry node
        edge_args = [c[0][0] for c in edge_calls]
        edge_params_list = [c[0][1] for c in edge_calls]
        found_agg_to_n1 = False
        for params in edge_params_list:
            if params[1] == "review-aggregator" and params[2] == "n1":
                found_agg_to_n1 = True
        assert found_agg_to_n1, "Missing aggregator -> n1 edge"

    @pytest.mark.asyncio
    async def test_transition_complete_bundler_planning_without_entry_nodes(self):
        """Should find nodes with no incoming edges when entry_nodes is empty."""
        from studio.orchestrator.state_machine import BundleStateMachine, BundleState

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value={
            "state": BundleState.PROPOSED,
            "proposal_json": "{}",
        })
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        db.transaction = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock()

        sm = BundleStateMachine(db, kernel_mode=False)

        proposal = {
            "task_dag": {
                "nodes": [
                    {"id": "n1", "kind": "worker", "spec": {}},
                ],
                "edges": [],
                "entry_nodes": [],
            },
        }

        await sm.transition_complete_bundler_planning("01FALLBACK", proposal)

        # Should have added an edge from review-aggregator -> n1 (fallback: no incoming edges)
        edge_calls = [c for c in db.execute.call_args_list
                     if "INSERT INTO dag_edges" in str(c[0][0])]
        found_agg_to_n1 = False
        for params in [c[0][1] for c in edge_calls]:
            if params[1] == "review-aggregator" and params[2] == "n1":
                found_agg_to_n1 = True
        assert found_agg_to_n1, "Fallback: should wire aggregator to node with no incoming edges"


class TestStateMachineTransition3:
    """Tests for Transition 3 (IN_REVIEW -> PROPOSED)."""

    @pytest.mark.asyncio
    async def test_transition_3_legal(self):
        from studio.orchestrator.state_machine import BundleStateMachine, BundleState

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value={"state": BundleState.IN_REVIEW})
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        db.transaction = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock()

        sm = BundleStateMachine(db, kernel_mode=False)
        await sm.transition_3_return_to_proposed("01TEST", "Security found critical issue")

        update_calls = [c for c in db.execute.call_args_list
                       if "UPDATE bundles" in str(c[0][0])]
        assert len(update_calls) == 1
        assert update_calls[0][0][1][0] == BundleState.PROPOSED

    @pytest.mark.asyncio
    async def test_transition_3_from_wrong_state_fails(self):
        from studio.orchestrator.state_machine import BundleStateMachine, BundleState, IllegalTransitionError

        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value={"state": BundleState.APPROVED})

        sm = BundleStateMachine(db, kernel_mode=False)
        with pytest.raises(IllegalTransitionError):
            await sm.transition_3_return_to_proposed("01TEST", "reason")


class TestReviewWorkerPrompts:
    """Unit tests for review worker system prompts."""

    def test_adversarial_prompt_contains_required_sections(self):
        from studio.workers.review import _ADVERSARIAL_PROMPT

        assert "findings" in _ADVERSARIAL_PROMPT
        assert "severity" in _ADVERSARIAL_PROMPT
        assert "blocking_issue" in _ADVERSARIAL_PROMPT
        assert "scope-creep" in _ADVERSARIAL_PROMPT
        assert "hidden-complexity" in _ADVERSARIAL_PROMPT

    def test_security_prompt_has_threat_model_requirement(self):
        from studio.workers.review import _SECURITY_PROMPT

        assert "threat_model" in _SECURITY_PROMPT
        assert "critical" in _SECURITY_PROMPT
        assert "auth" in _SECURITY_PROMPT
        assert "secret" in _SECURITY_PROMPT
        assert "PII" in _SECURITY_PROMPT

    def test_security_prompt_has_hard_rules(self):
        from studio.workers.review import _SECURITY_PROMPT

        assert "never auto-ship" in _SECURITY_PROMPT or "auto-ship" in _SECURITY_PROMPT
        assert "unresolved" in _SECURITY_PROMPT

    def test_qa_prompt_has_verification_plan_structure(self):
        from studio.workers.review import _QA_PROMPT

        assert "verification_plan" in _QA_PROMPT
        assert "acceptance_criteria" in _QA_PROMPT
        assert "rollback_plan" in _QA_PROMPT
        assert "pre_merge_gates" in _QA_PROMPT

    def test_qa_prompt_hard_rules(self):
        from studio.workers.review import _QA_PROMPT

        assert "no bundle reaches human review" in _QA_PROMPT.lower() or "without a Verification Plan" in _QA_PROMPT
        assert "observable" in _QA_PROMPT and "testable" in _QA_PROMPT

    def test_extract_json_markdown_fence(self):
        from studio.workers.review import _extract_json

        text = '```json\n{"findings": []}\n```'
        result = _extract_json(text)
        assert result == {"findings": []}

    def test_extract_json_plain(self):
        from studio.workers.review import _extract_json

        result = _extract_json('{"findings": [{"severity": "high"}]}')
        assert result["findings"][0]["severity"] == "high"

    def test_extract_json_fallback(self):
        from studio.workers.review import _extract_json

        result = _extract_json("not json at all")
        assert result.get("parse_error") is True

    def test_build_proposal_context(self):
        from studio.workers.review import _build_proposal_context

        task_spec = {
            "idea": "Add health check",
            "requirements_summary": "Need GET /health endpoint",
            "complexity_score": 2,
            "risk_score": 1,
            "concerns": ["No rollback plan"],
        }
        ctx = _build_proposal_context(task_spec)
        assert "Add health check" in ctx
        assert "GET /health" in ctx
        assert "Complexity: 2/10" in ctx
        assert "No rollback plan" in ctx


class TestReviewWorkerRoleDispatch:
    """Tests for ReviewWorker role-dispatch logic."""

    def test_role_dispatch_adversarial(self):
        with patch("studio.workers.review._TOKEN", "test-token"), \
             patch("studio.workers.review._NODE_ID", "adversarial"), \
             patch("studio.workers.review._TASK_SPEC_RAW", json.dumps({"role": "adversarial"})):
            from studio.workers.review import ReviewWorker
            w = ReviewWorker()
            assert w.role == "adversarial"

    def test_role_dispatch_security(self):
        with patch("studio.workers.review._TOKEN", "test-token"), \
             patch("studio.workers.review._NODE_ID", "security"), \
             patch("studio.workers.review._TASK_SPEC_RAW", json.dumps({"role": "security"})):
            from studio.workers.review import ReviewWorker
            w = ReviewWorker()
            assert w.role == "security"

    def test_role_dispatch_qa(self):
        with patch("studio.workers.review._TOKEN", "test-token"), \
             patch("studio.workers.review._NODE_ID", "qa"), \
             patch("studio.workers.review._TASK_SPEC_RAW", json.dumps({"role": "qa"})):
            from studio.workers.review import ReviewWorker
            w = ReviewWorker()
            assert w.role == "qa"

    def test_role_fallback_to_node_id(self):
        with patch("studio.workers.review._TOKEN", "test-token"), \
             patch("studio.workers.review._NODE_ID", "adversarial"), \
             patch("studio.workers.review._TASK_SPEC_RAW", "{}"):  # no role in spec

            from studio.workers.review import ReviewWorker
            w = ReviewWorker()
            assert w.role == "adversarial"  # falls back to NODE_ID


class TestApprovalMatrixStub:
    """Tests for the approval matrix evaluator stub in Bundle 2.4."""

    @pytest.mark.asyncio
    async def test_evaluate_approval_matrix_auto_tier_approves(self):
        """With low complexity and low risk, auto-approve fires transition 4."""
        from studio.orchestrator.main import Orchestrator

        app = Orchestrator()
        app.sm = MagicMock()
        app.sm.transition_4_approve_from_review = AsyncMock()
        app.sm._github_post_mirror = AsyncMock()
        app.sm.transition_6_start_execution = AsyncMock()
        app.sm.now = MagicMock(return_value=1700000000)

        # Mock DB to return a low-score bundle proposal
        app.db = MagicMock()
        app.db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({"proposal": {"complexity_score": 1, "risk_score": 1}}),
            "complexity_score": 1,
            "risk_score": 1,
            "tier": "full_review",
        })
        app.db.execute = AsyncMock()
        app.db.conn = MagicMock()
        app.db.conn.commit = AsyncMock()

        # Mock executor with artifact store
        app.executor = MagicMock()
        app.executor._artifact_store = None
        app.executor.start_bundle = AsyncMock()

        await app._evaluate_approval_matrix("01TEST", {})

        app.sm.transition_4_approve_from_review.assert_called_once_with(
            "01TEST", "approval-matrix"
        )
        app.sm.transition_6_start_execution.assert_called_once_with("01TEST")
        app.executor.start_bundle.assert_called_once_with("01TEST")


class TestMemoryRootSetting:
    """Tests for the memory_root setting."""

    def test_default_memory_root(self):
        from studio.orchestrator.models import OrchestratorSettings
        s = OrchestratorSettings()
        assert s.memory_root == "memory/"

    def test_custom_memory_root(self):
        from studio.orchestrator.models import OrchestratorSettings
        s = OrchestratorSettings(memory_root="/var/lib/studio/memory/")
        assert s.memory_root == "/var/lib/studio/memory/"
