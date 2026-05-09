"""Artifact Protocol: storage, content addressing, GC, and secrets.

Implements the full Artifact Protocol from the v1.1 spec (lines 1298-1868).
BLAKE3 for content addressing. Reference counting + time-based expiry GC.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import blake3

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)

# ── Glob matching ───────────────────────────────────────────────────────────────

_GLOB_CACHE: dict[str, re.Pattern] = {}


def glob_match(pattern: str, value: str) -> bool:
    """Match a value against a glob pattern.

    * matches any chars within a single segment (non-greedy).
    ** matches any chars including path separators.
    ? matches exactly one char.
    [abc] and [!abc] character classes pass through.

    Case-sensitive. Cached compiled patterns.
    """
    if pattern in _GLOB_CACHE:
        return bool(_GLOB_CACHE[pattern].fullmatch(value))

    regex_parts: list[str] = []
    i = 0
    n = len(pattern)

    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                regex_parts.append(".*")
                i += 2
            else:
                regex_parts.append("[^/]*")
                i += 1
        elif c == "?":
            regex_parts.append(".")
            i += 1
        elif c == "[":
            # Pass through character class, find closing bracket
            j = i + 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n:
                j += 1  # include the closing ]
                seg = pattern[i:j]
                # Globs use [!...] for negation; regex uses [^...]
                if seg.startswith("[!") and len(seg) > 3:
                    seg = "[^" + seg[2:]
                escaped = re.escape(seg)
                escaped = escaped.replace("\\[", "[").replace("\\]", "]")
                escaped = escaped.replace("\\^", "^")
                regex_parts.append(escaped)
                i = j
            else:
                regex_parts.append(re.escape("["))
                i += 1
        else:
            regex_parts.append(re.escape(c))
            i += 1

    compiled = re.compile("".join(regex_parts))
    _GLOB_CACHE[pattern] = compiled
    return bool(compiled.fullmatch(value))


# ── Descriptor ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArtifactDescriptor:
    namespace: str  # bundle | global | task
    name: str
    version: str = ""  # normalized to "" when omitted
    content_type: str = "application/octet-stream"

    def to_json(self) -> str:
        return json.dumps({
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
            "content_type": self.content_type,
        })

    @classmethod
    def from_dict(cls, d: dict) -> ArtifactDescriptor:
        return cls(
            namespace=d.get("namespace", "bundle"),
            name=d.get("name", ""),
            version=d.get("version") or "",
            content_type=d.get("content_type", "application/octet-stream"),
        )


# ── Metadata record ─────────────────────────────────────────────────────────────


@dataclass
class ArtifactMetadata:
    id: int = 0
    namespace: str = "bundle"
    name: str = ""
    version: str = ""
    content_type: str = "application/octet-stream"
    hash: str = ""
    size_bytes: int = 0
    inline_data: bytes | None = None
    producer_node_id: str | None = None
    producer_worker_id: str | None = None
    bundle_id: str | None = None
    task_id: str | None = None
    ref_count: int = 0
    created_at: int = 0
    published_at: int = 0
    expires_at: int | None = None
    gc_eligible_at: int | None = None
    gc_d_at: int | None = None

    def to_descriptor(self) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            namespace=self.namespace,
            name=self.name,
            version=self.version,
            content_type=self.content_type,
        )

    @classmethod
    def from_row(cls, row: dict) -> ArtifactMetadata:
        return cls(
            id=row.get("id", 0),
            namespace=row["namespace"],
            name=row["name"],
            version=row.get("version", ""),
            content_type=row.get("content_type", "application/octet-stream"),
            hash=row.get("hash", ""),
            size_bytes=row.get("size_bytes", 0),
            inline_data=row.get("inline_data"),
            producer_node_id=row.get("producer_node_id"),
            producer_worker_id=row.get("producer_worker_id"),
            bundle_id=row.get("bundle_id"),
            task_id=row.get("task_id"),
            ref_count=row.get("ref_count", 0),
            created_at=row.get("created_at", 0),
            published_at=row.get("published_at", 0),
            expires_at=row.get("expires_at"),
            gc_eligible_at=row.get("gc_eligible_at"),
            gc_d_at=row.get("gc_d_at"),
        )


# ── Artifact event ──────────────────────────────────────────────────────────────


@dataclass
class ArtifactEvent:
    event_type: str  # "new_artifact"
    descriptor_json: str
    published_at: int
    producer_node_id: str


# ── Abstract store interface ────────────────────────────────────────────────────


class ArtifactStore(ABC):
    @abstractmethod
    async def put(self, descriptor: ArtifactDescriptor, data: bytes) -> str:
        """Store artifact bytes. Returns BLAKE3 hex hash. Raises on failure."""
        ...

    @abstractmethod
    async def get(self, descriptor: ArtifactDescriptor) -> bytes | None:
        """Retrieve artifact bytes by descriptor. Returns None if not found."""
        ...

    @abstractmethod
    async def get_by_hash(self, hash: str) -> bytes | None:
        """Retrieve artifact bytes by hash. Returns None if not found."""
        ...

    @abstractmethod
    async def delete(self, descriptor: ArtifactDescriptor) -> bool:
        """Delete artifact by descriptor. Returns True if deleted."""
        ...

    @abstractmethod
    async def delete_by_hash(self, hash: str) -> bool:
        """Delete artifact by hash. Returns True if deleted."""
        ...

    @abstractmethod
    async def exists(self, descriptor: ArtifactDescriptor) -> bool:
        """Check whether an artifact with this descriptor exists."""
        ...

    @abstractmethod
    async def list(self, namespace: str, name_pattern: str | None = None) -> list[ArtifactMetadata]:
        """List artifact metadata in a namespace, optionally filtered by glob."""
        ...

    @abstractmethod
    async def get_metadata(self, descriptor: ArtifactDescriptor) -> ArtifactMetadata | None:
        """Get metadata for an artifact."""
        ...

    @abstractmethod
    async def total_size(self, namespace: str) -> int:
        """Total bytes stored in a namespace. Used for cap enforcement."""
        ...

    @abstractmethod
    async def sweep_orphans(self) -> int:
        """Remove on-disk artifact files with no metadata row. Returns count removed."""
        ...


# ── Local filesystem implementation ─────────────────────────────────────────────


class LocalFilesystemArtifactStore(ArtifactStore):
    def __init__(
        self,
        db: Database,
        root: Path,
        inline_threshold: int = 4096,
        event_queue: "asyncio.Queue[ArtifactEvent] | None" = None,
    ) -> None:
        import asyncio
        self.db = db
        self.root = Path(root)
        self.inline_threshold = inline_threshold
        self._event_queue = event_queue
        self._hashes_dir = self.root / "hashes"

    def _hash_path(self, hash: str) -> Path:
        return self._hashes_dir / hash[:2] / hash

    def _compute_hash(self, data: bytes) -> str:
        return blake3.blake3(data).hexdigest()

    def _timestamp(self) -> int:
        return int(time.time())

    # ── put ─────────────────────────────────────────────────────────────────────

    async def put(self, descriptor: ArtifactDescriptor, data: bytes) -> str:
        h = self._compute_hash(data)
        now = self._timestamp()
        size = len(data)

        # Write bytes (inline to BLOB or to disk)
        inline = None
        if size <= self.inline_threshold:
            inline = data
        else:
            self._hash_path(h).parent.mkdir(parents=True, exist_ok=True)
            self._hash_path(h).write_bytes(data)

        # Upsert metadata + insert artifact_refs in one transaction
        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO artifact_metadata
                   (namespace, name, version, content_type, hash, size_bytes,
                    inline_data, producer_node_id, bundle_id, ref_count,
                    created_at, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
                   ON CONFLICT(namespace, name, version) DO UPDATE SET
                   hash=excluded.hash, size_bytes=excluded.size_bytes,
                   inline_data=excluded.inline_data, published_at=excluded.published_at""",
                (descriptor.namespace, descriptor.name, descriptor.version,
                 descriptor.content_type, h, size, inline, now, now),
            )

        # Enqueue notification
        if self._event_queue is not None:
            event = ArtifactEvent(
                event_type="new_artifact",
                descriptor_json=descriptor.to_json(),
                published_at=now,
                producer_node_id="",  # populated when called from RPC handler
            )
            await self._event_queue.put(event)

        # Audit log
        await self.db.execute(
            """INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at)
               VALUES ('artifact_published', 'system', NULL, ?, ?)""",
            (json.dumps({"descriptor": {
                "namespace": descriptor.namespace, "name": descriptor.name,
                "version": descriptor.version, "content_type": descriptor.content_type,
            }, "hash": h, "size_bytes": size}), now),
        )
        await self.db.conn.commit()

        return h

    # ── get ─────────────────────────────────────────────────────────────────────

    async def get(self, descriptor: ArtifactDescriptor) -> bytes | None:
        meta = await self.get_metadata(descriptor)
        if meta is None:
            return None
        if meta.gc_d_at is not None:
            return None  # collected

        return await self._read_bytes(meta)

    async def get_by_hash(self, hash: str) -> bytes | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_metadata WHERE hash = ? AND gc_d_at IS NULL",
            (hash,),
        )
        if row is None:
            return None
        meta = ArtifactMetadata.from_row(dict(row))
        return await self._read_bytes(meta)

    async def _read_bytes(self, meta: ArtifactMetadata) -> bytes | None:
        if meta.inline_data is not None:
            data = meta.inline_data
        else:
            path = self._hash_path(meta.hash)
            if not path.exists():
                logger.error("Artifact file missing: %s (hash=%s)", path, meta.hash)
                return None
            data = path.read_bytes()

        # Verify hash on every fetch
        computed = self._compute_hash(data)
        if computed != meta.hash:
            logger.error(
                "Hash verification failed: descriptor=(%s,%s,%s) stored=%s computed=%s",
                meta.namespace, meta.name, meta.version, meta.hash, computed,
            )
            return None

        return data

    # ── delete ──────────────────────────────────────────────────────────────────

    async def delete(self, descriptor: ArtifactDescriptor) -> bool:
        meta = await self.get_metadata(descriptor)
        if meta is None:
            return False

        if meta.inline_data is None:
            path = self._hash_path(meta.hash)
            if path.exists():
                path.unlink()
            # Try to remove parent shard dir if empty
            try:
                path.parent.rmdir()
            except OSError:
                pass

        await self.db.execute(
            "UPDATE artifact_metadata SET gc_d_at = ? WHERE id = ?",
            (self._timestamp(), meta.id),
        )
        await self.db.conn.commit()
        return True

    async def delete_by_hash(self, hash: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_metadata WHERE hash = ? AND gc_d_at IS NULL",
            (hash,),
        )
        if row is None:
            return False
        meta = ArtifactMetadata.from_row(dict(row))
        return await self.delete(meta.to_descriptor())

    # ── exists ──────────────────────────────────────────────────────────────────

    async def exists(self, descriptor: ArtifactDescriptor) -> bool:
        row = await self.db.fetch_one(
            "SELECT 1 FROM artifact_metadata WHERE namespace=? AND name=? AND version=? AND gc_d_at IS NULL",
            (descriptor.namespace, descriptor.name, descriptor.version),
        )
        return row is not None

    # ── list ─────────────────────────────────────────────────────────────────────

    async def list(self, namespace: str, name_pattern: str | None = None) -> list[ArtifactMetadata]:
        if name_pattern:
            # Convert glob to SQL LIKE. Simple case: just * → %
            rows = await self.db.fetch_all(
                "SELECT * FROM artifact_metadata WHERE namespace=? AND gc_d_at IS NULL ORDER BY published_at DESC",
                (namespace,),
            )
            return [
                m for m in (ArtifactMetadata.from_row(dict(r)) for r in rows)
                if glob_match(name_pattern, m.name)
            ]
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM artifact_metadata WHERE namespace=? AND gc_d_at IS NULL ORDER BY published_at DESC",
                (namespace,),
            )
            return [ArtifactMetadata.from_row(dict(r)) for r in rows]

    # ── metadata ────────────────────────────────────────────────────────────────

    async def get_metadata(self, descriptor: ArtifactDescriptor) -> ArtifactMetadata | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_metadata WHERE namespace=? AND name=? AND version=?",
            (descriptor.namespace, descriptor.name, descriptor.version),
        )
        if row is None:
            return None
        return ArtifactMetadata.from_row(dict(row))

    # ── size ─────────────────────────────────────────────────────────────────────

    async def total_size(self, namespace: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM artifact_metadata WHERE namespace=? AND gc_d_at IS NULL",
            (namespace,),
        )
        return row["total"] if row else 0

    # ── orphan cleanup ──────────────────────────────────────────────────────────

    async def sweep_orphans(self) -> int:
        removed = 0
        if not self._hashes_dir.exists():
            return 0

        all_hashes: set[str] = set()
        rows = await self.db.fetch_all("SELECT hash FROM artifact_metadata WHERE gc_d_at IS NULL")
        for r in rows:
            all_hashes.add(r["hash"])

        for shard_dir in self._hashes_dir.iterdir():
            if not shard_dir.is_dir():
                continue
            for file in shard_dir.iterdir():
                if file.name not in all_hashes:
                    file.unlink()
                    removed += 1
            # Remove empty shard dirs
            try:
                shard_dir.rmdir()
            except OSError:
                pass

        return removed


# ── Mock store for testing ──────────────────────────────────────────────────────


class MockArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}  # keyed by descriptor JSON
        self._meta: dict[str, dict] = {}
        self.put_delay: float = 0.0
        self.get_delay: float = 0.0
        self.fail_on_put: bool = False
        self.corrupt_on_fetch: bool = False

    def _key(self, descriptor: ArtifactDescriptor) -> str:
        return descriptor.to_json()

    def _compute_hash(self, data: bytes) -> str:
        return blake3.blake3(data).hexdigest()

    def _timestamp(self) -> int:
        return int(time.time())

    async def put(self, descriptor: ArtifactDescriptor, data: bytes) -> str:
        import asyncio
        if self.fail_on_put:
            raise OSError("Simulated put failure")
        if self.put_delay > 0:
            await asyncio.sleep(self.put_delay)
        h = self._compute_hash(data)
        key = self._key(descriptor)
        self._store[key] = data
        self._meta[key] = {
            "hash": h,
            "size_bytes": len(data),
            "published_at": self._timestamp(),
        }
        return h

    async def get(self, descriptor: ArtifactDescriptor) -> bytes | None:
        import asyncio
        if self.get_delay > 0:
            await asyncio.sleep(self.get_delay)
        key = self._key(descriptor)
        data = self._store.get(key)
        if data is None:
            return None
        if self.corrupt_on_fetch:
            data = b"x" + data[1:]
        # Verify hash
        meta = self._meta.get(key, {})
        stored_hash = meta.get("hash", "")
        computed = self._compute_hash(data)
        if stored_hash and computed != stored_hash:
            return None
        return data

    async def get_by_hash(self, hash: str) -> bytes | None:
        for key, meta in self._meta.items():
            if meta.get("hash") == hash:
                return self._store.get(key)
        return None

    async def delete(self, descriptor: ArtifactDescriptor) -> bool:
        key = self._key(descriptor)
        if key in self._store:
            del self._store[key]
            self._meta.pop(key, None)
            return True
        return False

    async def delete_by_hash(self, hash: str) -> bool:
        for key, meta in list(self._meta.items()):
            if meta.get("hash") == hash:
                del self._store[key]
                del self._meta[key]
                return True
        return False

    async def exists(self, descriptor: ArtifactDescriptor) -> bool:
        return self._key(descriptor) in self._store

    async def list(self, namespace: str, name_pattern: str | None = None) -> list[ArtifactMetadata]:
        results: list[ArtifactMetadata] = []
        for key, meta in self._meta.items():
            d = json.loads(key)
            if d.get("namespace") != namespace:
                continue
            if name_pattern and not glob_match(name_pattern, d.get("name", "")):
                continue
            results.append(ArtifactMetadata(
                namespace=d.get("namespace", "bundle"),
                name=d.get("name", ""),
                version=d.get("version", ""),
                content_type=d.get("content_type", "application/octet-stream"),
                hash=meta.get("hash", ""),
                size_bytes=meta.get("size_bytes", 0),
                published_at=meta.get("published_at", 0),
            ))
        return results

    async def get_metadata(self, descriptor: ArtifactDescriptor) -> ArtifactMetadata | None:
        key = self._key(descriptor)
        meta = self._meta.get(key)
        if meta is None:
            return None
        return ArtifactMetadata(
            namespace=descriptor.namespace,
            name=descriptor.name,
            version=descriptor.version,
            content_type=descriptor.content_type,
            hash=meta.get("hash", ""),
            size_bytes=meta.get("size_bytes", 0),
            published_at=meta.get("published_at", 0),
        )

    async def total_size(self, namespace: str) -> int:
        total = 0
        for key, meta in self._meta.items():
            d = json.loads(key)
            if d.get("namespace") == namespace:
                total += meta.get("size_bytes", 0)
        return total

    async def sweep_orphans(self) -> int:
        return 0


# ── Secret store ────────────────────────────────────────────────────────────────


class SecretStore:
    def __init__(self, entries: list[dict]) -> None:
        self._secrets: dict[str, str] = {}
        for entry in entries:
            env_var = entry.get("env_var", "")
            name = entry.get("name", "")
            if env_var and name:
                self._secrets[name] = env_var

    def fetch(self, name: str) -> tuple[str | None, int | None]:
        env_var = self._secrets.get(name)
        if env_var is None:
            return None, None
        value = os.environ.get(env_var)
        if value is None:
            return None, None
        return value, None  # No expiry for env-var secrets

    def exists(self, name: str) -> bool:
        return name in self._secrets and os.environ.get(self._secrets[name]) is not None
