"""Operational tooling: stall detection, escalation ladder, recall, acting-soon, health.

Bundle 3.3 — periodic background checks wired as asyncio tasks in main.py.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .notify import Notifier
from .models import OpsSettings

logger = logging.getLogger(__name__)


# ── health snapshot ───────────────────────────────────────────────────────

@dataclass
class HealthSnapshot:
    orchestrator_ok: bool = True
    db_ok: bool = True
    uptime_seconds: float = 0
    active_bundles: int = 0
    stalled_bundles: int = 0
    total_bundles: int = 0
    recent_errors: list[str] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)
    by_tier: dict[str, int] = field(default_factory=dict)
    by_state: dict[str, int] = field(default_factory=dict)


# ── ops tooling ───────────────────────────────────────────────────────────

class OpsTooling:
    """Periodic operational checks: stall detection, escalation, acting-soon."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        settings: OpsSettings,
        notifier: Notifier,
        now_fn: callable = time.time,
    ) -> None:
        self._db = db
        self._settings = settings
        self._notifier = notifier
        self._now = now_fn
        self._start_time = now_fn()
        self._error_log: list[tuple[float, str]] = []

    # ── periodic entry points (called from main.py loop) ──────────────────

    async def check_stalled_bundles(self) -> list[str]:
        """Find IN_PROGRESS bundles with no worker heartbeat in stall_threshold_hours.

        Returns list of bundle IDs newly detected as stalled.
        """
        threshold = self._now() - (self._settings.stall_threshold_hours * 3600)
        newly_stalled: list[str] = []

        cursor = await self._db.execute(
            "SELECT id, concerns_json FROM bundles WHERE state = 'in_progress'"
        )
        in_progress = await cursor.fetchall()

        for row in in_progress:
            bundle_id = row["id"]
            # Find youngest heartbeat among this bundle's non-terminal workers
            cursor2 = await self._db.execute(
                "SELECT MAX(last_heartbeat) AS latest_hb FROM workers "
                "WHERE bundle_id = ? AND state NOT IN ('complete', 'failed', 'killed')",
                (bundle_id,),
            )
            hb_row = await cursor2.fetchone()
            latest_hb = hb_row["latest_hb"] if hb_row else None

            if latest_hb is None or latest_hb < threshold:
                # Stalled — check if already tracked
                concerns = _parse_concerns(row["concerns_json"])
                if "stalled_since" not in concerns:
                    concerns["stalled_since"] = int(self._now())
                    await self._db.execute(
                        "UPDATE bundles SET concerns_json = ? WHERE id = ?",
                        (json.dumps(concerns), bundle_id),
                    )
                    await self._db.commit()
                    newly_stalled.append(bundle_id)

                    stale_workers = await self._stale_worker_ids(bundle_id, threshold)
                    hours = (self._now() - (latest_hb or 0)) / 3600
                    await self._notifier.notify_stalled(
                        bundle_id, stale_workers, hours,
                        issue_number=await self._get_issue_number(bundle_id),
                    )
                    logger.warning("Bundle %s stalled (%0.1fh since last heartbeat)", bundle_id, hours)
            else:
                # Heartbeats are current — clear stalled state if previously set
                concerns = _parse_concerns(row["concerns_json"])
                if "stalled_since" in concerns:
                    del concerns["stalled_since"]
                    concerns.pop("escalation_last_notified_day", None)
                    await self._db.execute(
                        "UPDATE bundles SET concerns_json = ? WHERE id = ?",
                        (json.dumps(concerns), bundle_id),
                    )
                    await self._db.commit()

        return newly_stalled

    async def check_escalation_ladder(self) -> int:
        """Check stalled bundles against the escalation ladder (5/10/21 days).

        Returns count of bundles auto-failed at day 21.
        """
        auto_failed = 0
        cursor = await self._db.execute(
            "SELECT id, concerns_json, github_issue_number FROM bundles "
            "WHERE state = 'in_progress' AND concerns_json LIKE '%stalled_since%'"
        )
        stalled_rows = await cursor.fetchall()
        now_ts = int(self._now())

        for row in stalled_rows:
            concerns = _parse_concerns(row["concerns_json"])
            stalled_since = concerns.get("stalled_since")
            if stalled_since is None:
                continue

            days_stalled = (now_ts - stalled_since) / 86400
            last_notified = concerns.get("escalation_last_notified_day", 0)
            issue_number = row["github_issue_number"]

            for day in self._settings.escalation_days:
                if days_stalled >= day and last_notified < day:
                    if day == 21:
                        # Auto-fail
                        await self._db.execute(
                            "UPDATE bundles SET state = 'failed', completed_at = ?, "
                            "outcome_json = ? WHERE id = ?",
                            (now_ts, json.dumps({"exit_reason": "stalled_escalation_limit"}), row["id"]),
                        )
                        await self._db.commit()
                        auto_failed += 1
                        await self._notifier.notify_escalation(
                            row["id"], day,
                            "Auto-failed: bundle exceeded 21-day escalation limit.",
                            issue_number=issue_number,
                        )
                        logger.error("Bundle %s auto-failed at escalation day 21", row["id"])
                    else:
                        await self._notifier.notify_escalation(
                            row["id"], day,
                            f"Bundle has been stalled for {days_stalled:.0f} days and requires attention.",
                            issue_number=issue_number,
                        )
                        concerns["escalation_last_notified_day"] = day
                        await self._db.execute(
                            "UPDATE bundles SET concerns_json = ? WHERE id = ?",
                            (json.dumps(concerns), row["id"]),
                        )
                        await self._db.commit()
                        logger.warning("Bundle %s escalation day %d triggered", row["id"], day)

        return auto_failed

    async def check_acting_soon(self) -> None:
        """Check SUMMARY-tier bundles approaching the summary_timeout_hours deadline."""
        cursor = await self._db.execute(
            "SELECT id, tier, approved_at, github_issue_number FROM bundles "
            "WHERE state = 'in_review' AND tier = 'summary'"
        )
        summary_rows = await cursor.fetchall()
        now_ts = int(self._now())
        lead_seconds = self._settings.acting_soon_hours * 3600

        for row in summary_rows:
            approved_at = row["approved_at"]
            if approved_at is None:
                continue
            from .models import ApprovalTier
            # Use the approval settings timeout — hardcoded 4h default from ApprovalSettings
            timeout_seconds = 4 * 3600  # summary_timeout_hours default
            deadline = approved_at + timeout_seconds
            remaining = deadline - now_ts

            if 0 < remaining <= lead_seconds:
                # Check if we already notified for this window
                concerns = await self._load_concerns(row["id"])
                last_acting_soon = concerns.get("last_acting_soon_at", 0)
                # Re-notify every 4 hours
                if now_ts - last_acting_soon > 14400:
                    await self._notifier.notify_acting_soon(
                        row["id"], remaining / 3600,
                        issue_number=row["github_issue_number"],
                    )
                    concerns["last_acting_soon_at"] = now_ts
                    await self._store_concerns(row["id"], concerns)
                    logger.info("Acting-soon notification for bundle %s (%0.1fh remaining)", row["id"], remaining / 3600)

    # ── recall ────────────────────────────────────────────────────────────

    async def recall_bundle(
        self, bundle_id: str, actor: str = "cli"
    ) -> dict[str, Any]:
        """Check if a COMPLETE bundle is eligible for recall.

        Returns {"eligible": True, "bundle_id": "..."} or {"eligible": False, "reason": "..."}
        """
        cursor = await self._db.execute(
            "SELECT id, state, completed_at, proposal_json FROM bundles WHERE id = ?",
            (bundle_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"eligible": False, "reason": f"Bundle {bundle_id} not found"}

        if row["state"] != "complete":
            return {"eligible": False, "reason": f"Bundle is {row['state']}, not complete"}

        completed_at = row["completed_at"]
        if completed_at is None:
            return {"eligible": False, "reason": "Bundle has no completed_at timestamp"}

        now_ts = int(self._now())
        window_seconds = self._settings.recall_window_hours * 3600
        elapsed = now_ts - completed_at

        if elapsed > window_seconds:
            hours = elapsed / 3600
            return {
                "eligible": False,
                "reason": f"Recall window closed ({hours:.0f}h elapsed, max {self._settings.recall_window_hours}h)",
            }

        return {"eligible": True, "bundle_id": bundle_id, "completed_at": completed_at}

    async def notify_recall(
        self, bundle_id: str, rollback_bundle_id: str
    ) -> None:
        """Notify that a recall was requested for a bundle."""
        issue_number = await self._get_issue_number(bundle_id)
        await self._notifier.notify_recall(
            bundle_id, rollback_bundle_id, issue_number=issue_number
        )

    # ── health ────────────────────────────────────────────────────────────

    async def get_health(self) -> HealthSnapshot:
        """Assemble a health dashboard snapshot."""
        snap = HealthSnapshot(uptime_seconds=self._now() - self._start_time)

        # DB check
        try:
            cursor = await self._db.execute("SELECT 1")
            await cursor.fetchone()
        except Exception:
            snap.db_ok = False
            snap.orchestrator_ok = False

        # Bundle counts
        cursor = await self._db.execute("SELECT state, tier, COUNT(*) AS cnt FROM bundles GROUP BY state, tier")
        rows = await cursor.fetchall()
        for r in rows:
            state = r["state"]
            tier = r["tier"] or "unknown"
            snap.by_state[state] = snap.by_state.get(state, 0) + r["cnt"]
            snap.by_tier[tier] = snap.by_tier.get(tier, 0) + r["cnt"]
            snap.total_bundles += r["cnt"]
            if state in ("in_progress", "approved", "verifying"):
                snap.active_bundles += r["cnt"]

        # Stalled count
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS cnt FROM bundles WHERE concerns_json LIKE '%stalled_since%'"
        )
        row = await cursor.fetchone()
        snap.stalled_bundles = row["cnt"] if row else 0

        # Recent errors from in-memory log
        snap.recent_errors = [msg for _, msg in self._error_log[-10:]]

        # Calibration summary (high-level)
        try:
            import json as _json
            from pathlib import Path as _Path
            cal_path = _Path("memory/calibration/scoring-outcomes.jsonl")
            if cal_path.exists():
                lines = cal_path.read_text().strip().split("\n")
                total = len(lines)
                passed = sum(1 for line in lines if _json.loads(line).get("verification_passed"))
                snap.calibration = {
                    "total_outcomes": total,
                    "pass_rate": round(passed / total, 3) if total > 0 else None,
                }
        except Exception:
            snap.calibration = {"error": "unavailable"}

        return snap

    def record_error(self, message: str) -> None:
        self._error_log.append((self._now(), message))
        # Keep last 100
        if len(self._error_log) > 100:
            self._error_log = self._error_log[-100:]

    # ── internal helpers ──────────────────────────────────────────────────

    async def _stale_worker_ids(self, bundle_id: str, threshold: float) -> list[str]:
        cursor = await self._db.execute(
            "SELECT id FROM workers WHERE bundle_id = ? AND "
            "state NOT IN ('complete', 'failed', 'killed') AND "
            "(last_heartbeat IS NULL OR last_heartbeat < ?)",
            (bundle_id, threshold),
        )
        rows = await cursor.fetchall()
        return [r["id"] for r in rows]

    async def _get_issue_number(self, bundle_id: str) -> int | None:
        cursor = await self._db.execute(
            "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
        )
        row = await cursor.fetchone()
        return row["github_issue_number"] if row else None

    async def _load_concerns(self, bundle_id: str) -> dict:
        cursor = await self._db.execute(
            "SELECT concerns_json FROM bundles WHERE id = ?", (bundle_id,)
        )
        row = await cursor.fetchone()
        return _parse_concerns(row["concerns_json"] if row else None)

    async def _store_concerns(self, bundle_id: str, concerns: dict) -> None:
        await self._db.execute(
            "UPDATE bundles SET concerns_json = ? WHERE id = ?",
            (json.dumps(concerns), bundle_id),
        )
        await self._db.commit()


def _parse_concerns(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
