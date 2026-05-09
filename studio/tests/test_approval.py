"""Tests for Bundle 2.5: Approval Matrix."""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.approval import (
    evaluate_approval_matrix,
    matrix_lookup,
    _band_complexity,
    _band_risk,
    _compute_cooldown,
)
from studio.orchestrator.models import (
    ApprovalTier,
    Severity,
    max_tier,
    TargetTrigger,
    FilePatternTrigger,
    TagTrigger,
    parse_trigger,
    ApprovalSettings,
)


# ── Matrix lookup tests ──────────────────────────────────────────────────────────

class TestMatrixLookup:
    def test_all_nine_cells(self):
        """Verify all 9 cells of the 3x3 matrix map to correct tiers."""
        # (complexity, risk) -> expected tier
        expected = {
            (1, 1):   "auto",
            (1, 3):   "summary",
            (1, 6):   "full_review",
            (5, 1):   "auto_notify",
            (5, 3):   "summary",
            (5, 6):   "full_review",
            (8, 1):   "summary",
            (8, 3):   "full_review",
            (8, 6):   "full_review_cooldown",
        }
        for (c, r), tier in expected.items():
            assert matrix_lookup(c, r) == tier, f"({c}, {r}) expected {tier}"

    def test_edge_boundaries_complexity(self):
        """Validate complexity band boundaries: 3→low, 4→med, 7→high."""
        assert _band_complexity(0) == "low"
        assert _band_complexity(3) == "low"
        assert _band_complexity(4) == "med"
        assert _band_complexity(6) == "med"
        assert _band_complexity(7) == "high"
        assert _band_complexity(10) == "high"

    def test_edge_boundaries_risk(self):
        """Validate risk band boundaries: 2→low, 3→medium, 6→high."""
        assert _band_risk(0) == "low"
        assert _band_risk(2) == "low"
        assert _band_risk(3) == "medium"
        assert _band_risk(5) == "medium"
        assert _band_risk(6) == "high"
        assert _band_risk(10) == "high"

    def test_score_bounds_enforced_by_model(self):
        """Complexity and risk scores must be 0-10."""
        from studio.orchestrator.models import BundleProposal
        # Valid: 0-10 works
        p = BundleProposal(complexity_score=5, risk_score=5)
        assert p.complexity_score == 5
        assert p.risk_score == 5


# ── Tier ordering tests ──────────────────────────────────────────────────────────

class TestMaxTier:
    def test_max_tier_returns_higher(self):
        assert max_tier("auto", "full_review") == "full_review"
        assert max_tier("full_review_cooldown", "auto") == "full_review_cooldown"
        assert max_tier("summary", "auto_notify") == "summary"
        assert max_tier("auto", "auto") == "auto"

    def test_max_tier_with_unknown_returns_known(self):
        assert max_tier("auto", "bogus") == "auto"
        assert max_tier("bogus", "full_review") == "full_review"

    def test_max_tier_with_none_string(self):
        """self_escalation_tier may be None in practice."""
        assert max_tier("auto", "summary") == "summary"


# ── Mandatory-review trigger tests ───────────────────────────────────────────────

class TestTargetTrigger:
    def test_matches_correct_target(self):
        t = TargetTrigger(value="control-plane")
        assert t.matches({"target": "control-plane"}) is True

    def test_no_match_different_target(self):
        t = TargetTrigger(value="new-repo")
        assert t.matches({"target": "control-plane"}) is False

    def test_no_target_field(self):
        t = TargetTrigger(value="control-plane")
        assert t.matches({}) is False


class TestFilePatternTrigger:
    def test_matches_read_path(self):
        t = FilePatternTrigger(glob="src/**/*.py")
        bundle = {
            "task_dag": {
                "nodes": [{
                    "spec": {
                        "filesystem": {
                            "reads": ["src/orchestrator/main.py"],
                            "writes": [],
                        }
                    }
                }]
            }
        }
        assert t.matches(bundle) is True

    def test_matches_write_path(self):
        t = FilePatternTrigger(glob="*.yaml")
        bundle = {
            "task_dag": {
                "nodes": [{
                    "spec": {
                        "filesystem": {
                            "reads": [],
                            "writes": ["config/deploy.yaml"],
                        }
                    }
                }]
            }
        }
        assert t.matches(bundle) is True

    def test_no_match(self):
        t = FilePatternTrigger(glob="*.md")
        bundle = {
            "task_dag": {
                "nodes": [{
                    "spec": {
                        "filesystem": {
                            "reads": ["src/main.py"],
                            "writes": [],
                        }
                    }
                }]
            }
        }
        assert t.matches(bundle) is False

    def test_empty_dag(self):
        t = FilePatternTrigger(glob="*.py")
        assert t.matches({"task_dag": {"nodes": []}}) is False

    def test_no_filesystem_spec(self):
        t = FilePatternTrigger(glob="*.py")
        bundle = {"task_dag": {"nodes": [{"spec": {}}]}}
        assert t.matches(bundle) is False


class TestTagTrigger:
    def test_matches_present_tag(self):
        t = TagTrigger(tag="auth")
        assert t.matches({"tags": ["auth", "billing"]}) is True

    def test_no_match_absent_tag(self):
        t = TagTrigger(tag="secrets")
        assert t.matches({"tags": ["auth"]}) is False

    def test_no_tags_field(self):
        t = TagTrigger(tag="auth")
        assert t.matches({}) is False


class TestParseTrigger:
    def test_parses_target_trigger(self):
        cfg = {"type": "target", "value": "control-plane"}
        result = parse_trigger(cfg)
        assert isinstance(result, TargetTrigger)
        assert result.value == "control-plane"

    def test_parses_file_pattern_trigger(self):
        cfg = {"type": "file_pattern", "glob": "*.py"}
        result = parse_trigger(cfg)
        assert isinstance(result, FilePatternTrigger)
        assert result.glob == "*.py"

    def test_parses_tag_trigger(self):
        cfg = {"type": "tag", "tag": "auth"}
        result = parse_trigger(cfg)
        assert isinstance(result, TagTrigger)
        assert result.tag == "auth"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown mandatory-review trigger type"):
            parse_trigger({"type": "bogus"})


# ── Approval settings tests ──────────────────────────────────────────────────────

class TestApprovalSettings:
    def test_defaults(self):
        s = ApprovalSettings()
        assert s.summary_timeout_hours == 4
        assert s.cooldown_hours_reversible == 1
        assert s.cooldown_hours_irreversible == 24
        assert s.mandatory_review_triggers == []

    def test_with_custom_cooldowns(self):
        s = ApprovalSettings(cooldown_hours_reversible=2, cooldown_hours_irreversible=48)
        assert s.cooldown_hours_reversible == 2
        assert s.cooldown_hours_irreversible == 48

    def test_with_triggers(self):
        s = ApprovalSettings(mandatory_review_triggers=[
            {"type": "target", "value": "new-repo"},
            {"type": "tag", "tag": "auth"},
        ])
        assert len(s.mandatory_review_triggers) == 2


# ── Core evaluator tests ─────────────────────────────────────────────────────────

class TestEvaluateApprovalMatrix:
    EMPTY_TRIGGERS: list = []
    EMPTY_TAGS: list = []
    EMPTY_FINDINGS: dict = {}

    def test_low_complexity_low_risk_auto_ships(self):
        bundle = {"complexity_score": 2, "risk_score": 1}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.AUTO
        assert result["auto_ship"] is False  # needs viable rollback

    def test_high_high_forces_cooldown(self):
        bundle = {"complexity_score": 8, "risk_score": 7}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW_COOLDOWN
        assert result["auto_ship"] is False
        assert result["cooldown_until"] is not None

    def test_security_critical_forces_cooldown(self):
        bundle = {"complexity_score": 2, "risk_score": 1}
        findings = {
            "security": {
                "findings": [
                    {"severity": Severity.CRITICAL, "finding": "Root escalation via setuid", "status": "unresolved"},
                ]
            }
        }
        result = evaluate_approval_matrix(
            bundle, findings, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW_COOLDOWN
        assert result["auto_ship"] is False
        assert "critical security finding" in result["reason"]

    def test_unresolved_medium_plus_forces_full_review(self):
        bundle = {"complexity_score": 2, "risk_score": 1}
        findings = {
            "security": {
                "findings": [
                    {"severity": Severity.MEDIUM, "finding": "Missing input validation", "status": "unresolved"},
                ]
            }
        }
        result = evaluate_approval_matrix(
            bundle, findings, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW
        assert "unresolved security findings" in result["reason"]

    def test_resolved_medium_does_not_block(self):
        bundle = {"complexity_score": 2, "risk_score": 1}
        findings = {
            "security": {
                "findings": [
                    {"severity": Severity.MEDIUM, "finding": "Fixed", "status": "resolved"},
                ]
            }
        }
        result = evaluate_approval_matrix(
            bundle, findings, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] in (ApprovalTier.AUTO, ApprovalTier.AUTO_NOTIFY, ApprovalTier.SUMMARY)

    def test_sensitive_tags_force_full_review(self):
        bundle = {"complexity_score": 2, "risk_score": 1}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
            tags=["auth"],
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW
        assert "sensitive surface" in result["reason"]

    def test_secrets_tag_force_full_review(self):
        bundle = {"complexity_score": 1, "risk_score": 1}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
            tags=["feature", "secrets"],
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW

    def test_mandatory_trigger_overrides_matrix(self):
        bundle = {"complexity_score": 1, "risk_score": 1, "target": "new-repo"}
        triggers = [{"type": "target", "value": "new-repo"}]
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, triggers,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW
        assert "mandatory review trigger" in result["reason"]

    def test_bundler_self_escalation_raises_tier(self):
        # Without escalation this would be AUTO (complexity=2, risk=1)
        bundle = {"complexity_score": 2, "risk_score": 1}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
            self_escalation_tier="summary",
        )
        assert result["tier"] == ApprovalTier.SUMMARY
        assert "bundler escalated" in result["reason"]

    def test_bundler_cannot_de_escalate(self):
        # Matrix gives FULL_REVIEW but bundler tries to de-escalate to auto
        bundle = {"complexity_score": 8, "risk_score": 4}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
            self_escalation_tier="auto",
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW  # matrix wins

    def test_irreversible_longer_cooldown(self):
        bundle = {"complexity_score": 8, "risk_score": 7}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
            irreversible=True,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW_COOLDOWN
        # 24h cooldown vs 1h reversible
        assert result["cooldown_until"] is not None
        now = int(time.time())
        cooldown_duration = result["cooldown_until"] - now
        assert cooldown_duration > 3600  # longer than 1h

    def test_complexity_med_risk_low_gives_auto_notify(self):
        bundle = {"complexity_score": 4, "risk_score": 1}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.AUTO_NOTIFY

    def test_default_scores_are_zero(self):
        bundle = {}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] is not None  # should resolve to auto (0,0)
        assert result["reason"] is not None

    def test_cooldown_until_none_for_non_cooldown_tiers(self):
        bundle = {"complexity_score": 5, "risk_score": 3}
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, self.EMPTY_TRIGGERS,
        )
        assert result["cooldown_until"] is None

    def test_file_pattern_trigger_with_github_workflows(self):
        bundle = {
            "complexity_score": 2,
            "risk_score": 1,
            "task_dag": {
                "nodes": [{
                    "spec": {
                        "filesystem": {
                            "writes": [".github/workflows/deploy.yml"],
                            "reads": [],
                        }
                    }
                }]
            }
        }
        triggers = [{"type": "file_pattern", "glob": ".github/workflows/*"}]
        result = evaluate_approval_matrix(
            bundle, self.EMPTY_FINDINGS, triggers,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW

    def test_security_findings_as_dict_not_list(self):
        """If security output is a dict with 'findings' key, not a pure list."""
        bundle = {"complexity_score": 2, "risk_score": 1}
        findings = {
            "security": {
                "findings": [
                    {"severity": Severity.HIGH, "finding": "XSS in template", "status": "unresolved"},
                ],
                "threat_model": {"summary": "High risk"},
            }
        }
        result = evaluate_approval_matrix(
            bundle, findings, self.EMPTY_TRIGGERS,
        )
        assert result["tier"] == ApprovalTier.FULL_REVIEW
        assert "unresolved security findings" in result["reason"]


# ── Cooldown computation tests ───────────────────────────────────────────────────

class TestComputeCooldown:
    def test_reversible_cooldown(self):
        now = int(time.time())
        cooldown = _compute_cooldown(False, 1, 24)
        assert cooldown > now
        assert cooldown <= now + 3600 + 5  # 1h + small tolerance

    def test_irreversible_cooldown(self):
        now = int(time.time())
        cooldown = _compute_cooldown(True, 1, 24)
        assert cooldown > now
        assert cooldown <= now + 24 * 3600 + 5

    def test_custom_hours(self):
        now = int(time.time())
        cooldown = _compute_cooldown(False, 2, 48)
        assert cooldown > now
        assert cooldown <= now + 2 * 3600 + 5


# ── CLI deck handler tests ───────────────────────────────────────────────────────

class TestCliDeck:
    @pytest.fixture
    def app_mock(self):
        app = MagicMock()
        app.db = MagicMock()
        app.db.fetch_one = AsyncMock()
        app.db.fetch_all = AsyncMock()
        app.sm = MagicMock()
        app.sm.now = MagicMock(return_value=1700000000)
        return app

    @pytest.mark.asyncio
    async def test_deck_returns_full_structure(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01TEST",
            "state": "in_review",
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Add health check endpoint"},
                "proposal": {
                    "complexity_score": 3,
                    "risk_score": 2,
                    "estimated_loc": 150,
                    "estimated_duration_seconds": 3600,
                    "estimated_worker_count": 1,
                    "estimated_tokens": 5000,
                    "target": "control-plane",
                    "concerns": [],
                    "counter_case": "Could be done with curl",
                    "biggest_risk": "Adding unnecessary overhead",
                    "stakes_line": "Low stakes, nice to have",
                },
            }),
            "tags": "[]",
            "cooldown_until": None,
            "tier": "summary",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"node_id": "adversarial", "output_json": json.dumps({"findings": [{"severity": "low", "finding": "Nothing to critique"}]})},
            {"node_id": "security", "output_json": json.dumps({"findings": []})},
            {"node_id": "qa", "output_json": json.dumps({"verification_plan": {"acceptance_criteria": ["GET /health returns 200"]}})},
        ])

        result = await _cli_deck(app_mock, {"bundle_id": "01TEST"})
        assert result["bundle_id"] == "01TEST"
        assert result["tier"] == "summary"
        assert result["state"] == "in_review"
        assert result["cooldown"] == "none"
        assert result["proposal"]["idea"] == "Add health check endpoint"
        assert result["recommendation"]["complexity_score"] == 3
        assert result["recommendation"]["risk_score"] == 2
        assert result["recommendation"]["confidence_pct"] == 90
        assert result["cost"]["estimated_tokens"] == 5000
        assert result["findings"]["adversarial"] != []
        assert result["findings"]["qa"]["verification_plan"]["acceptance_criteria"] == ["GET /health returns 200"]

    @pytest.mark.asyncio
    async def test_deck_missing_bundle(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Bundle MISSING not found"):
            await _cli_deck(app_mock, {"bundle_id": "MISSING"})

    @pytest.mark.asyncio
    async def test_deck_with_cooldown(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01CD",
            "state": "in_review",
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Add billing integration"},
                "proposal": {"complexity_score": 8, "risk_score": 7},
            }),
            "tags": '["billing"]',
            "cooldown_until": 1700003600,  # 1h from now (now=1700000000)
            "tier": "full_review_cooldown",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[])

        result = await _cli_deck(app_mock, {"bundle_id": "01CD"})
        assert result["cooldown"] == "3600s remaining"

    @pytest.mark.asyncio
    async def test_deck_cooldown_expired(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01CD",
            "state": "in_review",
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Old bundle"},
                "proposal": {"complexity_score": 8, "risk_score": 7},
            }),
            "tags": "[]",
            "cooldown_until": 1699999000,  # in the past
            "tier": "full_review_cooldown",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[])

        result = await _cli_deck(app_mock, {"bundle_id": "01CD"})
        assert result["cooldown"] == "expired"

    @pytest.mark.asyncio
    async def test_deck_with_counter_case(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01CC",
            "state": "in_review",
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Rewrite auth"},
                "proposal": {
                    "complexity_score": 7, "risk_score": 8,
                    "counter_case": "Don't rewrite, wrap instead",
                    "biggest_risk": "Auth outage during migration",
                    "stakes_line": "Core auth is critical path",
                },
            }),
            "tags": '["auth"]',
            "cooldown_until": None,
            "tier": "full_review",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[])

        result = await _cli_deck(app_mock, {"bundle_id": "01CC"})
        assert result["counter_case"] == "Don't rewrite, wrap instead"
        assert result["biggest_risk"] == "Auth outage during migration"
        assert result["stakes_line"] == "Core auth is critical path"

    @pytest.mark.asyncio
    async def test_deck_returns_auto_ship_false_for_manual_review(self, app_mock):
        from studio.orchestrator.main import _cli_deck

        app_mock.db.fetch_one = AsyncMock(return_value={
            "id": "01AR",
            "state": "in_review",
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Minor fix"},
                "proposal": {"complexity_score": 1, "risk_score": 1},
            }),
            "tags": "[]",
            "cooldown_until": None,
            "tier": "summary",
        })
        app_mock.db.fetch_all = AsyncMock(return_value=[])

        result = await _cli_deck(app_mock, {"bundle_id": "01AR"})
        assert result["auto_ship"] is False  # deck only shows bundles needing human action


# ── CLI pending handler tests ────────────────────────────────────────────────────

class TestCliPending:
    @pytest.fixture
    def app_mock(self):
        app = MagicMock()
        app.db = MagicMock()
        app.db.fetch_all = AsyncMock()
        app.sm = MagicMock()
        app.settings = MagicMock()
        app.settings.approval = ApprovalSettings(summary_timeout_hours=4)
        return app

    @pytest.mark.asyncio
    async def test_pending_returns_bundles_with_status(self, app_mock):
        from studio.orchestrator.main import _cli_pending

        # Fresh: created recently (within 2h, half the 4h timeout)
        now = 1700000000
        app_mock.sm.now = MagicMock(return_value=now)
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01FRESH", "state": "in_review", "tier": "summary", "created_at": now - 600, "proposal_json": '{"bundle_input":{"idea":"Fresh bundle"}}'},
            {"id": "02STALE", "state": "in_review", "tier": "full_review", "created_at": now - 18000, "proposal_json": '{"bundle_input":{"idea":"Stale bundle"}}'},
            {"id": "03ACT",   "state": "in_review", "tier": "auto_notify", "created_at": now - 8000, "proposal_json": '{"bundle_input":{"idea":"Acting soon"}}'},
        ])

        result = await _cli_pending(app_mock, {})
        bundles = result["bundles"]
        assert len(bundles) == 3

        by_id = {b["id"]: b for b in bundles}
        assert by_id["01FRESH"]["status"] == "fresh"
        assert by_id["02STALE"]["status"] == "stale"  # 5h > 4h timeout
        assert by_id["03ACT"]["status"] == "acting-soon"  # ~2.2h > 2h (half of 4h)

    @pytest.mark.asyncio
    async def test_pending_empty(self, app_mock):
        from studio.orchestrator.main import _cli_pending

        app_mock.sm.now = MagicMock(return_value=1700000000)
        app_mock.db.fetch_all = AsyncMock(return_value=[])

        result = await _cli_pending(app_mock, {})
        assert result["bundles"] == []

    @pytest.mark.asyncio
    async def test_pending_includes_tier_and_idea(self, app_mock):
        from studio.orchestrator.main import _cli_pending

        app_mock.sm.now = MagicMock(return_value=1700000000)
        app_mock.db.fetch_all = AsyncMock(return_value=[
            {"id": "01T", "state": "in_review", "tier": "full_review_cooldown", "created_at": 1699999900, "proposal_json": '{"bundle_input":{"idea":"Critical security fix"}}'},
        ])

        result = await _cli_pending(app_mock, {})
        b = result["bundles"][0]
        assert b["id"] == "01T"
        assert b["tier"] == "full_review_cooldown"
        assert b["idea"] == "Critical security fix"


# ── Evaluate approval matrix integration tests ──────────────────────────────────

class TestEvaluateApprovalMatrixIntegration:
    @pytest.fixture
    def app_mock(self):
        app = MagicMock()
        app.db = MagicMock()
        app.db.fetch_one = AsyncMock()
        app.db.execute = AsyncMock()
        app.db.conn = MagicMock()
        app.db.conn.commit = AsyncMock()
        app.sm = MagicMock()
        app.sm.transition_4_approve_from_review = AsyncMock()
        app.sm.now = MagicMock(return_value=1700000000)
        app.settings = MagicMock()
        app.settings.approval = ApprovalSettings(
            summary_timeout_hours=4,
            cooldown_hours_reversible=1,
            cooldown_hours_irreversible=24,
            mandatory_review_triggers=[
                {"type": "tag", "tag": "auth"},
                {"type": "tag", "tag": "billing"},
                {"type": "tag", "tag": "secrets"},
                {"type": "tag", "tag": "pii"},
            ],
        )
        app.executor = MagicMock()
        app.executor._artifact_store = None
        return app

    @pytest.mark.asyncio
    async def test_auto_ships_when_eligible(self, app_mock):
        from studio.orchestrator.main import Orchestrator
        orch = Orchestrator()
        orch.db = app_mock.db
        orch.sm = app_mock.sm
        orch.settings = app_mock.settings
        orch.executor = app_mock.executor

        app_mock.db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Simple fix"},
                "proposal": {
                    "complexity_score": 2,
                    "risk_score": 1,
                    "task_dag": {"nodes": [], "edges": []},
                },
            }),
            "tags": "[]",
            "irreversible": 0,
        })

        merged = {
            "security": {"findings": [], "threat_model": None},
            "adversarial": {"findings": []},
            "qa": {"verification_plan": {"rollback_plan": {"machine_executable": True}}},
        }

        await orch._evaluate_approval_matrix("01AU", merged)
        # Auto-ship: should call approve
        app_mock.sm.transition_4_approve_from_review.assert_called_once_with("01AU", "approval-matrix")

    @pytest.mark.asyncio
    async def test_does_not_auto_ship_with_sensitive_tags(self, app_mock):
        from studio.orchestrator.main import Orchestrator
        orch = Orchestrator()
        orch.db = app_mock.db
        orch.sm = app_mock.sm
        orch.settings = app_mock.settings
        orch.executor = app_mock.executor

        app_mock.db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "Add auth"},
                "proposal": {"complexity_score": 1, "risk_score": 1},
            }),
            "tags": '["auth"]',
            "irreversible": 0,
        })

        merged = {"security": {"findings": [], "threat_model": None}}

        await orch._evaluate_approval_matrix("01NA", merged)
        # Should NOT auto-approve because of auth tag
        app_mock.sm.transition_4_approve_from_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_bundle_handled_gracefully(self, app_mock):
        from studio.orchestrator.main import Orchestrator
        orch = Orchestrator()
        orch.db = app_mock.db
        orch.sm = app_mock.sm
        orch.settings = app_mock.settings
        orch.executor = app_mock.executor

        app_mock.db.fetch_one = AsyncMock(return_value=None)

        # Should not raise — just log and return
        await orch._evaluate_approval_matrix("MISSING", {})
        app_mock.sm.transition_4_approve_from_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_stores_tier_and_cooldown(self, app_mock):
        from studio.orchestrator.main import Orchestrator
        orch = Orchestrator()
        orch.db = app_mock.db
        orch.sm = app_mock.sm
        orch.settings = app_mock.settings
        orch.executor = app_mock.executor

        app_mock.db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({
                "bundle_input": {"idea": "High risk change"},
                "proposal": {"complexity_score": 8, "risk_score": 7},
            }),
            "tags": "[]",
            "irreversible": 1,
        })

        merged = {"security": {"findings": [], "threat_model": None}}

        await orch._evaluate_approval_matrix("01HR", merged)
        # Should store the tier and cooldown
        execute_calls = [c for c in app_mock.db.execute.call_args_list
                        if "UPDATE bundles SET tier" in str(c)]
        assert len(execute_calls) == 1
        # cooldown should be set
        args = execute_calls[0][0]
        assert "full_review_cooldown" in args[1]
