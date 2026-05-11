"""Notification helper: routes events to GitHub Issues and the event log.

All notification-worthy events append to memory/notifications/log.jsonl.
GitHub Issue comments are posted when a GitHub client and issue number are available.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# Callback signature: async (issue_number: int, body: str) -> None
GitHubCommentFn = Callable[[int, str], Awaitable[None]]


class Notifier:
    """Central notification router for ops events."""

    def __init__(
        self,
        log_path: str | Path = "memory/notifications/log.jsonl",
        post_comment: GitHubCommentFn | None = None,
    ) -> None:
        self._log_path = Path(log_path)
        self._post_comment = post_comment

    # ── public event methods ──────────────────────────────────────────────

    async def notify_stalled(
        self,
        bundle_id: str,
        workers_stale: list[str],
        hours_since: float,
        issue_number: int | None = None,
    ) -> None:
        await self._emit(
            bundle_id=bundle_id,
            reason="stalled_bundle",
            channel="github_issue",
            payload={
                "workers_stale": workers_stale,
                "hours_since_last_heartbeat": round(hours_since, 1),
            },
        )
        if issue_number and self._post_comment:
            body = (
                f"Bundle stalled: no worker heartbeat for {hours_since:.0f}h. "
                f"Stale workers: {', '.join(workers_stale)}."
            )
            await self._post_comment(issue_number, body)

    async def notify_escalation(
        self,
        bundle_id: str,
        day: int,
        message: str,
        issue_number: int | None = None,
    ) -> None:
        await self._emit(
            bundle_id=bundle_id,
            reason=f"escalation_day_{day}",
            channel="github_issue",
            payload={"escalation_day": day, "message": message},
        )
        if issue_number and self._post_comment:
            if day == 21:
                body = f"Escalation day 21: auto-failing bundle. {message}"
            else:
                body = f"Escalation day {day}: {message}"
            await self._post_comment(issue_number, body)

    async def notify_acting_soon(
        self,
        bundle_id: str,
        hours_remaining: float,
        issue_number: int | None = None,
    ) -> None:
        await self._emit(
            bundle_id=bundle_id,
            reason="acting_soon",
            channel="github_issue",
            payload={"hours_remaining": round(hours_remaining, 1)},
        )
        if issue_number and self._post_comment:
            body = (
                f"Acting soon: SUMMARY tier bundle will auto-reject in "
                f"{hours_remaining:.1f}h if no action is taken."
            )
            await self._post_comment(issue_number, body)

    async def notify_recall(
        self,
        bundle_id: str,
        rollback_bundle_id: str,
        issue_number: int | None = None,
    ) -> None:
        await self._emit(
            bundle_id=bundle_id,
            reason="recall_requested",
            channel="github_issue",
            payload={"rollback_bundle_id": rollback_bundle_id},
        )
        if issue_number and self._post_comment:
            body = f"Recall requested. Rollback bundle created: {rollback_bundle_id}."
            await self._post_comment(issue_number, body)

    # ── internal ──────────────────────────────────────────────────────────

    async def _emit(
        self,
        bundle_id: str,
        reason: str,
        channel: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "bundle_id": bundle_id,
            "reason": reason,
            "channel": channel,
            "payload": payload or {},
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            logger.warning("Failed to write notification log", exc_info=True)
