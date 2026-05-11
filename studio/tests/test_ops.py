"""Tests for ops.py — stall detection, escalation, recall, acting-soon, health."""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
import aiosqlite

from studio.orchestrator.ops import OpsTooling, HealthSnapshot, _parse_concerns
from studio.orchestrator.notify import Notifier
from studio.orchestrator.models import OpsSettings


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    db_path = ":memory:"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    # Create minimal schema
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS bundles (
          id TEXT PRIMARY KEY,
          state TEXT NOT NULL,
          tier TEXT NOT NULL DEFAULT 'full_review',
          complexity_score INTEGER,
          risk_score INTEGER,
          proposal_json TEXT NOT NULL DEFAULT '{}',
          concerns_json TEXT,
          outcome_json TEXT,
          created_at INTEGER NOT NULL,
          approved_at INTEGER,
          completed_at INTEGER,
          github_issue_number INTEGER
        );
        CREATE TABLE IF NOT EXISTS workers (
          id TEXT PRIMARY KEY,
          bundle_id TEXT NOT NULL,
          node_id TEXT NOT NULL,
          token TEXT NOT NULL,
          manifest_json TEXT NOT NULL DEFAULT '{}',
          state TEXT NOT NULL,
          pid INTEGER,
          current_phase TEXT,
          last_heartbeat INTEGER,
          created_at INTEGER NOT NULL
        );
    """)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def settings():
    return OpsSettings(
        stall_threshold_hours=8,
        escalation_days=[5, 10, 21],
        recall_window_hours=48,
        acting_soon_hours=12,
    )


@pytest.fixture
def tmp_log_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "notifications" / "log.jsonl"


@pytest.fixture
def notifier(tmp_log_path):
    return Notifier(log_path=tmp_log_path)


@pytest.fixture
def frozen_time():
    """Return a controllable now_fn that starts at a fixed epoch."""
    base = 1700000000.0  # 2023-11-14
    state = {"now": base}

    def _now():
        return state["now"]

    return state, _now


# ── _parse_concerns ────────────────────────────────────────────────────────

class TestParseConcerns:
    def test_empty(self):
        assert _parse_concerns(None) == {}
        assert _parse_concerns("") == {}

    def test_valid_json(self):
        assert _parse_concerns('{"stalled_since": 123}') == {"stalled_since": 123}

    def test_invalid_json(self):
        assert _parse_concerns("{not valid") == {}


# ── stall detection ────────────────────────────────────────────────────────

class TestStallDetection:
    async def test_no_in_progress_bundles(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == []

    async def test_bundle_with_recent_heartbeat_not_stalled(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)",
            (now,)
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, state, last_heartbeat, created_at) "
            "VALUES ('w1', 'b1', 'n1', 'tok', 'running', ?, ?)",
            (now - 60, now),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == []

    async def test_bundle_with_stale_heartbeat_triggers_stall(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)",
            (now - 36000,)
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, state, last_heartbeat, created_at) "
            "VALUES ('w1', 'b1', 'n1', 'tok', 'running', ?, ?)",
            (now - 36000, now - 36000),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == ["b1"]

        # Verify concerns_json was updated
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"])
        assert "stalled_since" in concerns

    async def test_stalled_bundle_clears_when_heartbeat_returns(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?)",
            (now - 36000, json.dumps({"stalled_since": now - 36000})),
        )
        # Worker heartbeat is recent
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, state, last_heartbeat, created_at) "
            "VALUES ('w1', 'b1', 'n1', 'tok', 'running', ?, ?)",
            (now - 30, now - 36000),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == []

        # Verify stalled_since cleared
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"])
        assert "stalled_since" not in concerns

    async def test_bundle_with_no_workers_is_stalled(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)",
            (now - 36000,)
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == ["b1"]

    async def test_already_stalled_not_duplicated(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?)",
            (now - 36000, json.dumps({"stalled_since": now - 10000})),
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, state, last_heartbeat, created_at) "
            "VALUES ('w1', 'b1', 'n1', 'tok', 'running', ?, ?)",
            (now - 36000, now - 36000),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        stalled = await ops.check_stalled_bundles()
        assert stalled == []  # Already tracked, not newly stalled


# ── escalation ladder ──────────────────────────────────────────────────────

class TestEscalationLadder:
    async def test_day_5_escalation_notifies(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        stalled_since = now - (5 * 86400) - 3600  # 5 days + 1 hour
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json, github_issue_number) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?, ?)",
            (stalled_since, json.dumps({"stalled_since": stalled_since}), 1),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        auto_failed = await ops.check_escalation_ladder()
        assert auto_failed == 0

        # Verify escalation tracked
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"])
        assert concerns["escalation_last_notified_day"] == 5

    async def test_day_21_auto_fails(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        stalled_since = now - (21 * 86400) - 3600  # 21 days + 1 hour
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json, github_issue_number) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?, ?)",
            (stalled_since, json.dumps({"stalled_since": stalled_since}), 1),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        auto_failed = await ops.check_escalation_ladder()
        assert auto_failed == 1

        # Verify state changed to failed
        row = await db.execute("SELECT state, outcome_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        assert row["state"] == "failed"
        outcome = json.loads(row["outcome_json"])
        assert outcome["exit_reason"] == "stalled_escalation_limit"

    async def test_already_notified_day_not_duplicated(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        stalled_since = now - (6 * 86400)  # 6 days
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?)",
            (stalled_since, json.dumps({
                "stalled_since": stalled_since,
                "escalation_last_notified_day": 5,
            })),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        auto_failed = await ops.check_escalation_ladder()
        assert auto_failed == 0

        # Should NOT have updated to day 10 yet (hasn't reached day 10)
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"])
        assert concerns["escalation_last_notified_day"] == 5

    async def test_skips_non_stalled_bundles(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)",
            (now - 86400 * 30,)
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        auto_failed = await ops.check_escalation_ladder()
        assert auto_failed == 0


# ── recall ─────────────────────────────────────────────────────────────────

class TestRecall:
    async def test_bundle_not_found(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        result = await ops.recall_bundle("nonexistent")
        assert result["eligible"] is False
        assert "not found" in result["reason"]

    async def test_not_complete_state(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)",
            (now,)
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        result = await ops.recall_bundle("b1")
        assert result["eligible"] is False
        assert "not complete" in result["reason"]

    async def test_within_recall_window(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, completed_at) "
            "VALUES ('b1', 'complete', 'full_review', ?, ?)",
            (now - 86400, now - 3600),  # completed 1 hour ago
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        result = await ops.recall_bundle("b1")
        assert result["eligible"] is True

    async def test_outside_recall_window(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        completed_at = now - (49 * 3600)  # 49 hours ago (> 48h window)
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, completed_at) "
            "VALUES ('b1', 'complete', 'full_review', ?, ?)",
            (completed_at - 3600, completed_at),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        result = await ops.recall_bundle("b1")
        assert result["eligible"] is False
        assert "Recall window closed" in result["reason"]


# ── acting-soon ────────────────────────────────────────────────────────────

class TestActingSoon:
    async def test_summary_bundle_within_warning_window(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        # Approved 3 hours ago, summary_timeout = 4h, acting_soon = 12h warning
        # So 1h remaining, should fire
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, approved_at, github_issue_number) "
            "VALUES ('b1', 'in_review', 'summary', ?, ?, ?)",
            (now - 14400, now - 10800, 1),  # approved 3h ago
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        await ops.check_acting_soon()

        # Verify concerns_json has acting-soon tracking
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"] or "{}")
        assert "last_acting_soon_at" in concerns

    async def test_summary_bundle_warns_immediately_with_short_timeout(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        # Just approved with 4h timeout and 12h acting-soon window —
        # within warning window immediately
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, approved_at) "
            "VALUES ('b1', 'in_review', 'summary', ?, ?)",
            (now, now),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        await ops.check_acting_soon()

        # Acts immediately because 4h timeout < 12h warning lead
        row = await db.execute("SELECT concerns_json FROM bundles WHERE id = 'b1'")
        row = await row.fetchone()
        concerns = json.loads(row["concerns_json"] or "{}")
        assert "last_acting_soon_at" in concerns

    async def test_already_past_deadline_no_duplicate(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        # Already past deadline (approved 6h ago, 4h timeout)
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, approved_at) "
            "VALUES ('b1', 'in_review', 'summary', ?, ?)",
            (now - 25200, now - 21600),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        await ops.check_acting_soon()
        # Past deadline, remaining <= 0, should not fire acting-soon


# ── health ─────────────────────────────────────────────────────────────────

class TestHealth:
    async def test_empty_db(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        snap = await ops.get_health()
        assert snap.orchestrator_ok is True
        assert snap.db_ok is True
        assert snap.total_bundles == 0

    async def test_with_bundles(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at) VALUES ('b1', 'in_progress', 'full_review', ?)", (now,)
        )
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, completed_at) VALUES ('b2', 'complete', 'auto', ?, ?)",
            (now, now),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        snap = await ops.get_health()
        assert snap.total_bundles == 2
        assert snap.active_bundles == 1
        assert "in_progress" in snap.by_state
        assert "complete" in snap.by_state

    async def test_stalled_count(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        now = int(now_fn())
        await db.execute(
            "INSERT INTO bundles (id, state, tier, created_at, concerns_json) "
            "VALUES ('b1', 'in_progress', 'full_review', ?, ?)",
            (now, json.dumps({"stalled_since": now - 3600})),
        )
        await db.commit()

        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        snap = await ops.get_health()
        assert snap.stalled_bundles == 1

    async def test_uptime_increases(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        snap = await ops.get_health()
        assert snap.uptime_seconds >= 0

    async def test_record_and_retrieve_errors(self, db, settings, notifier, frozen_time):
        state, now_fn = frozen_time
        ops = OpsTooling(db, settings, notifier, now_fn=now_fn)
        ops.record_error("something went wrong")
        snap = await ops.get_health()
        assert "something went wrong" in snap.recent_errors


# ── notifier ───────────────────────────────────────────────────────────────

class TestNotifier:
    async def test_notify_stalled_writes_log(self, tmp_log_path):
        n = Notifier(log_path=tmp_log_path)
        await n.notify_stalled("b1", ["w1"], 10.0)
        assert tmp_log_path.exists()
        lines = tmp_log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["bundle_id"] == "b1"
        assert entry["reason"] == "stalled_bundle"
        assert entry["payload"]["workers_stale"] == ["w1"]

    async def test_notify_escalation(self, tmp_log_path):
        n = Notifier(log_path=tmp_log_path)
        await n.notify_escalation("b1", 21, "Auto-failed")
        entry = json.loads(tmp_log_path.read_text().strip().split("\n")[0])
        assert entry["reason"] == "escalation_day_21"
        assert entry["payload"]["escalation_day"] == 21

    async def test_notify_acting_soon(self, tmp_log_path):
        n = Notifier(log_path=tmp_log_path)
        await n.notify_acting_soon("b1", 5.5)
        entry = json.loads(tmp_log_path.read_text().strip().split("\n")[0])
        assert entry["reason"] == "acting_soon"
        assert entry["payload"]["hours_remaining"] == 5.5

    async def test_notify_recall(self, tmp_log_path):
        n = Notifier(log_path=tmp_log_path)
        await n.notify_recall("b1", "rollback-b1")
        entry = json.loads(tmp_log_path.read_text().strip().split("\n")[0])
        assert entry["reason"] == "recall_requested"
        assert entry["payload"]["rollback_bundle_id"] == "rollback-b1"

    async def test_post_comment_called_when_issue_number_present(self, tmp_log_path):
        calls = []

        async def mock_post(issue_number, body):
            calls.append((issue_number, body))

        n = Notifier(log_path=tmp_log_path, post_comment=mock_post)
        await n.notify_stalled("b1", ["w1"], 10.0, issue_number=42)
        assert len(calls) == 1
        assert calls[0][0] == 42
        assert "stalled" in calls[0][1].lower()

    async def test_post_comment_skipped_when_no_issue_number(self, tmp_log_path):
        calls = []

        async def mock_post(issue_number, body):
            calls.append((issue_number, body))

        n = Notifier(log_path=tmp_log_path, post_comment=mock_post)
        await n.notify_stalled("b1", ["w1"], 10.0)  # no issue_number
        assert len(calls) == 0
        # Log still written
        assert tmp_log_path.exists()


# ── OpsSettings defaults ───────────────────────────────────────────────────

class TestOpsSettingsDefaults:
    def test_default_values(self):
        s = OpsSettings()
        assert s.stall_threshold_hours == 8
        assert s.escalation_days == [5, 10, 21]
        assert s.recall_window_hours == 48
        assert s.acting_soon_hours == 12


# ── HealthSnapshot ─────────────────────────────────────────────────────────

class TestHealthSnapshot:
    def test_defaults(self):
        snap = HealthSnapshot()
        assert snap.orchestrator_ok is True
        assert snap.active_bundles == 0
        assert snap.recent_errors == []
