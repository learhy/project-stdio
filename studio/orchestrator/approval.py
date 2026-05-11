"""Approval matrix evaluator — deterministic, auditable, score-driven.

The evaluator reads the bundle's complexity and risk scores, review track
findings, and the mandatory-review trigger list. It produces a tier decision
and an auto-ship eligibility boolean. It can escalate but never de-escalate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .models import ApprovalTier

if TYPE_CHECKING:
    from .models import BundleProposal

logger = logging.getLogger(__name__)

# ── Band thresholds ──────────────────────────────────────────────────────────

LOW_COMPLEXITY_MAX = 3
MED_COMPLEXITY_MAX = 6
LOW_RISK_MAX = 2
MED_RISK_MAX = 5

# ── 3×3 matrix: (complexity_band, risk_band) → ApprovalTier ──────────────────

_MATRIX: dict[tuple[str, str], ApprovalTier] = {
    ("low", "low"): ApprovalTier.AUTO,
    ("med", "low"): ApprovalTier.AUTO_NOTIFY,
    ("high", "low"): ApprovalTier.SUMMARY,
    ("low", "med"): ApprovalTier.SUMMARY,
    ("med", "med"): ApprovalTier.SUMMARY,
    ("high", "med"): ApprovalTier.FULL_REVIEW,
    ("low", "high"): ApprovalTier.FULL_REVIEW,
    ("med", "high"): ApprovalTier.FULL_REVIEW,
    ("high", "high"): ApprovalTier.FULL_REVIEW_COOLDOWN,
}

SENSITIVE_TAGS = frozenset({"auth", "billing", "secrets", "pii"})


def _band(score: int, low_max: int, med_max: int) -> str:
    if score <= low_max:
        return "low"
    elif score <= med_max:
        return "med"
    return "high"


def matrix_lookup(complexity_score: int, risk_score: int) -> ApprovalTier:
    """Return the score-driven tier from the 3×3 approval matrix."""
    c_band = _band(complexity_score, LOW_COMPLEXITY_MAX, MED_COMPLEXITY_MAX)
    r_band = _band(risk_score, LOW_RISK_MAX, MED_RISK_MAX)
    return _MATRIX[(c_band, r_band)]


# ── Cooldown ─────────────────────────────────────────────────────────────────

class CooldownError(Exception):
    """Raised when approval is attempted on a bundle still in cooldown."""

    def __init__(self, bundle_id: str, cooldown_until: int) -> None:
        super().__init__(
            f"Bundle {bundle_id} is in cooldown until {cooldown_until}"
        )
        self.bundle_id = bundle_id
        self.cooldown_until = cooldown_until


def cooldown_seconds(reversible: bool, cooldown_hours_reversible: int = 1, cooldown_hours_irreversible: int = 24) -> int:
    """Return the cooldown duration in seconds for the given reversibility."""
    hours = cooldown_hours_irreversible if not reversible else cooldown_hours_reversible
    return hours * 3600


# ── Mandatory-review trigger ─────────────────────────────────────────────────

@dataclass
class MandatoryReviewTrigger:
    """A condition that forces full review regardless of scores.

    Attributes:
        name: Human-readable label for audit / settings display.
        description: What this trigger catches and why.
        path_patterns: Glob patterns matched against changed file paths.
        tag_matches: If the bundle has any of these tags, trigger fires.
        min_files_deleted: Trigger if more than this many files are deleted.
        target_new_repo: Trigger if target is 'new-repo'.
    """

    name: str
    description: str = ""
    path_patterns: list[str] = field(default_factory=list)
    tag_matches: list[str] = field(default_factory=list)
    min_files_deleted: int | None = None
    target_new_repo: bool = False

    def matches(self, proposal: "BundleProposal | None", bundle_tags: list[str] | None = None) -> bool:
        """Return True if this trigger matches the given bundle."""
        tags = bundle_tags or []

        if self.target_new_repo and proposal is not None and proposal.target == "new-repo":
            return True

        if self.tag_matches:
            if any(t in self.tag_matches for t in tags):
                return True

        # Path-pattern matching and file-deletion thresholds require the
        # actual changed-files list, which isn't available at matrix-eval
        # time in v1.1. The trigger definitions are stored so the reviewer
        # surface can flag bundles that match, and the execution layer can
        # re-check after the DAG completes.
        return False


# ── Approval decision ────────────────────────────────────────────────────────

@dataclass
class ApprovalDecision:
    tier: ApprovalTier
    auto_ship: bool
    reason: str


# ── Evaluator ────────────────────────────────────────────────────────────────

def evaluate_approval_matrix(
    proposal: "BundleProposal",
    findings: dict,
    triggers: list[MandatoryReviewTrigger],
    bundle_tags: list[str] | None = None,
    self_escalation_tier: str | None = None,
    settings: "ApprovalSettings | None" = None,
) -> ApprovalDecision:
    """Evaluate the approval matrix for a bundle.

    Args:
        proposal: The bundler's proposal with complexity/risk scores.
        findings: Aggregated review track outputs keyed by role name
                  (e.g. {'security': [...], 'adversarial': [...], 'qa': {...}}).
        triggers: Configured mandatory-review triggers from settings.
        bundle_tags: Tags applied to the bundle (for sensitive-surface detection).
        self_escalation_tier: If the bundler self-escalated, the requested tier.
        settings: Approval settings (band thresholds, cooldown durations).

    Returns:
        ApprovalDecision with the resolved tier, auto_ship flag, and reason.
    """
    tags = bundle_tags or []

    # Mandatory-review triggers override the matrix entirely
    if any(trigger.matches(proposal, tags) for trigger in triggers):
        return ApprovalDecision(
            tier=ApprovalTier.FULL_REVIEW,
            auto_ship=False,
            reason="mandatory review trigger",
        )

    # Security findings gate auto-ship
    security = findings.get("security", [])
    has_critical = any(
        _get_severity(f) == "critical" for f in security
    )
    has_unresolved_medium_plus = any(
        _get_severity(f) in ("medium", "high", "critical")
        and _get_status(f) == "unresolved"
        for f in security
    )

    if has_critical:
        return ApprovalDecision(
            tier=ApprovalTier.FULL_REVIEW_COOLDOWN,
            auto_ship=False,
            reason="critical security finding",
        )
    if has_unresolved_medium_plus:
        return ApprovalDecision(
            tier=ApprovalTier.FULL_REVIEW,
            auto_ship=False,
            reason="unresolved security findings",
        )

    # Auth / billing / secrets / PII gate auto-ship
    touches_sensitive = any(tag in SENSITIVE_TAGS for tag in tags)
    if touches_sensitive:
        return ApprovalDecision(
            tier=ApprovalTier.FULL_REVIEW,
            auto_ship=False,
            reason="touches sensitive surface",
        )

    # Score-driven tier from the 3×3 matrix
    tier = matrix_lookup(proposal.complexity_score, proposal.risk_score)

    # Self-escalation: bundler can escalate but never de-escalate
    if self_escalation_tier is not None:
        escalated = _parse_tier(self_escalation_tier)
        if escalated is not None and _tier_rank(escalated) > _tier_rank(tier):
            tier = escalated

    # Auto-ship is disabled for anything above auto or auto-notify
    can_auto_ship = (
        tier in (ApprovalTier.AUTO, ApprovalTier.AUTO_NOTIFY)
        and not has_unresolved_medium_plus
        and not touches_sensitive
    )

    return ApprovalDecision(tier=tier, auto_ship=can_auto_ship, reason="score-driven")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_severity(finding: dict | object) -> str:
    if isinstance(finding, dict):
        return finding.get("severity", "")
    return getattr(finding, "severity", "")


def _get_status(finding: dict | object) -> str:
    if isinstance(finding, dict):
        return finding.get("status", "")
    return getattr(finding, "status", "")


def _tier_rank(tier: ApprovalTier) -> int:
    """Return an ordinal rank so tiers can be compared (higher = stricter)."""
    _order = {
        ApprovalTier.AUTO: 0,
        ApprovalTier.AUTO_NOTIFY: 1,
        ApprovalTier.SUMMARY: 2,
        ApprovalTier.FULL_REVIEW: 3,
        ApprovalTier.FULL_REVIEW_COOLDOWN: 4,
    }
    return _order.get(tier, 0)


def _parse_tier(raw: str) -> ApprovalTier | None:
    """Parse a tier string into an ApprovalTier, returning None on failure."""
    try:
        return ApprovalTier(raw)
    except ValueError:
        return None


# ── Settings model (imported lazily to avoid circular imports) ────────────────
# ApprovalSettings is defined in models.py. The reference here is used only in
# type hints via TYPE_CHECKING.
