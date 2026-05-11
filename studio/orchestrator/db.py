"""SQLite database layer: connection pool, schema creation, migrations."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

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

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);
"""


class Database:
    """Manages the SQLite connection pool and schema lifecycle."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        """Open the database connection, enable WAL, create schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL and foreign keys via pragmas, then run schema
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self._run_migrations()
        logger.info("Database connected: %s (WAL mode)", self.db_path)
        return self._conn

    async def _run_migrations(self) -> None:
        """Apply schema migrations based on stored version vs current SCHEMA_VERSION."""
        row = await self._conn.execute("SELECT version FROM schema_version")
        stored = await row.fetchone()

        if stored is None:
            # Brand new database — SCHEMA_SQL just created everything at current version.
            await self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            await self._conn.commit()
            return

        current_version = stored["version"]

        if current_version < 2:
            # Migration v2: Replace old artifact_metadata with spec-compliant schema.
            await self._conn.execute("DROP TABLE IF EXISTS artifact_metadata")
            await self._conn.executescript("""
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
            await self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (2,)
            )
            await self._conn.commit()
            logger.info("Applied migration v2: artifact_metadata schema updated")

        if current_version < 3:
            await self._conn.execute(
                "ALTER TABLE bundles ADD COLUMN github_issue_number INTEGER"
            )
            await self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (3,)
            )
            await self._conn.commit()
            logger.info("Applied migration v3: github_issue_number column added")

        if current_version < 4:
            # Migration v4: approval matrix columns + drop legacy schema_version table.
            await self._conn.execute(
                "ALTER TABLE bundles ADD COLUMN irreversible INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.execute(
                "ALTER TABLE bundles ADD COLUMN cooldown_until INTEGER"
            )
            await self._conn.execute(
                "ALTER TABLE bundles ADD COLUMN tags TEXT"
            )
            await self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (4,)
            )
            await self._conn.commit()
            logger.info("Applied migration v4: approval matrix columns added")

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
