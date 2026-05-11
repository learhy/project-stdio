"""Tests for db.py — schema creation and connection lifecycle."""
import pytest
from studio.orchestrator.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_connect_creates_file(db):
    assert db.db_path.exists()


@pytest.mark.asyncio
async def test_wal_mode_enabled(db):
    row = await db.fetch_one("PRAGMA journal_mode")
    assert row[0].upper() == "WAL"


@pytest.mark.asyncio
async def test_foreign_keys_enabled(db):
    row = await db.fetch_one("PRAGMA foreign_keys")
    assert row[0] == 1


@pytest.mark.asyncio
async def test_all_tables_exist(db):
    tables = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_names = {r["name"] for r in tables}
    expected = {
        "bundles", "workers", "capabilities", "capability_requests",
        "approval_decisions", "capability_checks", "audit_log",
        "dag_nodes", "dag_edges", "node_state_history",
        "dag_expansions", "approval_requests", "artifact_refs",
    }
    assert expected.issubset(table_names)


@pytest.mark.asyncio
async def test_insert_and_fetch(db):
    import time
    now = int(time.time())
    await db.execute_insert(
        "INSERT INTO bundles (id, repo, state, tier, proposal_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("01JTEST", "control-plane", "proposed", "full_review", "{}", now),
    )
    row = await db.fetch_one("SELECT * FROM bundles WHERE id = ?", ("01JTEST",))
    assert row is not None
    assert row["state"] == "proposed"
    assert row["repo"] == "control-plane"


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(db):
    import time
    now = int(time.time())
    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO bundles (id, repo, state, tier, proposal_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("01JTXN", "control-plane", "proposed", "full_review", "{}", now),
            )
            raise RuntimeError("forced error")
    except RuntimeError:
        pass

    row = await db.fetch_one("SELECT * FROM bundles WHERE id = ?", ("01JTXN",))
    assert row is None
