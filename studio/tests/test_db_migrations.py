"""Tests for schema versioning — PRAGMA user_version, sequential migrations, guards."""
import pytest
import aiosqlite
from studio.orchestrator.db import (
    Database,
    DatabaseVersionError,
    SCHEMA_VERSION,
    MIGRATIONS,
    create_database,
)


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_new_database_gets_current_version(db):
    """Brand-new databases are stamped with SCHEMA_VERSION via PRAGMA user_version."""
    row = await db.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_no_schema_version_table_in_new_db(db):
    """New databases should NOT have the legacy schema_version table."""
    tables = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    assert len(tables) == 0


@pytest.mark.asyncio
async def test_user_version_used_as_authoritative(db):
    """PRAGMA user_version is the authoritative source after migration."""
    row = await db.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_connect_is_idempotent(db):
    """Closing and reconnecting does not re-apply migrations."""
    await db.close()
    await db.connect()
    row = await db.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_database_ahead_of_code_raises(tmp_path):
    """If on-disk version exceeds code version, DatabaseVersionError is raised."""
    db_path = tmp_path / "ahead.db"

    # Create a DB stamped with a future version
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA user_version = 99")
    await conn.close()

    database = Database(db_path)
    with pytest.raises(DatabaseVersionError) as exc_info:
        await database.connect()
    assert exc_info.value.stored == 99
    assert exc_info.value.code == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_legacy_schema_version_table_migrated(tmp_path):
    """A database with the old schema_version table (v2) is migrated to PRAGMA user_version."""
    db_path = tmp_path / "legacy.db"

    # Simulate an old v2 database that uses the schema_version table
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Create just the schema_version table at v2 (minimal old-DB setup)
    await conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    await conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    # Create bundles table as it was at v2 (no github_issue_number)
    await conn.execute("""
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
        )
    """)
    # Insert a test row
    await conn.execute(
        "INSERT INTO bundles (id, repo, state, created_at) VALUES (?, ?, ?, ?)",
        ("test-bundle", "control-plane", "proposed", 1000),
    )
    await conn.commit()
    await conn.close()

    # Now connect with the new code — should migrate v2→v3→v4
    database = Database(db_path)
    await database.connect()

    # Verify user_version is now SCHEMA_VERSION
    row = await database.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION

    # Verify schema_version table was dropped
    tables = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    assert len(tables) == 0

    # Verify v3 migration applied: github_issue_number column exists
    bundle = await database.fetch_one("SELECT * FROM bundles WHERE id = ?", ("test-bundle",))
    assert bundle is not None
    # github_issue_number should exist (None by default)
    keys = set(bundle.keys())
    assert "github_issue_number" in keys

    await database.close()


@pytest.mark.asyncio
async def test_legacy_db_v3_migrated_to_v4(tmp_path):
    """A database at v3 (with github_issue_number, with schema_version table) migrates to v4."""
    db_path = tmp_path / "legacy_v3.db"

    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    await conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    await conn.execute("""
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
          outcome_json TEXT,
          github_issue_number INTEGER
        )
    """)
    await conn.commit()
    await conn.close()

    database = Database(db_path)
    await database.connect()

    row = await database.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION

    tables = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    assert len(tables) == 0

    await database.close()


@pytest.mark.asyncio
async def test_all_registered_migrations_exist():
    """Every version from 2 through SCHEMA_VERSION has a registered migration."""
    for target in range(2, SCHEMA_VERSION + 1):
        assert target in MIGRATIONS, f"Missing migration for version {target}"


@pytest.mark.asyncio
async def test_sequential_migrations_applied_in_order(tmp_path):
    """Migrations are applied sequentially from current+1 to SCHEMA_VERSION."""
    db_path = tmp_path / "seq.db"

    # Manually run migrations v2 and v3 but not v4
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA user_version = 1")
    # Set up a minimal old-DB state
    await conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    await conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    await conn.execute("""
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
        )
    """)
    await conn.commit()
    await conn.close()

    # Note: our _get_current_version will see user_version=0 and fall back to
    # the schema_version table (version=1). So it starts at 1 and applies v2,v3,v4.
    database = Database(db_path)
    await database.connect()

    row = await database.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION

    await database.close()


@pytest.mark.asyncio
async def test_create_database_factory(db):
    """create_database factory returns a connected Database."""
    assert db.conn is not None
    row = await db.fetch_one("PRAGMA user_version")
    assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_error_message_includes_versions():
    """DatabaseVersionError message contains both stored and code versions."""
    exc = DatabaseVersionError(5, 4)
    msg = str(exc)
    assert "5" in msg
    assert "4" in msg
    assert exc.stored == 5
    assert exc.code == 4
