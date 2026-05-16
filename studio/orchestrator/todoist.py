"""Todoist REST API v2 integration for PM task creation (Bundle 6.4).

Called from _record_calibration when code quality thresholds are exceeded.
Requires TODOIST_API_TOKEN env var; skips silently if not set.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TODOIST_API_BASE = "https://api.todoist.com/rest/v2"


def _auth_headers() -> dict[str, str] | None:
    token = os.environ.get("TODOIST_API_TOKEN", "")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


async def create_review_task(
    bundle_id: str,
    bundle_idea: str,
    attempts: int,
    qa_pass_rate: float | None,
    artifact_type: str,
) -> str | None:
    """Create a Todoist task for PM review when quality thresholds are exceeded."""
    headers = _auth_headers()
    if headers is None:
        logger.debug("TODOIST_API_TOKEN not set, skipping Todoist task creation")
        return None

    qa_pct = f"{round(qa_pass_rate * 100)}%" if qa_pass_rate is not None else "N/A"

    content = (
        f"Review: {bundle_idea} needed {attempts} attempts and "
        f"scored {qa_pct} on QA criteria. "
        f"Consider improving the spec template for {artifact_type} bundles."
    )

    payload: dict = {
        "content": content,
        "description": (
            f"Bundle: {bundle_id}\n"
            f"Developer attempts: {attempts}\n"
            f"QA criterion pass rate: {qa_pct}\n"
            f"Artifact type: {artifact_type}"
        ),
        "priority": 2,
    }

    project_id = os.environ.get("TODOIST_PROJECT_ID", "")
    if project_id:
        payload["project_id"] = project_id

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{_TODOIST_API_BASE}/tasks",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("id", "")
            logger.info("Todoist task created: %s for bundle %s", task_id, bundle_id)
            return task_id
    except Exception as exc:
        logger.warning("Failed to create Todoist task for bundle %s: %s", bundle_id, exc)
        return None
