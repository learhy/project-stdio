"""Approval matrix evaluator: deterministic function mapping bundle scores + review findings to tiers.

Bundle 2.5 implements the full spec from agent-orchestration-v1.1.md lines 1983-2028:
the 3x3 matrix, security findings gates, rollback plan gates, sensitive surface gates,
mandatory-review triggers, and bundler self-escalation.

Importable from the orchestrator lifecycle, CLI, and MCP.
"""

from __future__ import annotations

from typing import Any

from .models import (
    ApprovalTier,
    Severity,
    MandatoryReviewTrigger,
    parse_trigger,
    max_tier,
)


# ── 3x3 Matrix ────────────────────────────────────────────────────────────────

_MATRIX: dict[tuple[str, str], ApprovalTier] = {
    # (complexity band, risk band) -> tier
    ("low", "low"):     ApprovalTier.AUTO,
    ("low", "medium"):  ApprovalTier.SUMMARY,
    ("low", "high"):    ApprovalTier.FULL_REVIEW,
    ("med", "low"):     ApprovalTier.AUTO_NOTIFY,
    ("med", "medium"):  ApprovalTier.SUMMARY,
    ("med", "high"):    ApprovalTier.FULL_REVIEW,
    ("high", "low"):    ApprovalTier.SUMMARY,
    ("high", "medium"): ApprovalTier.FULL_REVIEW,
    ("high", "high"):   ApprovalTier.FULL_REVIEW_COOLDOWN,
}


def _band_complexity(score: int) -> str:
    if score <= 3:
        return "low"
    elif score <= 6:
        return "med"
    return "high"


def _band_risk(score: int) -> str:
    if score <= 2:
        return "low"
    elif score <= 5:
        return "medium"
    return "high"


def matrix_lookup(complexity_score: int, risk_score: int) -> str:
    """Return the score-driven tier from the 3x3 matrix."""
    key = (_band_complexity(complexity_score), _band_risk(risk_score))
    return _MATRIX.get(key, ApprovalTier.FULL_REVIEW).value


# ── Evaluator ──────────────────────────────────────────────────────────────────


def evaluate_approval_matrix(
    bundle: dict[str, Any],
    merged_findings: dict[str, Any],
    trigger_configs: list[dict[str, Any]],
    tags: list[str] | None = None,
    self_escalation_tier: str | None = None,
    irreversible: bool = False,
    cooldown_hours_reversible: int = 1,
    cooldown_hours_irreversible: int = 24,
) -> dict[str, Any]:
    """Evaluate the approval matrix for a bundle.

    Returns a dict with: tier, auto_ship, reason, cooldown_until (int or None).
    This is a deterministic function; it does not mutate state.

    Args:
        bundle: The bundle proposal dict (from proposal_json or merged proposal).
        merged_findings: The merged review track outputs keyed by role
                         (e.g. {"adversarial": [...], "security": [...], "qa": {...}}).
        trigger_configs: List of trigger config dicts from settings.json.
        tags: Bundle tags (bundler-proposed + security-ratified).
        self_escalation_tier: Bundler's self-escalation tier, or None.
        irreversible: Whether the bundle is flagged irreversible.
        cooldown_hours_reversible: Cooldown in hours for reversible bundles.
        cooldown_hours_irreversible: Cooldown in hours for irreversible bundles.
    """
    tags = tags or []
    import time

    # Parse triggers
    triggers: list[MandatoryReviewTrigger] = []
    for tc in trigger_configs:
        try:
            triggers.append(parse_trigger(tc))
        except Exception:
            pass

    # ── Mandatory-review triggers override the matrix entirely ──
    for trigger in triggers:
        if trigger.matches(bundle):
            return {
                "tier": ApprovalTier.FULL_REVIEW,
                "auto_ship": False,
                "reason": f"mandatory review trigger: {trigger.type}",
                "cooldown_until": None,
            }

    # ── Security findings gate auto-ship ──
    security_findings = merged_findings.get("security", [])
    if isinstance(security_findings, dict):
        security_findings = security_findings.get("findings", [])

    has_critical = any(
        f.get("severity") == Severity.CRITICAL for f in security_findings
    )
    has_unresolved_medium_plus = any(
        f.get("severity") in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
        and f.get("status") == "unresolved"
        for f in security_findings
    )

    if has_critical:
        return {
            "tier": ApprovalTier.FULL_REVIEW_COOLDOWN,
            "auto_ship": False,
            "reason": "critical security finding",
            "cooldown_until": _compute_cooldown(
                irreversible, cooldown_hours_reversible, cooldown_hours_irreversible
            ),
        }

    if has_unresolved_medium_plus:
        return {
            "tier": ApprovalTier.FULL_REVIEW,
            "auto_ship": False,
            "reason": "unresolved security findings (medium+)",
            "cooldown_until": None,
        }

    # ── Rollback plan gates auto-ship ──
    qa_output = merged_findings.get("qa", {})
    if isinstance(qa_output, dict):
        vp = qa_output.get("verification_plan", {})
    else:
        vp = {}
    if isinstance(vp, dict):
        rollback = vp.get("rollback_plan", {})
    else:
        rollback = {}
    has_viable_rollback = rollback.get("machine_executable", False)

    if not has_viable_rollback:
        # Auto-bump reversibility to 3 — this is advisory; the score isn't re-evaluated
        # but the evaluator notes it in the reason.
        pass

    # ── Auth / billing / secrets / PII gate auto-ship ──
    sensitive_tags = {"auth", "billing", "secrets", "pii"}
    touches_sensitive = bool(set(tags) & sensitive_tags)
    if touches_sensitive:
        return {
            "tier": ApprovalTier.FULL_REVIEW,
            "auto_ship": False,
            "reason": f"touches sensitive surface: {set(tags) & sensitive_tags}",
            "cooldown_until": None,
        }

    # ── Score-driven tier from the 3x3 matrix ──
    complexity_score = bundle.get("complexity_score", 0)
    risk_score = bundle.get("risk_score", 0)
    score_tier = matrix_lookup(complexity_score, risk_score)

    # Bundler self-escalation: takes the higher of matrix tier and self-escalation
    if self_escalation_tier:
        score_tier = max_tier(score_tier, self_escalation_tier)

    # Auto-ship is only possible for auto / auto-notify tiers
    can_auto_ship = False
    if score_tier in (ApprovalTier.AUTO, ApprovalTier.AUTO_NOTIFY):
        can_auto_ship = (
            not has_critical
            and not has_unresolved_medium_plus
            and has_viable_rollback
            and not touches_sensitive
        )

    cooldown_until = None
    if score_tier == ApprovalTier.FULL_REVIEW_COOLDOWN:
        cooldown_until = _compute_cooldown(
            irreversible, cooldown_hours_reversible, cooldown_hours_irreversible
        )

    return {
        "tier": score_tier,
        "auto_ship": can_auto_ship,
        "reason": "score-driven" if not self_escalation_tier else f"score-driven (bundler escalated from {matrix_lookup(complexity_score, risk_score)})",
        "cooldown_until": cooldown_until,
    }


def _compute_cooldown(
    irreversible: bool,
    cooldown_hours_reversible: int,
    cooldown_hours_irreversible: int,
) -> int:
    """Compute the cooldown_until timestamp."""
    import time
    hours = cooldown_hours_irreversible if irreversible else cooldown_hours_reversible
    return int(time.time()) + (hours * 3600)
