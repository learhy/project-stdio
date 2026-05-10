"""MCP tool implementations — reads from SQLite directly, mutations over RPC to orchestrator."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite


async def _fetchall(db: aiosqlite.Connection, sql: str, params: tuple | None = None) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql, params or ())
    return await cursor.fetchall()


class McpRpcClient:
    """JSON-RPC 2.0 client to the orchestrator — connects as MCP role."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._req_id = 0

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        # Authenticate as MCP system role
        auth_msg = {
            "jsonrpc": "2.0",
            "method": "auth",
            "params": {"role": "mcp"},
            "id": 0,
        }
        self.writer.write((json.dumps(auth_msg) + "\n").encode())
        await self.writer.drain()
        line = await self.reader.readline()
        if not line:
            raise RuntimeError("Orchestrator closed connection during MCP auth")
        resp = json.loads(line.decode("utf-8"))
        if "error" in resp:
            raise RuntimeError(f"MCP auth rejected: {resp['error']}")
        if not resp.get("result", {}).get("bound"):
            raise RuntimeError("MCP auth not bound by orchestrator")

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._req_id,
        }
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()
        line = await self.reader.readline()
        if not line:
            return {"error": {"code": -1, "message": "Connection closed"}}
        return json.loads(line.decode("utf-8"))


# ── Tool implementations ────────────────────────────────────────────────────────


async def list_pending_bundles(
    db: aiosqlite.Connection, filter_args: dict | None = None
) -> dict:
    """List pending bundles with optional filters."""
    filter_args = filter_args or {}
    tier = filter_args.get("tier")
    state = filter_args.get("state")
    repo = filter_args.get("repo")
    limit = min(filter_args.get("limit", 20), 100)

    query = (
        "SELECT id, state, tier, repo, complexity_score, risk_score, "
        "proposal_json, created_at, approved_at FROM bundles "
        "WHERE state NOT IN ('complete','failed','rejected','aborted')"
    )
    params: list[Any] = []

    if tier:
        query += " AND tier = ?"
        params.append(tier)
    if state:
        query += " AND state = ?"
        params.append(state)
    if repo:
        query += " AND repo = ?"
        params.append(repo)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = await _fetchall(db,query, tuple(params))
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

    total = len(bundles)
    return {"bundles": bundles, "total": total, "truncated": total >= limit}


async def get_bundle(db: aiosqlite.Connection, bundle_id: str) -> dict:
    """Get a single bundle by ID with all related data."""
    row = await _fetchall(db,
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    )
    if not row:
        return {"error": "NOT_FOUND", "detail": f"Bundle {bundle_id} does not exist"}
    b = row[0]

    workers = await _fetchall(db,
        "SELECT id, node_id, state, current_phase, created_at, started_at, "
        "last_heartbeat, ended_at FROM workers WHERE bundle_id = ?",
        (bundle_id,),
    )
    nodes = await _fetchall(db,
        "SELECT node_id, kind, state, started_at, ended_at, output_json "
        "FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
    )
    edges = await _fetchall(db,
        "SELECT from_node_id, to_node_id, condition_kind, condition_expr, fired "
        "FROM dag_edges WHERE bundle_id = ?", (bundle_id,)
    )

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
            "workers": [dict(w) for w in workers],
            "dag_nodes": [dict(n) for n in nodes],
            "dag_edges": [dict(e) for e in edges],
        }
    }


async def approve_bundle(rpc: McpRpcClient, bundle_id: str, comment: str = "") -> dict:
    """Approve a bundle via MCP RPC."""
    resp = await rpc.call("mcp.approve_bundle", {"id": bundle_id, "comment": comment})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def reject_bundle(rpc: McpRpcClient, bundle_id: str, reason: str) -> dict:
    """Reject a bundle via MCP RPC."""
    resp = await rpc.call("mcp.reject_bundle", {"id": bundle_id, "reason": reason})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def request_modification(rpc: McpRpcClient, bundle_id: str, instructions: str) -> dict:
    """Request modification of a bundle via MCP RPC."""
    resp = await rpc.call("mcp.request_modification", {"id": bundle_id, "instructions": instructions})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def escalate_bundle(rpc: McpRpcClient, bundle_id: str, reason: str) -> dict:
    """Escalate a bundle to the next tier via MCP RPC."""
    resp = await rpc.call("mcp.escalate_bundle", {"id": bundle_id, "reason": reason})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def pause_bundle(rpc: McpRpcClient, bundle_id: str) -> dict:
    """Pause a bundle via MCP RPC."""
    resp = await rpc.call("mcp.pause_bundle", {"id": bundle_id})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def resume_bundle(rpc: McpRpcClient, bundle_id: str, note: str = "") -> dict:
    """Resume a bundle via MCP RPC."""
    resp = await rpc.call("mcp.resume_bundle", {"id": bundle_id, "note": note})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def kill_worker(rpc: McpRpcClient, bundle_id: str, worker_id: str, reason: str) -> dict:
    """Kill a worker via MCP RPC."""
    resp = await rpc.call("mcp.kill_worker",
                          {"bundle_id": bundle_id, "worker_id": worker_id, "reason": reason})
    if "error" in resp:
        return resp
    return resp.get("result", resp)


async def grant_capability() -> dict:
    """Stub — capability grant/revoke deferred to Phase 3."""
    return {"error": "not_implemented", "detail": "Capability grant/revoke deferred to Phase 3."}


async def revoke_capability() -> dict:
    """Stub — capability grant/revoke deferred to Phase 3."""
    return {"error": "not_implemented", "detail": "Capability grant/revoke deferred to Phase 3."}
