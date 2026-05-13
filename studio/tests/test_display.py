"""Tests for display formatting functions."""
import json
import time

import pytest
from studio.orchestrator.display import (
    format_bundle_show,
    format_bundle_list,
    format_worker_show,
    format_health,
    format_status,
    format_calibration,
    _format_age,
    _format_duration,
)


class TestFormatAge:
    def test_seconds(self):
        assert _format_age(0) == "0s"
        assert _format_age(45) == "45s"

    def test_minutes(self):
        assert _format_age(60) == "1m"
        assert _format_age(3599) == "59m"

    def test_hours(self):
        assert _format_age(3600) == "1h"
        assert _format_age(86399) == "23h"

    def test_days(self):
        assert _format_age(86400) == "1d"
        assert _format_age(172800) == "2d"


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(30) == "30s"

    def test_minutes(self):
        assert _format_duration(90) == "1m 30s"

    def test_hours(self):
        assert _format_duration(3661) == "1h 1m"


class TestFormatBundleShow:
    def _bundle(self, **overrides):
        now = int(time.time())
        defaults = {
            "id": "01TEST1234ABCDEF",
            "state": "in_review",
            "tier": "pending_review",
            "created_at": now - 1200,
            "irreversible": 0,
        }
        defaults.update(overrides)
        return defaults

    def _proposal(self):
        return {
            "bundle_input": {
                "idea": "Test idea for a feature",
            },
            "proposal": {
                "complexity_score": 2,
                "risk_score": 1,
                "estimated_loc": 50,
                "estimated_duration_seconds": 60,
                "estimated_worker_count": 1,
                "estimated_tokens": 500,
                "implementation_plan": "Create app.py with Flask",
                "concerns": ["Test mode concern"],
                "requirements_summary": "Test summary",
            },
        }

    def _nodes(self):
        return [
            {"node_id": "adversarial", "kind": "worker", "state": "pending"},
            {"node_id": "security", "kind": "worker", "state": "pending"},
            {"node_id": "qa", "kind": "worker", "state": "pending"},
            {"node_id": "review-aggregator", "kind": "aggregator", "state": "pending"},
            {"node_id": "implement-idea", "kind": "worker", "state": "pending"},
        ]

    def _edges(self):
        return [
            {"from_node_id": "adversarial", "to_node_id": "review-aggregator", "condition_kind": "on_success"},
        ]

    def _audit(self):
        now = int(time.time())
        return [
            {
                "event_type": "bundle_input_received",
                "created_at": now - 1200,
                "payload_json": json.dumps({"state": "proposed", "mode": "idea_only"}),
            },
            {
                "event_type": "bundle_planning_complete",
                "created_at": now - 1190,
                "payload_json": json.dumps({"from_state": "proposed", "to_state": "in_review"}),
            },
        ]

    def test_default_output(self):
        output = format_bundle_show(
            self._bundle(), self._proposal(), self._nodes(), self._edges(),
            self._audit(), [], verbose=False,
        )
        assert "Bundle: 01TEST1234ABCDEF" in output
        assert "State: in_review (pending_review)" in output
        assert "Test idea for a feature" in output
        assert "Complexity: 2/10" in output
        assert "Risk: 1/10" in output
        assert "Irreversible: no" in output
        assert "Estimate: 50 loc" in output
        assert "Plan: Create app.py with Flask" in output
        assert "DAG: 5 total" in output
        assert "Approve: studio approve" in output
        assert "bundle_input_received" in output
        assert "bundle_planning_complete" in output

    def test_verbose_output(self):
        output = format_bundle_show(
            self._bundle(), self._proposal(), self._nodes(), self._edges(),
            self._audit(), [], verbose=True,
        )
        assert "Full audit trail" in output
        assert "DAG nodes:" in output
        assert "DAG edges:" in output
        assert "adversarial" in output
        assert "review-aggregator" in output

    def test_bundle_in_progress_shows_kill_hint(self):
        output = format_bundle_show(
            self._bundle(state="in_progress"), self._proposal(), [], [], [], [],
        )
        assert "Kill:" in output
        assert "studio kill" in output

    def test_irreversible_bundle(self):
        output = format_bundle_show(
            self._bundle(irreversible=1), self._proposal(), [], [], [], [],
        )
        assert "Irreversible: yes" in output

    def test_no_concerns(self):
        p = self._proposal()
        p["proposal"]["concerns"] = []
        output = format_bundle_show(self._bundle(), p, [], [], [], [])
        assert "Concerns:" not in output

    def test_no_implementation_plan(self):
        p = self._proposal()
        p["proposal"]["implementation_plan"] = ""
        output = format_bundle_show(self._bundle(), p, [], [], [], [])
        assert "Plan:" not in output


class TestFormatBundleList:
    def test_empty(self):
        assert format_bundle_list([]) == "No bundles found."

    def test_with_bundles(self):
        bundles = [
            {
                "id": "01KRHT91RB58JND8JTN1XWMYBN",
                "state": "in_review",
                "tier": "pending_review",
                "age": "19m",
                "idea": "Build a hello-world app using flask and docker.",
            },
        ]
        output = format_bundle_list(bundles)
        assert "ID" in output
        assert "STATE" in output
        assert "TIER" in output
        assert "AGE" in output
        assert "IDEA" in output
        assert "in_review" in output
        assert "pending_review" in output
        assert "19m" in output

    def test_truncates_long_id(self):
        bundles = [{"id": "01KRHT91RB58JND8JTN1XWMYBN_extra", "state": "x", "tier": "y", "age": "0s", "idea": "z"}]
        output = format_bundle_list(bundles)
        assert "01KRHT91RB58JND8JTN…" in output
        assert "_extra" not in output

    def test_truncates_long_idea(self):
        bundles = [{"id": "x", "state": "x", "tier": "y", "age": "0s", "idea": "a" * 100}]
        output = format_bundle_list(bundles)
        assert "…" in output


class TestFormatWorkerShow:
    def _worker(self, **overrides):
        now = int(time.time())
        defaults = {
            "id": "worker-1",
            "bundle_id": "bundle-1",
            "node_id": "implement-idea",
            "state": "complete",
            "current_phase": None,
            "created_at": now - 300,
            "ended_at": now - 60,
            "last_heartbeat": now - 120,
            "exit_reason": None,
            "manifest_json": "{}",
        }
        defaults.update(overrides)
        return defaults

    def test_basic_output(self):
        output = format_worker_show(self._worker(), None, {"allowed": 0, "denied": 0})
        assert "Worker: worker-1" in output
        assert "Bundle: bundle-1" in output
        assert "State:  complete" in output
        assert "Capabilities: no manifest" in output

    def test_with_node(self):
        node = {"spec_json": json.dumps({"objective": "Build a feature"})}
        output = format_worker_show(self._worker(), node, {"allowed": 0, "denied": 0})
        assert "Task: Build a feature" in output

    def test_bundler_worker(self):
        output = format_worker_show(
            self._worker(node_id="bundler"), None, {"allowed": 0, "denied": 0}
        )
        assert "bundler — produces proposal" in output

    def test_exit_reason(self):
        output = format_worker_show(
            self._worker(exit_reason="killed via cli"), None, {"allowed": 0, "denied": 0}
        )
        assert "Exit:   killed via cli" in output

    def test_cap_checks(self):
        output = format_worker_show(
            self._worker(), None, {"allowed": 5, "denied": 2}
        )
        assert "Cap checks: 5 allowed, 2 denied" in output

    def test_capability_summary(self):
        manifest = json.dumps({
            "grants": {
                "filesystem": {"reads": [{"path": "/a"}, {"path": "/b"}]},
                "network": {"egress": [{}]},
            }
        })
        output = format_worker_show(
            self._worker(manifest_json=manifest), None, {"allowed": 0, "denied": 0}
        )
        assert "Capabilities: 2 reads, 1 egress endpoints" in output


class TestFormatHealth:
    def test_basic(self):
        snap = {
            "orchestrator_ok": True,
            "db_ok": True,
            "uptime_seconds": 150,
            "total_bundles": 3,
            "active_bundles": 0,
            "stalled_bundles": 0,
            "by_state": {"in_review": 2, "complete": 1},
            "by_tier": {"pending_review": 2, "full_review": 1},
            "calibration": {"total_outcomes": 10, "pass_rate": 0.8},
            "recent_errors": [],
        }
        output = format_health(snap)
        assert "Orchestrator: OK" in output
        assert "DB: OK" in output
        assert "Uptime: 2m 30s" in output
        assert "Bundles: 3 total" in output
        assert "in_review" in output
        assert "pending_review" in output
        assert "pass rate 80%" in output
        assert "Recent errors: (none)" in output

    def test_degraded(self):
        snap = {
            "orchestrator_ok": False,
            "db_ok": False,
            "uptime_seconds": 0,
            "total_bundles": 0,
            "active_bundles": 0,
            "stalled_bundles": 1,
            "by_state": {},
            "by_tier": {},
            "calibration": {},
            "recent_errors": ["Connection refused", "Timeout"],
        }
        output = format_health(snap)
        assert "DEGRADED" in output
        assert "FAILED" in output
        assert "Connection refused" in output


class TestFormatStatus:
    def test_basic(self):
        output = format_status(uptime=300, worker_count=4, queue_depth=2)
        assert "Uptime: 5m 0s" in output
        assert "Workers: 4 running" in output
        assert "Queue: 2" in output


class TestFormatCalibration:
    def test_empty(self):
        data = {"message": "No calibration data recorded yet."}
        assert format_calibration(data) == "No calibration data recorded yet."

    def test_with_entries(self):
        data = {
            "total_entries": 2,
            "entries_with_divergence": 0,
            "recent": [
                {
                    "bundle_id": "01TEST1234",
                    "estimated": {"loc": 50, "duration_seconds": 60, "tokens": 500},
                    "actual": {"loc": 45, "duration_seconds": 55, "tokens": 480},
                },
            ],
        }
        output = format_calibration(data)
        assert "Calibration: 2 entries, 0 with divergence" in output
        assert "01TEST12" in output
        assert "loc=50" in output
        assert "dur=1m 0s" in output
        assert "tok=500" in output
