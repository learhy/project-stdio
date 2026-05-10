"""MCP server process — exposes Studio tools, resources, and prompts via MCP.

Runs as a separate process alongside the orchestrator. Connects to SQLite for reads
and to the orchestrator's Unix socket for state mutations.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiosqlite
import mcp.types as types
from mcp.server.fastmcp import FastMCP

from .tools import McpRpcClient, list_pending_bundles, get_bundle, approve_bundle, \
    reject_bundle, request_modification, escalate_bundle, pause_bundle, resume_bundle, \
    kill_worker, grant_capability, revoke_capability
from .resources import route_resource


async def _fetchall(db: aiosqlite.Connection, sql: str, params: tuple | None = None) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql, params or ())
    return await cursor.fetchall()


def _load_settings() -> dict:
    """Load settings.json and return the mcp section."""
    settings_path = Path("settings.json")
    if not settings_path.exists():
        return {"port": 8080, "bearer_token": ""}
    data = json.loads(settings_path.read_text())
    return data.get("mcp", {"port": 8080, "bearer_token": ""})


class StudioMcpServer(FastMCP):
    """FastMCP subclass with Studio tools, resources, and prompts."""

    def __init__(self, settings: dict) -> None:
        port = settings.get("port", 8080)
        super().__init__("studio-mcp", host="127.0.0.1", port=port)
        self._settings = settings
        self._db: aiosqlite.Connection | None = None
        self._rpc: McpRpcClient | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            db_path = os.environ.get("STUDIO_DB_PATH", "/var/lib/studio/state.db")
            self._db = await aiosqlite.connect(db_path)
            self._db.row_factory = aiosqlite.Row
        return self._db

    async def _get_rpc(self) -> McpRpcClient:
        if self._rpc is None:
            socket_path = os.environ.get("STUDIO_SOCKET_PATH", "/run/studio/orchestrator.sock")
            self._rpc = McpRpcClient(socket_path)
            await self._rpc.connect()
        return self._rpc

    # ── Tools ────────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[types.Tool]:
        return [
            types.Tool(
                name="list_pending_bundles",
                description="List bundles not in a terminal state. Optional filter by tier, state, repo.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "object",
                            "properties": {
                                "tier": {"type": "string"},
                                "state": {"type": "string"},
                                "repo": {"type": "string"},
                                "limit": {"type": "integer", "default": 20, "maximum": 100},
                            },
                        }
                    },
                },
            ),
            types.Tool(
                name="get_bundle",
                description="Get full details for a bundle by ID.",
                inputSchema={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            ),
            types.Tool(
                name="approve_bundle",
                description="Approve a bundle. Requires explicit human confirmation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "comment": {"type": "string"},
                    },
                    "required": ["id"],
                },
            ),
            types.Tool(
                name="reject_bundle",
                description="Reject a bundle with a reason. Requires explicit human confirmation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            ),
            types.Tool(
                name="request_modification",
                description="Request modifications to a bundle. Sends it back for revision.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "instructions": {"type": "string"},
                    },
                    "required": ["id", "instructions"],
                },
            ),
            types.Tool(
                name="escalate_bundle",
                description="Escalate a bundle to the next higher review tier.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            ),
            types.Tool(
                name="pause_bundle",
                description="Pause a bundle that is currently in progress.",
                inputSchema={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            ),
            types.Tool(
                name="resume_bundle",
                description="Resume a paused bundle.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["id"],
                },
            ),
            types.Tool(
                name="kill_worker",
                description="Kill a specific worker in a bundle.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bundle_id": {"type": "string"},
                        "worker_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["bundle_id", "worker_id", "reason"],
                },
            ),
            types.Tool(
                name="grant_capability",
                description="Grant a capability request. [DEFERRED to Phase 3]",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "scope": {"type": "object"},
                        "expiry": {"type": "string"},
                    },
                    "required": ["request_id"],
                },
            ),
            types.Tool(
                name="revoke_capability",
                description="Revoke a granted capability. [DEFERRED to Phase 3]",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["capability_id", "reason"],
                },
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        db = await self._get_db()
        result = None

        if name == "list_pending_bundles":
            result = await list_pending_bundles(db, arguments.get("filter"))
        elif name == "get_bundle":
            result = await get_bundle(db, arguments["id"])
        elif name == "approve_bundle":
            rpc = await self._get_rpc()
            result = await approve_bundle(rpc, arguments["id"], arguments.get("comment", ""))
        elif name == "reject_bundle":
            rpc = await self._get_rpc()
            result = await reject_bundle(rpc, arguments["id"], arguments["reason"])
        elif name == "request_modification":
            rpc = await self._get_rpc()
            result = await request_modification(rpc, arguments["id"], arguments["instructions"])
        elif name == "escalate_bundle":
            rpc = await self._get_rpc()
            result = await escalate_bundle(rpc, arguments["id"], arguments.get("reason", ""))
        elif name == "pause_bundle":
            rpc = await self._get_rpc()
            result = await pause_bundle(rpc, arguments["id"])
        elif name == "resume_bundle":
            rpc = await self._get_rpc()
            result = await resume_bundle(rpc, arguments["id"], arguments.get("note", ""))
        elif name == "kill_worker":
            rpc = await self._get_rpc()
            result = await kill_worker(rpc, arguments["bundle_id"],
                                       arguments["worker_id"], arguments.get("reason", ""))
        elif name == "grant_capability":
            result = await grant_capability()
        elif name == "revoke_capability":
            result = await revoke_capability()
        else:
            result = {"error": "UNKNOWN_TOOL", "detail": f"Unknown tool: {name}"}

        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    # ── Resources ────────────────────────────────────────────────────────────

    async def list_resources(self) -> list[types.Resource]:
        return [
            types.Resource(uri="studio://bundles/pending", name="Pending Bundles",
                           description="All bundles not in a terminal state", mimeType="application/json"),
            types.Resource(uri="studio://bundles/{id}", name="Bundle Detail",
                           description="Full bundle output for a specific bundle", mimeType="application/json"),
            types.Resource(uri="studio://bundles/{id}/workers", name="Bundle Workers",
                           description="Workers assigned to a bundle", mimeType="application/json"),
            types.Resource(uri="studio://workers/active", name="Active Workers",
                           description="All workers in running or paused state", mimeType="application/json"),
            types.Resource(uri="studio://workers/{bundle_id}/{worker_id}/report", name="Worker Report",
                           description="Final report for a specific worker", mimeType="application/json"),
            types.Resource(uri="studio://capabilities/manifest", name="Capability Manifest",
                           description="Current system capability source of truth", mimeType="text/markdown"),
            types.Resource(uri="studio://capabilities/pending-requests", name="Pending Capability Requests",
                           description="Pending capability requests with status", mimeType="application/json"),
            types.Resource(uri="studio://memory/agents/{repo}", name="AGENTS.md",
                           description="AGENTS.md content for a named repo", mimeType="text/markdown"),
            types.Resource(uri="studio://calibration/recent", name="Recent Calibration",
                           description="Last 30 days of calibration data", mimeType="application/json"),
            types.Resource(uri="studio://decisions/recent", name="Recent Decisions",
                           description="Last 30 days of approval decisions", mimeType="application/json"),
            types.Resource(uri="studio://system/status", name="System Status",
                           description="Orchestrator health, worker pool stats", mimeType="application/json"),
        ]

    async def read_resource(self, uri: types.AnyUrl) -> str:
        db = await self._get_db()
        result = await route_resource(str(uri), db)
        if isinstance(result, dict) and result.get("content_type") == "text/markdown":
            return result.get("content", "")
        return json.dumps(result, indent=2, default=str)

    # ── Prompts ──────────────────────────────────────────────────────────────

    async def list_prompts(self) -> list[types.Prompt]:
        return [
            types.Prompt(
                name="review-pending",
                description="Summarize pending bundles and recommend approve/reject/modify for each.",
                arguments=[],
            ),
            types.Prompt(
                name="morning-digest",
                description="Summarize overnight activity: completed bundles, new proposals, calibration alerts.",
                arguments=[],
            ),
            types.Prompt(
                name="risk-audit",
                description="Audit recent bundles targeting a repo for risk scoring patterns and failure modes.",
                arguments=[types.PromptArgument(name="repo", description="Repository to audit", required=True)],
            ),
            types.Prompt(
                name="bundle-deep-dive",
                description="Deep review of a bundle: RFC, verification plan, review findings.",
                arguments=[types.PromptArgument(name="id", description="Bundle ID to review", required=True)],
            ),
        ]

    async def get_prompt(self, name: str, arguments: dict | None) -> types.GetPromptResult:
        db = await self._get_db()
        arguments = arguments or {}

        if name == "review-pending":
            pending = await list_pending_bundles(db)
            text = (
                "You are reviewing Studio bundles on behalf of the PM. "
                "Here are the pending bundles:\n\n"
                + json.dumps(pending.get("bundles", []), indent=2, default=str)
                + "\n\nFor each bundle, summarize the proposal, flag any concerns, "
                "and recommend approve, reject, or request modification."
            )
            return types.GetPromptResult(
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))]
            )

        elif name == "morning-digest":
            pending = await list_pending_bundles(db)
            recent = await _fetchall(db,
                "SELECT id, state, tier, created_at FROM bundles ORDER BY created_at DESC LIMIT 20"
            )
            completed = [dict(r) for r in recent if r["state"] in ("complete", "failed")]
            text = (
                "You are the PM's morning digest. Here is what happened:\n\n"
                "## Completed/Failed\n" + json.dumps(completed, indent=2, default=str) +
                "\n\n## Pending\n" + json.dumps(pending.get("bundles", []), indent=2, default=str) +
                "\n\nSummarize the overnight activity: completed bundles, new proposals, calibration alerts."
            )
            return types.GetPromptResult(
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))]
            )

        elif name == "risk-audit":
            repo = arguments.get("repo", "")
            rows = await _fetchall(db,
                "SELECT id, state, tier, complexity_score, risk_score, proposal_json, created_at "
                "FROM bundles WHERE repo = ? ORDER BY created_at DESC LIMIT 50",
                (repo,),
            )
            text = (
                f"Audit recent bundles targeting repo '{repo}'. "
                "Identify patterns in risk scoring, failure modes, and security findings.\n\n"
                + json.dumps([dict(r) for r in rows], indent=2, default=str)
            )
            return types.GetPromptResult(
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))]
            )

        elif name == "bundle-deep-dive":
            bundle_id = arguments.get("id", "")
            bundle = await get_bundle(db, bundle_id)
            text = (
                f"Deep review of bundle {bundle_id}. Read the RFC, verification plan, "
                "worker decomposition, and all review track findings. "
                "Flag anything the automated review may have missed.\n\n"
                + json.dumps(bundle, indent=2, default=str)
            )
            return types.GetPromptResult(
                messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))]
            )

        else:
            raise ValueError(f"Unknown prompt: {name}")


# ── Entry point ─────────────────────────────────────────────────────────────────


async def _run_server() -> None:
    settings = _load_settings()
    port = settings.get("port", 8080)
    server = StudioMcpServer(settings)
    print(f"[studio-mcp] Starting on port {port}", file=sys.stderr, flush=True)
    # Connect DB and RPC eagerly so they're ready before first request
    await server._get_db()
    try:
        await server._get_rpc()
        print(f"[studio-mcp] Connected to orchestrator", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[studio-mcp] WARNING: Could not connect to orchestrator: {exc}", file=sys.stderr, flush=True)
        print(f"[studio-mcp] Read-only mode — mutation tools will fail", file=sys.stderr, flush=True)
    server.run(transport="streamable-http")


def main() -> None:
    asyncio.run(_run_server())


if __name__ == "__main__":
    main()
