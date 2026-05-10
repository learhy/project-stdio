"""MCP resource URI handlers — all read-only, all from SQLite or memory/ tree."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite


async def _fetchall(db: aiosqlite.Connection, sql: str, params: tuple | None = None) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql, params or ())
    return await cursor.fetchall()


TERMINAL_STATES = {"complete", "failed", "rejected", "aborted"}


def _now() -> int:
    return int(time.time())


def _uri_segments(uri: str) -> list[str]:
    """Parse a studio:// URI into path segments."""
    # "studio://bundles/pending" → ["bundles", "pending"]
    # "studio://bundles/{id}/workers" → ["bundles", "{id}", "workers"]
    return uri.replace("studio://", "").strip("/").split("/")


async def handle_bundles_pending(db: aiosqlite.Connection) -> dict:
    rows = await _fetchall(db,
        "SELECT id, state, tier, repo, complexity_score, risk_score, "
        "proposal_json, created_at, approved_at FROM bundles "
        "WHERE state NOT IN ('complete','failed','rejected','aborted') "
        "ORDER BY created_at DESC LIMIT 50"
    )
    bundles = []
    for r in rows:
        title = ""
        try:
            pj = json.loads(r["proposal_json"] or "{}")
            bi = pj.get("bundle_input", {})
            title = bi.get("idea", bi.get("objective", ""))[:100]
        except Exception:
            pass
        bundles.append({
            "id": r["id"],
            "state": r["state"],
            "tier": r["tier"],
            "target": r["repo"],
            "complexity_score": r["complexity_score"],
            "risk_score": r["risk_score"],
            "title": title,
            "created_at": r["created_at"],
            "approved_at": r["approved_at"],
        })
    return {"bundles": bundles, "total": len(bundles), "truncated": False}


async def handle_bundle_detail(db: aiosqlite.Connection, bundle_id: str) -> dict:
    row = await _fetchall(db,
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    )
    if not row:
        return {"error": "NOT_FOUND", "detail": f"Bundle {bundle_id} does not exist"}
    b = row[0]

    # Gather workers
    workers = await _fetchall(db,
        "SELECT id, node_id, state, current_phase, created_at, started_at, "
        "last_heartbeat, ended_at FROM workers WHERE bundle_id = ?",
        (bundle_id,),
    )
    worker_list = [dict(w) for w in workers]

    # Gather DAG nodes
    nodes = await _fetchall(db,
        "SELECT node_id, kind, state, started_at, ended_at, output_json "
        "FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
    )
    node_list = [dict(n) for n in nodes]

    # Gather DAG edges
    edges = await _fetchall(db,
        "SELECT from_node_id, to_node_id, condition_kind, condition_expr, fired "
        "FROM dag_edges WHERE bundle_id = ?", (bundle_id,)
    )
    edge_list = [dict(e) for e in edges]

    return {
        "bundle": {
            "id": b["id"],
            "repo": b["repo"],
            "state": b["state"],
            "tier": b["tier"],
            "complexity_score": b["complexity_score"],
            "risk_score": b["risk_score"],
            "proposal_json": b["proposal_json"],
            "concerns_json": b["concerns_json"],
            "created_at": b["created_at"],
            "approved_at": b["approved_at"],
            "approved_by": b["approved_by"],
            "completed_at": b["completed_at"],
            "outcome_json": b["outcome_json"],
            "workers": worker_list,
            "dag_nodes": node_list,
            "dag_edges": edge_list,
        }
    }


async def handle_bundle_workers(db: aiosqlite.Connection, bundle_id: str) -> list[dict]:
    rows = await _fetchall(db,
        "SELECT id, node_id, state, current_phase, created_at, started_at, "
        "last_heartbeat, ended_at FROM workers WHERE bundle_id = ?",
        (bundle_id,),
    )
    return [dict(r) for r in rows]


async def handle_workers_active(db: aiosqlite.Connection) -> list[dict]:
    rows = await _fetchall(db,
        "SELECT id, bundle_id, node_id, state, current_phase, created_at, "
        "started_at, last_heartbeat FROM workers "
        "WHERE state IN ('running','paused') ORDER BY started_at DESC"
    )
    return [dict(r) for r in rows]


async def handle_worker_report(db: aiosqlite.Connection, bundle_id: str, worker_id: str) -> dict:
    # Look up the final report from dag_nodes.output_json matching this worker
    rows = await _fetchall(db,
        "SELECT output_json FROM dag_nodes WHERE bundle_id = ? AND worker_id = ?",
        (bundle_id, worker_id),
    )
    if rows and rows[0]["output_json"]:
        try:
            return json.loads(rows[0]["output_json"])
        except Exception:
            pass
    # Fallback: try by node_id from workers
    wrow = await _fetchall(db,
        "SELECT node_id FROM workers WHERE id = ?", (worker_id,)
    )
    if wrow:
        node_id = f"{bundle_id}:{wrow[0]['node_id']}"
        nrows = await _fetchall(db,
            "SELECT output_json FROM dag_nodes WHERE id = ?", (node_id,)
        )
        if nrows and nrows[0]["output_json"]:
            try:
                return json.loads(nrows[0]["output_json"])
            except Exception:
                pass
    return {"error": "NOT_FOUND", "detail": "No report found"}


async def handle_capabilities_manifest() -> dict:
    path = Path("memory/capabilities/manifest.md")
    if path.exists():
        return {"content": path.read_text(), "content_type": "text/markdown"}
    return {"content": "# Capability Manifest\n\nNot yet generated.\n", "content_type": "text/markdown"}


async def handle_capabilities_pending(db: aiosqlite.Connection) -> list[dict]:
    rows = await _fetchall(db,
        "SELECT id, bundle_id, worker_id, requested_scope_json, rationale, state, "
        "created_at FROM capability_requests WHERE state = 'pending' "
        "ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def handle_memory_agents(repo: str) -> dict:
    # Try memory/agents/<repo>.md first, then <repo>/AGENTS.md
    candidates = [
        Path(f"memory/agents/{repo}.md"),
        Path(f"memory/agents/{repo}/AGENTS.md"),
        Path("AGENTS.md"),
    ]
    for p in candidates:
        if p.exists():
            return {"content": p.read_text(), "content_type": "text/markdown"}
    return {"content": f"# AGENTS.md for {repo}\n\nNot found.\n", "content_type": "text/markdown"}


async def handle_calibration_recent() -> list[dict]:
    path = Path("memory/calibration/scoring-outcomes.jsonl")
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().strip().splitlines()[-30:]:
        if line.strip():
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return entries


async def handle_decisions_recent(db: aiosqlite.Connection) -> list[dict]:
    rows = await _fetchall(db,
        "SELECT id, bundle_id, decision, surface, actor, comment, created_at "
        "FROM approval_decisions ORDER BY created_at DESC LIMIT 50"
    )
    return [dict(r) for r in rows]


async def handle_system_status(db: aiosqlite.Connection) -> dict:
    now = _now()
    workers_active = await _fetchall(db,
        "SELECT COUNT(*) as cnt FROM workers WHERE state IN ('running','paused')"
    )
    bundles_pending = await _fetchall(db,
        "SELECT COUNT(*) as cnt FROM bundles WHERE state NOT IN ('complete','failed','rejected','aborted')"
    )
    bundles_in_progress = await _fetchall(db,
        "SELECT COUNT(*) as cnt FROM bundles WHERE state = 'in_progress'"
    )
    last_heartbeat = await _fetchall(db,
        "SELECT MAX(last_heartbeat) as ts FROM workers"
    )

    return {
        "status": "healthy",
        "timestamp": now,
        "workers_active": workers_active[0]["cnt"] if workers_active else 0,
        "bundles_pending": bundles_pending[0]["cnt"] if bundles_pending else 0,
        "bundles_in_progress": bundles_in_progress[0]["cnt"] if bundles_in_progress else 0,
        "last_worker_heartbeat": last_heartbeat[0]["ts"] if last_heartbeat else None,
    }


# ── URI router ──────────────────────────────────────────────────────────────────

async def route_resource(uri: str, db: aiosqlite.Connection) -> dict | list[dict] | str:
    """Route a studio:// URI to the correct handler. Returns JSON-serializable data."""
    if uri == "studio://bundles/pending":
        return await handle_bundles_pending(db)

    if uri.startswith("studio://bundles/") and uri.endswith("/workers"):
        # studio://bundles/{id}/workers
        parts = _uri_segments(uri)
        if len(parts) == 3:
            return await handle_bundle_workers(db, parts[1])

    if uri.startswith("studio://bundles/"):
        # studio://bundles/{id}
        parts = _uri_segments(uri)
        if len(parts) == 2:
            return await handle_bundle_detail(db, parts[1])

    if uri == "studio://workers/active":
        return await handle_workers_active(db)

    if uri.startswith("studio://workers/"):
        # studio://workers/{bundle_id}/{worker_id}/report
        parts = _uri_segments(uri)
        if len(parts) == 4 and parts[3] == "report":
            return await handle_worker_report(db, parts[1], parts[2])

    if uri == "studio://capabilities/manifest":
        return await handle_capabilities_manifest()

    if uri == "studio://capabilities/pending-requests":
        return await handle_capabilities_pending(db)

    if uri.startswith("studio://memory/agents/"):
        parts = _uri_segments(uri)
        if len(parts) == 3:
            return await handle_memory_agents(parts[2])

    if uri == "studio://calibration/recent":
        return await handle_calibration_recent()

    if uri == "studio://decisions/recent":
        return await handle_decisions_recent(db)

    if uri == "studio://system/status":
        return await handle_system_status(db)

    return {"error": "UNKNOWN_RESOURCE", "detail": f"No handler for URI: {uri}"}
