"""Tests for approval.py — matrix evaluator, cooldown enforcement, triggers."""
import pytest
from studio.orchestrator.approval import (
    matrix_lookup,
    evaluate_approval_matrix,
    CooldownError,
    cooldown_seconds,
    MandatoryReviewTrigger,
    ApprovalDecision,
)
from studio.orchestrator.models import ApprovalTier, BundleProposal, ApprovalSettings


# ── matrix_lookup ────────────────────────────────────────────────────────────

class TestMatrixLookup:
    """Verify all 9 cells of the 3x3 approval matrix."""

    def test_all_nine_cells(self):
        cells = {
            # (complexity, risk) -> expected tier
            (1, 1): ApprovalTier.AUTO,
            (4, 1): ApprovalTier.AUTO_NOTIFY,
            (7, 1): ApprovalTier.SUMMARY,
            (1, 3): ApprovalTier.SUMMARY,
            (4, 3): ApprovalTier.SUMMARY,
            (7, 4): ApprovalTier.FULL_REVIEW,
            (1, 8): ApprovalTier.FULL_REVIEW,
            (4, 8): ApprovalTier.FULL_REVIEW,
            (7, 8): ApprovalTier.FULL_REVIEW_COOLDOWN,
        }
        for (c, r), expected in cells.items():
            assert matrix_lookup(c, r) == expected, f"({c}, {r}) → {expected}"

    def test_boundary_low_med_complexity(self):
        """Complexity 3 is low, 4 is med."""
        assert matrix_lookup(3, 1) == ApprovalTier.AUTO
        assert matrix_lookup(4, 1) == ApprovalTier.AUTO_NOTIFY

    def test_boundary_med_high_complexity(self):
        """Complexity 6 is med, 7 is high."""
        assert matrix_lookup(6, 1) == ApprovalTier.AUTO_NOTIFY
        assert matrix_lookup(7, 1) == ApprovalTier.SUMMARY

    def test_boundary_low_med_risk(self):
        """Risk 2 is low, 3 is med."""
        assert matrix_lookup(1, 2) == ApprovalTier.AUTO
        assert matrix_lookup(1, 3) == ApprovalTier.SUMMARY

    def test_boundary_med_high_risk(self):
        """Risk 5 is med, 6 is high."""
        assert matrix_lookup(1, 5) == ApprovalTier.SUMMARY
        assert matrix_lookup(1, 6) == ApprovalTier.FULL_REVIEW


# ── evaluate_approval_matrix ─────────────────────────────────────────────────

def _make_proposal(complexity=1, risk=1, tags=None, irreversible=False, self_escalation=None, target="control-plane"):
    return BundleProposal(
        complexity_score=complexity,
        risk_score=risk,
        tags=tags or [],
        irreversible=irreversible,
        self_escalation_tier=self_escalation,
        target=target,
    )


def _make_triggers():
    return [
        MandatoryReviewTrigger(name="new_repo", target_new_repo=True),
        MandatoryReviewTrigger(name="sensitive_tags", tag_matches=["auth", "billing"]),
    ]


class TestEvaluateApprovalMatrix:
    """Test the full evaluator against the spec pseudocode."""

    def test_score_driven_auto(self):
        decision = evaluate_approval_matrix(
            _make_proposal(2, 1), {}, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.AUTO
        assert decision.auto_ship is True

    def test_score_driven_full_review(self):
        decision = evaluate_approval_matrix(
            _make_proposal(7, 7), {}, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW_COOLDOWN
        assert decision.auto_ship is False

    def test_mandatory_trigger_override(self):
        """new-repo target forces full review regardless of scores."""
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1, target="new-repo"),
            {}, _make_triggers(), [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW
        assert decision.auto_ship is False
        assert "mandatory review trigger" in decision.reason

    def test_critical_security_finding(self):
        """A critical security finding escalates to FULL_REVIEW_COOLDOWN."""
        findings = {"security": [{"severity": "critical", "status": "unresolved"}]}
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1), findings, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW_COOLDOWN
        assert decision.auto_ship is False
        assert "critical security" in decision.reason

    def test_unresolved_medium_security(self):
        """Unresolved medium+ security finding escalates to FULL_REVIEW."""
        findings = {"security": [{"severity": "medium", "status": "unresolved"}]}
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1), findings, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW
        assert decision.auto_ship is False
        assert "unresolved security" in decision.reason

    def test_resolved_medium_security_does_not_escalate(self):
        """A resolved medium finding does not trigger escalation."""
        findings = {"security": [{"severity": "medium", "status": "resolved"}]}
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1), findings, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.AUTO
        assert decision.auto_ship is True

    def test_sensitive_tag_forces_full_review(self):
        """Tags like auth, billing force FULL_REVIEW."""
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1, tags=["auth"]), {}, [], ["auth"], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW
        assert "sensitive surface" in decision.reason

    def test_self_escalation_overrides_upward(self):
        """Bundler can self-escalate to a higher tier."""
        proposal = _make_proposal(1, 1, self_escalation="full_review")
        decision = evaluate_approval_matrix(
            proposal, {}, [], [],
            self_escalation_tier=proposal.self_escalation_tier,
            settings=ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW

    def test_self_escalation_cannot_de_escalate(self):
        """Bundler cannot self-de-escalate below the score-driven tier."""
        proposal = _make_proposal(7, 8, self_escalation="auto")
        decision = evaluate_approval_matrix(
            proposal, {}, [], [],
            self_escalation_tier=proposal.self_escalation_tier,
            settings=ApprovalSettings(),
        )
        # Stays at FULL_REVIEW_COOLDOWN (score-driven) despite self_escalation="auto"
        assert decision.tier == ApprovalTier.FULL_REVIEW_COOLDOWN

    def test_multiple_triggers_only_first_wins(self):
        """When multiple triggers match, reason reflects the first one."""
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1, target="new-repo", tags=["auth"]),
            {}, _make_triggers(), ["auth"], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.FULL_REVIEW
        assert "mandatory review trigger" in decision.reason

    def test_no_security_findings_preserves_score_tier(self):
        """Empty security findings preserve the score-driven tier."""
        findings = {"security": []}
        decision = evaluate_approval_matrix(
            _make_proposal(1, 1), findings, [], [], None, ApprovalSettings(),
        )
        assert decision.tier == ApprovalTier.AUTO
        assert decision.auto_ship is True


# ── cooldown_seconds ─────────────────────────────────────────────────────────

class TestCooldownSeconds:
    def test_reversible_default(self):
        assert cooldown_seconds(reversible=True) == 3600  # 1 hour

    def test_irreversible_default(self):
        assert cooldown_seconds(reversible=False) == 86400  # 24 hours

    def test_custom_durations(self):
        assert cooldown_seconds(True, cooldown_hours_reversible=2) == 7200
        assert cooldown_seconds(False, cooldown_hours_irreversible=48) == 172800


# ── CooldownError ────────────────────────────────────────────────────────────

class TestCooldownError:
    def test_error_contains_bundle_id_and_until(self):
        exc = CooldownError("b1", 1700000000)
        assert exc.bundle_id == "b1"
        assert exc.cooldown_until == 1700000000
        assert "b1" in str(exc)
        assert "1700000000" in str(exc)


# ── MandatoryReviewTrigger ───────────────────────────────────────────────────

class TestMandatoryReviewTrigger:
    def test_target_new_repo_matches(self):
        trigger = MandatoryReviewTrigger(name="new_repo", target_new_repo=True)
        proposal = _make_proposal(target="new-repo")
        assert trigger.matches(proposal) is True

    def test_target_new_repo_no_match(self):
        trigger = MandatoryReviewTrigger(name="new_repo", target_new_repo=True)
        proposal = _make_proposal(target="control-plane")
        assert trigger.matches(proposal) is False

    def test_tag_matches(self):
        trigger = MandatoryReviewTrigger(name="pii_check", tag_matches=["pii", "secrets"])
        assert trigger.matches(None, bundle_tags=["pii"]) is True

    def test_tag_no_match(self):
        trigger = MandatoryReviewTrigger(name="pii_check", tag_matches=["pii"])
        assert trigger.matches(None, bundle_tags=["auth"]) is False

    def test_no_conditions_no_match(self):
        trigger = MandatoryReviewTrigger(name="empty")
        assert trigger.matches(None, bundle_tags=[]) is False


# ── ApprovalSettings ─────────────────────────────────────────────────────────

class TestApprovalSettings:
    def test_default_values(self):
        settings = ApprovalSettings()
        assert settings.cooldown_hours_reversible == 1
        assert settings.cooldown_hours_irreversible == 24
        assert settings.low_complexity_max == 3
        assert settings.med_complexity_max == 6
        assert settings.summary_timeout_hours == 4


# ── ApprovalTier enum ────────────────────────────────────────────────────────

class TestApprovalTier:
    def test_all_tiers_exist(self):
        assert len(ApprovalTier) == 5
        assert ApprovalTier.AUTO == "auto"
        assert ApprovalTier.AUTO_NOTIFY == "auto_notify"
        assert ApprovalTier.SUMMARY == "summary"
        assert ApprovalTier.FULL_REVIEW == "full_review"
        assert ApprovalTier.FULL_REVIEW_COOLDOWN == "full_review_cooldown"
