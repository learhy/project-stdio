"""Shared PM escalation logic (Bundle 5.3).

Used by both question routing (rpc.py) and review verdict handling (review.py)
to avoid circular imports. Handles: pause worker, insert intervention record,
post GitHub comment, record audit trail.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import ulid

if TYPE_CHECKING:
    from .db import Database
    from .rpc import RpcHandlers, ConnectionManager
    from .github import GitHubClient

logger = logging.getLogger(__name__)


async def escalate_to_pm(
    db: "Database",
    handlers: "RpcHandlers",
    conn_mgr: "ConnectionManager",
    github_client: "GitHubClient | None",
    worker_id: str,
    bundle_id: str,
    node_id: str,
    reason: str,
    content: str,
    escalation_type: str,
    question_id: str | None = None,
) -> str:
    """Escalate a worker question or review concern to the PM.

    Returns the intervention_id.
    """
    now = int(time.time())
    intervention_id = str(ulid.ULID())

    # 1. Pause worker: update DB state
    await db.execute(
        "UPDATE workers SET state = ? WHERE id = ?",
        ("paused", worker_id),
    )
    await db.conn.commit()

    # Send worker.pause RPC signal to stop token consumption
    try:
        await conn_mgr.call_worker(worker_id, "worker.pause", {
            "reason": reason,
        }, timeout=10.0)
    except (ValueError, Exception):
        logger.warning("Could not send pause signal to worker %s", worker_id)

    # 2. Record intervention
    await db.execute(
        "INSERT INTO worker_interventions (intervention_id, worker_id, bundle_id, "
        "type, content, triggered_by, trigger_reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (intervention_id, worker_id, bundle_id, escalation_type, content,
         reason, "pending", now),
    )
    await db.conn.commit()

    # 3. Post GitHub comment
    if github_client is not None:
        bundle_row = await db.fetch_one(
            "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
        )
        if bundle_row and bundle_row["github_issue_number"]:
            issue_number = bundle_row["github_issue_number"]

            if escalation_type == "question_escalation":
                body = _format_question_escalation_comment(
                    worker_id, node_id, content, question_id,
                )
            else:
                body = _format_review_escalation_comment(
                    worker_id, node_id, content, reason,
                )

            await github_client.post_comment(issue_number, body)

    # 4. Audit log
    await db.execute(
        "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("worker.escalated_to_pm", "worker", worker_id,
         json.dumps({"intervention_id": intervention_id, "type": escalation_type,
                    "reason": reason, "question_id": question_id}), now),
    )
    await db.conn.commit()

    logger.info("Escalated to PM: worker=%s type=%s intervention=%s",
                worker_id, escalation_type, intervention_id)
    return intervention_id


async def resolve_escalation(
    db: "Database",
    handlers: "RpcHandlers",
    conn_mgr: "ConnectionManager",
    github_client: "GitHubClient | None",
    intervention_id: str,
    worker_id: str,
    bundle_id: str,
    response_text: str,
    actor: str,
) -> bool:
    """Resolve an escalated intervention with a PM answer.

    Sends inject_context to worker, resumes it, updates intervention status,
    and posts GitHub confirmation.
    """
    now = int(time.time())

    # 1. Send answer to worker via inject_context
    if handlers._on_inject_context:
        injection_id = str(ulid.ULID())
        await handlers._on_inject_context(
            worker_id, injection_id, "question_response", response_text, None,
        )

    # 2. Resume worker: update DB
    await db.execute(
        "UPDATE workers SET state = ? WHERE id = ?",
        ("running", worker_id),
    )
    await db.conn.commit()

    # Send worker.resume RPC signal
    try:
        await conn_mgr.call_worker(worker_id, "worker.resume", {
            "context": response_text,
        }, timeout=10.0)
    except (ValueError, Exception):
        logger.warning("Could not send resume signal to worker %s", worker_id)

    # 3. Update intervention status
    await db.execute(
        "UPDATE worker_interventions SET status = ? WHERE intervention_id = ?",
        ("answered", intervention_id),
    )
    await db.conn.commit()

    # 4. Post GitHub confirmation
    if github_client is not None:
        bundle_row = await db.fetch_one(
            "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
        )
        if bundle_row and bundle_row["github_issue_number"]:
            ack = f"@{actor} Intervention `{intervention_id}` resolved. Worker resumed."
            await github_client.post_comment(bundle_row["github_issue_number"], ack)

    # 5. Audit log
    await db.execute(
        "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("worker.escalation_resolved", "worker", worker_id,
         json.dumps({"intervention_id": intervention_id, "actor": actor}), now),
    )
    await db.conn.commit()

    logger.info("Escalation resolved: intervention=%s by=%s", intervention_id, actor)
    return True


# ── GitHub comment formatters ─────────────────────────────────────────────

def _format_question_escalation_comment(
    worker_id: str, node_id: str, question: str, question_id: str | None,
) -> str:
    return (
        f"## Worker needs guidance — `{worker_id}` on `{node_id}`\n\n"
        f"**Question:**\n{question}\n\n"
        f"---\n"
        f"Reply with `/answer:{question_id} your response here` or use Claude Desktop."
    )


def _format_review_escalation_comment(
    worker_id: str, node_id: str, rationale: str, trigger_reason: str,
) -> str:
    return (
        f"## Review escalation — `{worker_id}` on `{node_id}`\n\n"
        f"The orchestrator's review pass flagged a concern and needs human judgment.\n\n"
        f"**Concern:**\n{rationale}\n\n"
        f"**Trigger:** {trigger_reason}\n\n"
        f"**Options:**\n"
        f"- Let it continue: `/resume:{worker_id}`\n"
        f"- Redirect: `/resume:{worker_id} <new direction>`\n"
        f"- Kill and restart: `studio kill <bundle-id>`"
    )


def format_checkpoint_comment(
    worker_id: str, phase_completed: str, phase_starting: str,
    summary: str, concerns: list[str],
) -> str:
    body = (
        f"## Worker checkpoint — `{worker_id}`\n\n"
        f"**Completed:** {phase_completed}\n"
        f"**Starting:** {phase_starting}\n\n"
        f"{summary}"
    )
    if concerns:
        body += f"\n\n**Worker flagged concerns:** {'; '.join(concerns)}"
    return body


def format_final_report_comment(
    bundle_id: str, outcome: dict, proposal: dict,
) -> str:
    status_icon = "PASSED" if outcome.get("status") == "shipped" else "FAILED"
    summary = outcome.get("summary", outcome.get("rationale", "No summary"))

    bundler = proposal.get("proposal", {})
    estimated_loc = bundler.get("estimated_loc", 0)
    estimated_duration = bundler.get("estimated_duration_seconds", 0)
    estimated_tokens = bundler.get("estimated_tokens", 0)

    cal = outcome.get("calibration", {})
    actual_loc = cal.get("actual_loc", 0)
    actual_duration = cal.get("actual_duration_seconds", 0)
    actual_tokens = cal.get("actual_tokens", 0)

    def pct(est, act):
        if est == 0:
            return "-"
        return f"{abs(act - est) / est * 100:.0f}%"

    body = (
        f"## Bundle complete — `{bundle_id}`\n\n"
        f"**Result:** {status_icon}\n\n"
        f"### What was done\n{summary}\n\n"
        f"### Calibration\n"
        f"| Axis | Estimated | Actual | Divergence |\n"
        f"|------|-----------|--------|------------|\n"
        f"| LOC  | {estimated_loc} | {actual_loc} | {pct(estimated_loc, actual_loc)} |\n"
        f"| Time | {_fmt_duration(estimated_duration)} | {_fmt_duration(actual_duration)} | {pct(estimated_duration, actual_duration)} |\n"
        f"| Tokens | {estimated_tokens} | {actual_tokens} | {pct(estimated_tokens, actual_tokens)} |\n"
    )
    return body


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"
