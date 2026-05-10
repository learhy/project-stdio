"""Tests for Bundle 2.7: MCP Server — tools, resources, and server."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

from studio.mcp.tools import (
    list_pending_bundles,
    get_bundle,
    grant_capability,
    revoke_capability,
    McpRpcClient,
)
from studio.mcp.resources import (
    route_resource,
    handle_bundles_pending,
    handle_system_status,
)


@pytest.fixture
async def db():
    """Create an in-memory SQLite database with the Studio schema."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    # Minimal schema
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript("""
        CREATE TABLE bundles (
          id TEXT PRIMARY KEY,
          repo TEXT NOT NULL,
          state TEXT NOT NULL,
          tier TEXT NOT NULL DEFAULT 'full_review',
          complexity_score INTEGER,
          risk_score INTEGER,
          proposal_json TEXT NOT NULL DEFAULT '{}',
          concerns_json TEXT,
          created_at INTEGER NOT NULL,
          approved_at INTEGER,
          approved_by TEXT,
          completed_at INTEGER,
          outcome_json TEXT
        );
        CREATE TABLE workers (
          id TEXT PRIMARY KEY,
          bundle_id TEXT NOT NULL,
          node_id TEXT NOT NULL,
          token TEXT NOT NULL,
          manifest_json TEXT NOT NULL DEFAULT '{}',
          state TEXT NOT NULL,
          pid INTEGER,
          current_phase TEXT,
          created_at INTEGER NOT NULL,
          started_at INTEGER,
          last_heartbeat INTEGER,
          ended_at INTEGER,
          exit_reason TEXT
        );
        CREATE TABLE dag_nodes (
          id TEXT PRIMARY KEY,
          bundle_id TEXT NOT NULL,
          node_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          spec_json TEXT NOT NULL,
          state TEXT NOT NULL,
          worker_id TEXT,
          started_at INTEGER,
          ended_at INTEGER,
          output_json TEXT
        );
        CREATE TABLE dag_edges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          bundle_id TEXT NOT NULL,
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          condition_kind TEXT NOT NULL,
          condition_expr TEXT,
          fired INTEGER DEFAULT 0
        );
        CREATE TABLE approval_decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          bundle_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          surface TEXT NOT NULL,
          actor TEXT NOT NULL,
          comment TEXT,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE capability_requests (
          id TEXT PRIMARY KEY,
          bundle_id TEXT,
          worker_id TEXT,
          requested_scope_json TEXT NOT NULL,
          rationale TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
    """)
    await conn.commit()
    yield conn
    await conn.close()


# ── Tools ──────────────────────────────────────────────────────────────────────

class TestListPendingBundles:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_bundles(self, db):
        result = await list_pending_bundles(db)
        assert result["bundles"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_returns_non_terminal_bundles(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "full_review", 1000),
        )
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b2", "test", "complete", "auto", 2000),
        )
        await db.commit()
        result = await list_pending_bundles(db)
        assert result["total"] == 1
        assert result["bundles"][0]["id"] == "b1"

    @pytest.mark.asyncio
    async def test_filter_by_tier(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "auto", 1000),
        )
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b2", "test", "proposed", "full_review", 2000),
        )
        await db.commit()
        result = await list_pending_bundles(db, {"tier": "auto"})
        assert result["total"] == 1
        assert result["bundles"][0]["id"] == "b1"

    @pytest.mark.asyncio
    async def test_filter_by_state(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "in_review", "auto", 1000),
        )
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b2", "test", "approved", "auto", 2000),
        )
        await db.commit()
        result = await list_pending_bundles(db, {"state": "in_review"})
        assert result["total"] == 1
        assert result["bundles"][0]["id"] == "b1"


class TestGetBundle:
    @pytest.mark.asyncio
    async def test_returns_bundle_with_related_data(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "full_review", 1000),
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("w1", "b1", "n1", "tok", "running", 1000),
        )
        await db.execute(
            "INSERT INTO dag_nodes (id, bundle_id, node_id, kind, spec_json, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("b1:n1", "b1", "n1", "worker", "{}", "running"),
        )
        await db.commit()
        result = await get_bundle(db, "b1")
        assert "error" not in result
        b = result["bundle"]
        assert b["id"] == "b1"
        assert b["state"] == "proposed"
        assert len(b["workers"]) == 1
        assert b["workers"][0]["id"] == "w1"
        assert len(b["dag_nodes"]) == 1

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        result = await get_bundle(db, "nonexistent")
        assert result["error"] == "NOT_FOUND"


class TestStubCapabilities:
    def test_grant_capability_is_stub(self):
        result = asyncio.run(grant_capability())
        assert result["error"] == "not_implemented"

    def test_revoke_capability_is_stub(self):
        result = asyncio.run(revoke_capability())
        assert result["error"] == "not_implemented"


# ── Resources ──────────────────────────────────────────────────────────────────

class TestResources:
    @pytest.mark.asyncio
    async def test_pending_resource_returns_list(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "full_review", 1000),
        )
        await db.commit()
        result = await handle_bundles_pending(db)
        assert "bundles" in result
        assert len(result["bundles"]) == 1

    @pytest.mark.asyncio
    async def test_route_bundles_pending(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "full_review", 1000),
        )
        await db.commit()
        result = await route_resource("studio://bundles/pending", db)
        assert result["bundles"][0]["id"] == "b1"

    @pytest.mark.asyncio
    async def test_route_bundle_detail(self, db):
        await db.execute(
            "INSERT INTO bundles (id, repo, state, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b1", "test", "proposed", "full_review", 1000),
        )
        await db.commit()
        result = await route_resource("studio://bundles/b1", db)
        assert result["bundle"]["id"] == "b1"

    @pytest.mark.asyncio
    async def test_route_bundle_not_found(self, db):
        result = await route_resource("studio://bundles/nonexistent", db)
        assert result["error"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_system_status(self, db):
        result = await handle_system_status(db)
        assert result["status"] == "healthy"
        assert "workers_active" in result
        assert "bundles_pending" in result

    @pytest.mark.asyncio
    async def test_route_unknown_uri(self, db):
        result = await route_resource("studio://unknown/thing", db)
        assert result["error"] == "UNKNOWN_RESOURCE"


# ── RPC Client ─────────────────────────────────────────────────────────────────

class TestMcpRpcClient:
    @pytest.mark.asyncio
    async def test_connect_sends_mcp_role_auth(self):
        client = McpRpcClient("/tmp/test.sock")
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "result": {"bound": True, "role": "mcp"}, "id": 0
        }).encode())
        with patch("asyncio.open_unix_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))):
            await client.connect()
            # Verify auth message was sent
            data_sent = mock_writer.write.call_args[0][0].decode()
            auth = json.loads(data_sent)
            assert auth["method"] == "auth"
            assert auth["params"]["role"] == "mcp"

    @pytest.mark.asyncio
    async def test_call_sends_jsonrpc_message(self):
        client = McpRpcClient("/tmp/test.sock")
        client.reader = AsyncMock()
        client.writer = AsyncMock()
        client.reader.readline = AsyncMock(return_value=json.dumps({
            "jsonrpc": "2.0", "result": {"ok": True}, "id": 1
        }).encode())

        result = await client.call("mcp.approve_bundle", {"id": "b1"})
        assert result["result"]["ok"] is True


# ── Server settings ────────────────────────────────────────────────────────────

class TestServerSettings:
    def test_default_settings_when_no_file(self):
        from studio.mcp.server import _load_settings
        with patch("pathlib.Path.exists", return_value=False):
            s = _load_settings()
            assert s["port"] == 8080
            assert s["bearer_token"] == ""

    def test_loads_mcp_section(self):
        import tempfile
        import studio.mcp.server as srv
        td = tempfile.mkdtemp()
        import os
        settings_path = os.path.join(td, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"mcp": {"port": 9090, "bearer_token": "secret"}}, f)
        old_cwd = os.getcwd()
        try:
            os.chdir(td)
            # Force reload of settings
            s = srv._load_settings()
            assert s["port"] == 9090
            assert s["bearer_token"] == "secret"
        finally:
            os.chdir(old_cwd)
