"""Tests for Bundle 3.4: Security Pass — token hardening, secrets rotation,
capability audit, expansion subset check, audit log completeness."""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from studio.orchestrator.db import Database
from studio.orchestrator.capability import is_subset
from studio.orchestrator.models import CapabilityManifest
from studio.orchestrator.runner import _generate_token, NoopWorkerRunner, WorkerSpawnResult
from studio.orchestrator.artifact import SecretStore


# ── helpers ────────────────────────────────────────────────────────────────

def _make_manifest(**overrides) -> dict:
    base = {
        "schema_version": "1.0",
        "subject": {"kind": "bundle", "id": "test-bundle"},
        "grants": {
            "filesystem": {"reads": [], "writes": []},
            "network": {"egress": [], "ingress": {"enabled": False}, "dns": {"enabled": False}},
            "process": {"exec": [], "spawn_subtasks": {"enabled": False, "max_depth": 0, "max_count": 0}},
            "secrets": [],
            "rpc": {"methods": ["worker.*"], "artifact_access": {"reads": [], "writes": []}},
            "resources": {"cpu_limit": 0, "memory_limit": 0, "disk_limit": 0,
                          "wall_time_limit": 0,
                          "llm_token_budget": {"input_tokens": 0, "output_tokens": 0, "by_model": {}}},
        },
        "metadata": {"rationale": "test"},
    }
    for key, value in overrides.items():
        if key in base:
            base[key] = value
        elif "." not in key:
            base["grants"][key] = value
        else:
            parts = key.split(".")
            target = base["grants"]
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            target[parts[-1]] = value
    return base


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "test.db"
        database = Database(db_path)
        await database.connect()
        yield database
        await database.close()


@pytest.fixture
def secret_entries():
    return [
        {"name": "github_token", "env_var": "GITHUB_TOKEN"},
        {"name": "cloud_api_key", "env_var": "CLOUD_API_KEY"},
    ]


@pytest.fixture
def tmp_memory_root():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ── Token hardening tests ──────────────────────────────────────────────────

class TestTokenHardening:
    def test_token_has_expiry_set(self):
        runner = NoopWorkerRunner(None, token_expiry_minutes=15)  # type: ignore[arg-type]
        assert runner.token_expiry_minutes == 15

    def test_token_expiry_default(self):
        runner = NoopWorkerRunner(None)  # type: ignore[arg-type]
        assert runner.token_expiry_minutes == 15

    async def test_worker_spawn_includes_token_expiry(self, db):
        now = int(time.time())
        await db.execute(
            "INSERT INTO bundles (id, repo, state, proposal_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bundle-1", "test-repo", "proposed", "{}", now),
        )
        runner = NoopWorkerRunner(db, token_expiry_minutes=10)
        manifest = CapabilityManifest.model_validate(_make_manifest())
        result = await runner.spawn_worker(
            "worker-1", "bundle-1", "node-1", manifest, "/tmp/work"
        )
        assert isinstance(result, WorkerSpawnResult)

        row = await db.fetch_one("SELECT token_expires_at FROM workers WHERE id = ?", ("worker-1",))
        assert row is not None
        assert row["token_expires_at"] is not None
        now2 = int(time.time())
        assert now2 - 600 - 10 < row["token_expires_at"] <= now2 + 600 + 10

    async def test_worker_spawn_audit_entry(self, db):
        now = int(time.time())
        await db.execute(
            "INSERT INTO bundles (id, repo, state, proposal_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bundle-2", "test-repo", "proposed", "{}", now),
        )
        runner = NoopWorkerRunner(db, token_expiry_minutes=5)
        manifest = CapabilityManifest.model_validate(_make_manifest())
        await runner.spawn_worker("worker-2", "bundle-2", "node-2", manifest, "/tmp/work")

        row = await db.fetch_one(
            "SELECT * FROM audit_log WHERE event_type = ? AND subject_id = ?",
            ("worker_spawned", "worker-2"),
        )
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert payload["bundle_id"] == "bundle-2"


# ── Capability subset check tests ──────────────────────────────────────────

class TestCapabilitySubsetCheck:
    def test_empty_task_is_subset_of_empty_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest())
        task = CapabilityManifest.model_validate(_make_manifest())
        ok, reason = is_subset(task, bundle)
        assert ok
        assert reason == ""

    def test_task_filesystem_read_within_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            filesystem={"reads": [{"path": "/home", "recursive": True}], "writes": []}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            filesystem={"reads": [{"path": "/home/user", "recursive": False}], "writes": []}
        ))
        ok, _ = is_subset(task, bundle)
        assert ok

    def test_task_filesystem_read_outside_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            filesystem={"reads": [{"path": "/home", "recursive": True}], "writes": []}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            filesystem={"reads": [{"path": "/etc", "recursive": False}], "writes": []}
        ))
        ok, reason = is_subset(task, bundle)
        assert not ok
        assert "filesystem" in reason

    def test_task_network_egress_subset(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            network={"egress": [{"destination": "api.github.com", "protocol": "https", "ports": [443]}],
                     "ingress": {"enabled": False}, "dns": {"enabled": False}}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            network={"egress": [{"destination": "api.github.com", "protocol": "https", "ports": [443]}],
                     "ingress": {"enabled": False}, "dns": {"enabled": False}}
        ))
        ok, _ = is_subset(task, bundle)
        assert ok

    def test_task_network_egress_not_in_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            network={"egress": [{"destination": "api.github.com", "protocol": "https", "ports": [443]}],
                     "ingress": {"enabled": False}, "dns": {"enabled": False}}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            network={"egress": [{"destination": "evil.com", "protocol": "https", "ports": [443]}],
                     "ingress": {"enabled": False}, "dns": {"enabled": False}}
        ))
        ok, reason = is_subset(task, bundle)
        assert not ok
        assert "network" in reason

    def test_task_secrets_subset_same_purpose(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "github_token", "purpose": "github_auth", "delivery": "env"}]
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "github_token", "purpose": "github_auth", "delivery": "env"}]
        ))
        ok, _ = is_subset(task, bundle)
        assert ok

    def test_task_secrets_subset_bundle_custom_subsumes(self):
        """Bundle purpose 'custom' subsumes any task purpose."""
        bundle = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "github_token", "purpose": "custom", "delivery": "env"}]
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "github_token", "purpose": "github_auth", "delivery": "env"}]
        ))
        ok, _ = is_subset(task, bundle)
        assert ok

    def test_task_secrets_not_in_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "github_token", "purpose": "custom", "delivery": "env"}]
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            secrets=[{"name": "aws_key", "purpose": "custom", "delivery": "env"}]
        ))
        ok, reason = is_subset(task, bundle)
        assert not ok
        assert "secrets" in reason

    def test_task_rpc_methods_subset(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            rpc={"methods": ["worker.*", "artifact.*"], "artifact_access": {"reads": [], "writes": []}}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            rpc={"methods": ["worker.heartbeat", "artifact.publish"], "artifact_access": {"reads": [], "writes": []}}
        ))
        ok, _ = is_subset(task, bundle)
        assert ok

    def test_task_rpc_methods_not_in_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            rpc={"methods": ["worker.*"], "artifact_access": {"reads": [], "writes": []}}
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            rpc={"methods": ["secrets.fetch"], "artifact_access": {"reads": [], "writes": []}}
        ))
        ok, reason = is_subset(task, bundle)
        assert not ok
        assert "rpc" in reason

    def test_task_resources_limits_exceed_bundle(self):
        bundle = CapabilityManifest.model_validate(_make_manifest(
            resources={"cpu_limit": 2, "memory_limit": 512, "disk_limit": 0, "wall_time_limit": 0,
                       "llm_token_budget": {"input_tokens": 10000, "output_tokens": 5000, "by_model": {}}},
        ))
        task = CapabilityManifest.model_validate(_make_manifest(
            resources={"cpu_limit": 4, "memory_limit": 1024, "disk_limit": 0, "wall_time_limit": 0,
                       "llm_token_budget": {"input_tokens": 5000, "output_tokens": 2000, "by_model": {}}},
        ))
        ok, reason = is_subset(task, bundle)
        assert not ok
        assert "resources" in reason


# ── Secret store tests ─────────────────────────────────────────────────────

class TestSecretStore:
    def test_fetch_unknown_secret(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        value, expires = store.fetch("unknown")
        assert value is None
        assert expires is None

    def test_fetch_from_env(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        os.environ["GITHUB_TOKEN"] = "gh_test_value"
        try:
            value, expires = store.fetch("github_token")
            assert value == "gh_test_value"
            assert expires is None
        finally:
            del os.environ["GITHUB_TOKEN"]

    def test_fetch_missing_env(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]
        value, expires = store.fetch("github_token")
        assert value is None

    def test_exists_with_env(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        os.environ["GITHUB_TOKEN"] = "test_val"
        try:
            assert store.exists("github_token")
        finally:
            del os.environ["GITHUB_TOKEN"]

    def test_exists_without_env_or_file(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]
        assert not store.exists("github_token")

    def test_rotate_creates_new_value(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        new_value, error = store.rotate("github_token")
        assert error is None
        assert new_value is not None
        assert len(new_value) == 64

    def test_rotate_writes_file(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        new_value, _ = store.rotate("github_token")
        file_path = Path(tmp_memory_root) / "secrets" / "github_token.json"
        assert file_path.exists()
        data = json.loads(file_path.read_text())
        assert data["value"] == new_value
        assert data["name"] == "github_token"
        assert "rotated_at" in data

    def test_rotate_unknown_secret(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        value, error = store.rotate("unknown")
        assert value is None
        assert error is not None
        assert "Unknown secret" in error

    def test_file_store_takes_precedence(self, secret_entries, tmp_memory_root):
        store = SecretStore(secret_entries, memory_root=tmp_memory_root)
        new_value, _ = store.rotate("github_token")

        os.environ["GITHUB_TOKEN"] = "old_env_value"
        try:
            value, _ = store.fetch("github_token")
            assert value == new_value
        finally:
            del os.environ["GITHUB_TOKEN"]


# ── Audit log completeness tests ───────────────────────────────────────────

class TestAuditLogCompleteness:
    async def test_worker_spawn_audited(self, db):
        now = int(time.time())
        await db.execute(
            "INSERT INTO bundles (id, repo, state, proposal_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b-audit-1", "test-repo", "proposed", "{}", now),
        )
        runner = NoopWorkerRunner(db, token_expiry_minutes=5)
        manifest = CapabilityManifest.model_validate(_make_manifest())
        await runner.spawn_worker("w-audit-1", "b-audit-1", "n-audit-1", manifest, "/tmp")

        row = await db.fetch_one(
            "SELECT * FROM audit_log WHERE event_type = 'worker_spawned' AND subject_id = 'w-audit-1'"
        )
        assert row is not None

    async def test_capability_check_allowed_recorded(self, db):
        now = int(time.time())
        await db.execute(
            "INSERT INTO bundles (id, repo, state, proposal_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b-1", "test-repo", "proposed", "{}", now),
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w-1", "b-1", "node-1", "tok", "{}", "running", now),
        )
        await db.execute(
            "INSERT INTO capability_checks (worker_id, bundle_id, requested_op, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("w-1", "b-1", "filesystem.read:/tmp/test", "allowed", now),
        )

        row = await db.fetch_one("SELECT * FROM capability_checks WHERE worker_id = 'w-1'")
        assert row is not None
        assert row["result"] == "allowed"

    async def test_capability_check_denied_recorded(self, db):
        now = int(time.time())
        await db.execute(
            "INSERT INTO bundles (id, repo, state, proposal_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("b-2", "test-repo", "proposed", "{}", now),
        )
        await db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w-2", "b-2", "node-2", "tok", "{}", "running", now),
        )
        await db.execute(
            "INSERT INTO capability_checks (worker_id, bundle_id, requested_op, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("w-2", "b-2", "network.egress:evil.com:443", "denied", now),
        )

        row = await db.fetch_one("SELECT * FROM capability_checks WHERE worker_id = 'w-2'")
        assert row is not None
        assert row["result"] == "denied"

    async def test_secret_access_audited_name_only(self, db):
        now = int(time.time())
        payload = json.dumps({
            "worker_id": "w-3", "bundle_id": "b-3",
            "secret_name": "github_token", "purpose": "github_auth",
        })
        await db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("secret_access", "secret", "github_token", payload, now),
        )

        row = await db.fetch_one("SELECT * FROM audit_log WHERE event_type = 'secret_access'")
        assert row is not None
        p = json.loads(row["payload_json"])
        assert p["secret_name"] == "github_token"
        assert "value" not in p

    async def test_auth_failure_audited(self, db):
        now = int(time.time())
        payload = json.dumps({"reason": "invalid_token", "token_prefix": "abc12345"})
        await db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_auth_failure", "worker", "", payload, now),
        )

        row = await db.fetch_one("SELECT * FROM audit_log WHERE event_type = 'worker_auth_failure'")
        assert row is not None
        p = json.loads(row["payload_json"])
        assert p["reason"] == "invalid_token"

    async def test_secret_rotated_audited(self, db):
        now = int(time.time())
        payload = json.dumps({"affected_workers": ["w-1", "w-2"], "rotated_at": now})
        await db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("secret_rotated", "secret", "github_token", payload, now),
        )

        row = await db.fetch_one("SELECT * FROM audit_log WHERE event_type = 'secret_rotated'")
        assert row is not None
        p = json.loads(row["payload_json"])
        assert "w-1" in p["affected_workers"]
        assert "w-2" in p["affected_workers"]

    async def test_expansion_denied_audited(self, db):
        now = int(time.time())
        payload = json.dumps({
            "requesting_node_id": "node-1",
            "reason": "Node 'evil-node' capability exceeds bundle grant: network grant exceeds bundle scope",
        })
        await db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dag_expansion_denied", "bundle", "b-1", payload, now),
        )

        row = await db.fetch_one("SELECT * FROM audit_log WHERE event_type = 'dag_expansion_denied'")
        assert row is not None
        p = json.loads(row["payload_json"])
        assert "capability exceeds bundle grant" in p["reason"]
