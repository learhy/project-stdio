"""SQLite database layer: connection pool, schema creation, migrations."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Awaitable

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 8


class DatabaseVersionError(RuntimeError):
    """Raised when the on-disk database schema version is ahead of the code version."""

    def __init__(self, stored: int, code: int) -> None:
        super().__init__(
            f"Database schema version {stored} is ahead of code version {code}. "
            "Upgrade the orchestrator."
        )
        self.stored = stored
        self.code = code

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bundles (
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
  github_issue_number INTEGER,
  irreversible INTEGER NOT NULL DEFAULT 0,
  cooldown_until INTEGER,
  tags TEXT
);

CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  node_id TEXT NOT NULL,
  token TEXT NOT NULL,
  token_expires_at INTEGER,
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

CREATE TABLE IF NOT EXISTS capabilities (
  id TEXT PRIMARY KEY,
  scope_json TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  granted_by TEXT NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER,
  revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS capability_requests (
  id TEXT PRIMARY KEY,
  bundle_id TEXT REFERENCES bundles(id),
  worker_id TEXT REFERENCES workers(id),
  requested_scope_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  decided_at INTEGER,
  decided_by TEXT
);

CREATE TABLE IF NOT EXISTS approval_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  decision TEXT NOT NULL,
  surface TEXT NOT NULL,
  actor TEXT NOT NULL,
  comment TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT REFERENCES workers(id),
  bundle_id TEXT REFERENCES bundles(id),
  requested_op TEXT NOT NULL,
  result TEXT NOT NULL,
  matched_capability_id TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  subject_type TEXT,
  subject_id TEXT,
  payload_json TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dag_nodes (
  id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  node_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  task_manifest_id TEXT,
  gate_config_json TEXT,
  aggregator_config_json TEXT,
  state TEXT NOT NULL,
  worker_id TEXT REFERENCES workers(id),
  ready_at INTEGER,
  started_at INTEGER,
  ended_at INTEGER,
  output_json TEXT,
  failure_reason TEXT,
  UNIQUE(bundle_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_dag_nodes_bundle_state ON dag_nodes(bundle_id, state);

CREATE TABLE IF NOT EXISTS dag_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  condition_kind TEXT NOT NULL,
  condition_expr TEXT,
  fired INTEGER DEFAULT 0,
  fired_at INTEGER,
  UNIQUE(bundle_id, from_node_id, to_node_id)
);

CREATE INDEX IF NOT EXISTS idx_dag_edges_to ON dag_edges(bundle_id, to_node_id);
CREATE INDEX IF NOT EXISTS idx_dag_edges_from ON dag_edges(bundle_id, from_node_id);

CREATE TABLE IF NOT EXISTS node_state_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL REFERENCES dag_nodes(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  at INTEGER NOT NULL,
  reason TEXT,
  event_id INTEGER
);

CREATE TABLE IF NOT EXISTS dag_expansions (
  id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  parent_node_id TEXT NOT NULL,
  graft_point_node_id TEXT NOT NULL,
  fragment_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL,
  requested_at INTEGER NOT NULL,
  decided_at INTEGER,
  decided_by TEXT,
  applied_at INTEGER
);

CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  context_json TEXT NOT NULL,
  state TEXT NOT NULL,
  decision TEXT,
  decided_at INTEGER,
  decided_by TEXT,
  decided_surface TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_metadata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL CHECK(namespace IN ('bundle', 'global', 'task')),
  name TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  content_type TEXT NOT NULL,
  hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  inline_data BLOB,
  producer_node_id TEXT,
  producer_worker_id TEXT,
  bundle_id TEXT,
  task_id TEXT,
  ref_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  published_at INTEGER NOT NULL,
  expires_at INTEGER,
  gc_eligible_at INTEGER,
  gc_d_at INTEGER,
  UNIQUE(namespace, name, version)
);

CREATE INDEX IF NOT EXISTS idx_artifact_metadata_hash ON artifact_metadata(hash);
CREATE INDEX IF NOT EXISTS idx_artifact_metadata_bundle ON artifact_metadata(bundle_id);
CREATE INDEX IF NOT EXISTS idx_artifact_metadata_ns_name ON artifact_metadata(namespace, name);
CREATE INDEX IF NOT EXISTS idx_artifact_metadata_gc ON artifact_metadata(gc_eligible_at)
    WHERE gc_eligible_at IS NOT NULL AND gc_d_at IS NULL;

CREATE TABLE IF NOT EXISTS artifact_refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  producer_node_id TEXT NOT NULL REFERENCES dag_nodes(id),
  descriptor_json TEXT NOT NULL,
  published_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_refs_descriptor ON artifact_refs(bundle_id, descriptor_json);
"""


# ── Migration registry (sequential, single global version) ──────────────────

MigrationFn = Callable[[aiosqlite.Connection], Awaitable[None]]
MIGRATIONS: dict[int, MigrationFn] = {}


def migration(target: int):
    """Decorator: register a migration function for the given target version."""
    def decorator(fn: MigrationFn) -> MigrationFn:
        MIGRATIONS[target] = fn
        return fn
    return decorator


@migration(2)
async def _migrate_v2(conn: aiosqlite.Connection) -> None:
    """Replace artifact_metadata with spec-compliant schema."""
    await conn.execute("DROP TABLE IF EXISTS artifact_metadata")
    await conn.executescript("""
        CREATE TABLE artifact_metadata (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          namespace TEXT NOT NULL CHECK(namespace IN ('bundle', 'global', 'task')),
          name TEXT NOT NULL,
          version TEXT NOT NULL DEFAULT '',
          content_type TEXT NOT NULL,
          hash TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          inline_data BLOB,
          producer_node_id TEXT,
          producer_worker_id TEXT,
          bundle_id TEXT,
          task_id TEXT,
          ref_count INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          published_at INTEGER NOT NULL,
          expires_at INTEGER,
          gc_eligible_at INTEGER,
          gc_d_at INTEGER,
          UNIQUE(namespace, name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_artifact_metadata_hash ON artifact_metadata(hash);
        CREATE INDEX IF NOT EXISTS idx_artifact_metadata_bundle ON artifact_metadata(bundle_id);
        CREATE INDEX IF NOT EXISTS idx_artifact_metadata_ns_name ON artifact_metadata(namespace, name);
        CREATE INDEX IF NOT EXISTS idx_artifact_metadata_gc ON artifact_metadata(gc_eligible_at)
            WHERE gc_eligible_at IS NOT NULL AND gc_d_at IS NULL;
    """)


@migration(3)
async def _migrate_v3(conn: aiosqlite.Connection) -> None:
    """Add github_issue_number column to bundles."""
    await conn.execute(
        "ALTER TABLE bundles ADD COLUMN github_issue_number INTEGER"
    )


@migration(4)
async def _migrate_v4(conn: aiosqlite.Connection) -> None:
    """Add approval matrix columns to bundles (irreversible, cooldown_until, tags)."""
    await conn.execute(
        "ALTER TABLE bundles ADD COLUMN irreversible INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE bundles ADD COLUMN cooldown_until INTEGER"
    )
    await conn.execute(
        "ALTER TABLE bundles ADD COLUMN tags TEXT"
    )


@migration(5)
async def _migrate_v5(conn: aiosqlite.Connection) -> None:
    """Drop the legacy schema_version table (replaced by PRAGMA user_version)."""
    await conn.execute("DROP TABLE IF EXISTS schema_version")


@migration(6)
async def _migrate_v6(conn: aiosqlite.Connection) -> None:
    """Add token_expires_at column to workers for time-limited tokens (Bundle 3.4)."""
    # Check if column already exists (SCHEMA_SQL may have created it on new DBs)
    cursor = await conn.execute("PRAGMA table_info('workers')")
    columns = {row[1] for row in await cursor.fetchall()}
    if "token_expires_at" not in columns:
        await conn.execute(
            "ALTER TABLE workers ADD COLUMN token_expires_at INTEGER"
        )


@migration(7)
async def _migrate_v7(conn: aiosqlite.Connection) -> None:
    """Create settings_metadata table for audit trail of feature flags (Bundle 4.1)."""
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at INTEGER NOT NULL
        );
    """)
    # Record current remote_workers_enabled state
    await conn.execute(
        "INSERT OR IGNORE INTO settings_metadata (key, value, updated_at) VALUES (?, ?, ?)",
        ("remote_workers_enabled", "0", 0),
    )


@migration(8)
async def _migrate_v8(conn: aiosqlite.Connection) -> None:
    """Record remote_fleet_enabled in settings_metadata (Bundle 4.2)."""
    await conn.execute(
        "INSERT OR IGNORE INTO settings_metadata (key, value, updated_at) VALUES (?, ?, ?)",
        ("remote_fleet_enabled", "0", 0),
    )


class Database:
    """Manages the SQLite connection pool and schema lifecycle."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        """Open the database connection, enable WAL, create schema, run migrations."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self._run_migrations()
        logger.info("Database connected: %s (WAL mode, schema v%s)", self.db_path, SCHEMA_VERSION)
        return self._conn

    async def _get_current_version(self) -> int:
        """Determine the current schema version of the on-disk database.

        Uses PRAGMA user_version as the authoritative source. Falls back to
        reading the legacy schema_version table for databases created before
        the PRAGMA-based versioning was introduced (Bundle 3.2).
        """
        row = await self._conn.execute("PRAGMA user_version")
        result = await row.fetchone()
        user_ver = result[0] if result else 0

        if user_ver > 0:
            return user_ver

        # user_version is 0 — could be an old DB that still uses the
        # schema_version table, or a brand-new empty database.
        try:
            row = await self._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            )
            stored = await row.fetchone()
            if stored is not None:
                return stored["version"]
        except aiosqlite.OperationalError:
            pass

        return 0

    async def _set_version(self, version: int) -> None:
        await self._conn.execute(f"PRAGMA user_version = {version}")

    async def _run_migrations(self) -> None:
        """Apply pending schema migrations in sequence.

        Raises DatabaseVersionError if the on-disk database is ahead of the
        code's SCHEMA_VERSION.
        """
        current = await self._get_current_version()

        if current == 0:
            # Brand-new database — SCHEMA_SQL just created everything.
            await self._set_version(SCHEMA_VERSION)
            await self._conn.commit()
            return

        if current > SCHEMA_VERSION:
            raise DatabaseVersionError(current, SCHEMA_VERSION)

        if current < SCHEMA_VERSION:
            for target in range(current + 1, SCHEMA_VERSION + 1):
                fn = MIGRATIONS.get(target)
                if fn is None:
                    logger.error(
                        "Missing migration for version %d (current=%d, code=%d)",
                        target, current, SCHEMA_VERSION,
                    )
                    raise RuntimeError(
                        f"No migration registered for version {target}"
                    )
                await fn(self._conn)
                await self._set_version(target)
                await self._conn.commit()
                logger.info("Applied migration v%d: %s", target, fn.__doc__ or fn.__name__)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple | dict | None = None) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params or ())

    async def execute_insert(self, sql: str, params: tuple | dict | None = None) -> int:
        cursor = await self.conn.execute(sql, params or ())
        await self.conn.commit()
        return cursor.lastrowid

    async def fetch_one(self, sql: str, params: tuple | dict | None = None) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(sql, params or ())
        return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: tuple | dict | None = None) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, params or ())
        return await cursor.fetchall()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager for explicit transactions.

        Usage:
            async with db.transaction():
                await db.execute(...)
        """
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            await self.conn.execute("ROLLBACK")
            raise
        else:
            await self.conn.execute("COMMIT")


async def create_database(db_path: str | Path) -> Database:
    """Factory: create and connect a Database instance."""
    db = Database(db_path)
    await db.connect()
    return db
