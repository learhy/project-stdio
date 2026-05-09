"""Tests for Artifact Protocol: glob_match, stores, content addressing, secrets."""
import json
import os
import pytest
from studio.orchestrator.artifact import (
    glob_match,
    ArtifactDescriptor,
    ArtifactStore,
    ArtifactMetadata,
    ArtifactEvent,
    LocalFilesystemArtifactStore,
    MockArtifactStore,
    SecretStore,
)


class TestGlobMatch:
    def test_exact_match(self):
        assert glob_match("hello", "hello") is True
        assert glob_match("hello", "world") is False

    def test_star_wildcard(self):
        assert glob_match("test-*", "test-results") is True
        assert glob_match("test-*", "test-42") is True
        assert glob_match("test-*", "prod-test") is False

    def test_double_star_wildcard(self):
        assert glob_match("**", "anything/with/slashes") is True
        assert glob_match("prefix-**", "prefix-foo/bar") is True
        assert glob_match("a/**/z", "a/b/c/z") is True

    def test_question_mark(self):
        assert glob_match("file-?.txt", "file-a.txt") is True
        assert glob_match("file-?.txt", "file-1.txt") is True
        assert glob_match("file-?.txt", "file-ab.txt") is False

    def test_character_class(self):
        assert glob_match("file-[abc].txt", "file-a.txt") is True
        assert glob_match("file-[abc].txt", "file-b.txt") is True
        assert glob_match("file-[abc].txt", "file-d.txt") is False

    def test_negated_character_class(self):
        assert glob_match("file-[!abc].txt", "file-d.txt") is True
        assert glob_match("file-[!abc].txt", "file-a.txt") is False

    def test_empty_strings(self):
        assert glob_match("", "") is True
        assert glob_match("*", "") is True
        assert glob_match("", "x") is False

    def test_special_regex_chars_escaped(self):
        assert glob_match("test.$", "test.$") is True
        assert glob_match("test.$", "testX$") is False
        assert glob_match("(parens)", "(parens)") is True

    def test_star_does_not_cross_segments(self):
        # Single * does not match /
        assert glob_match("foo/*/baz", "foo/bar/baz") is True
        assert glob_match("foo/*/baz", "foo/bar/qux/baz") is False

    def test_case_sensitive(self):
        assert glob_match("Test", "Test") is True
        assert glob_match("Test", "test") is False

    def test_pattern_caching(self):
        # Multiple calls with same pattern use cache
        assert glob_match("cached-*", "cached-value") is True
        assert glob_match("cached-*", "cached-other") is True
        assert glob_match("cached-*", "no-match") is False


class TestArtifactDescriptor:
    def test_frozen_equality(self):
        d1 = ArtifactDescriptor(namespace="bundle", name="test", version="v1")
        d2 = ArtifactDescriptor(namespace="bundle", name="test", version="v1")
        assert d1 == d2
        assert hash(d1) == hash(d2)

    def test_different_descriptors(self):
        d1 = ArtifactDescriptor(namespace="bundle", name="a")
        d2 = ArtifactDescriptor(namespace="bundle", name="b")
        assert d1 != d2

    def test_to_json(self):
        d = ArtifactDescriptor(namespace="bundle", name="results", version="v1", content_type="application/json")
        j = d.to_json()
        parsed = json.loads(j)
        assert parsed["namespace"] == "bundle"
        assert parsed["name"] == "results"
        assert parsed["version"] == "v1"

    def test_from_dict(self):
        d = ArtifactDescriptor.from_dict({"namespace": "global", "name": "config", "version": "v2"})
        assert d.namespace == "global"
        assert d.name == "config"
        assert d.version == "v2"

    def test_version_normalized_to_empty(self):
        d = ArtifactDescriptor.from_dict({"namespace": "bundle", "name": "x"})
        assert d.version == ""

    def test_default_content_type(self):
        d = ArtifactDescriptor.from_dict({"namespace": "bundle", "name": "x"})
        assert d.content_type == "application/octet-stream"


class TestMockArtifactStore:
    async def test_put_and_get_roundtrip(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="test", version="v1")
        data = b"hello world"
        h = await store.put(desc, data)
        assert len(h) == 64  # BLAKE3 hex hash

        result = await store.get(desc)
        assert result == data

    async def test_hash_deterministic(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        h1 = await store.put(desc, b"same data")
        h2 = await store.put(desc, b"same data")
        assert h1 == h2

    async def test_hash_different_for_different_data(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        h1 = await store.put(desc, b"data one")
        h2 = await store.put(desc, b"data two")
        assert h1 != h2

    async def test_get_by_hash(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        h = await store.put(desc, b"find me")
        result = await store.get_by_hash(h)
        assert result == b"find me"

    async def test_overwrite_upsert(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x", version="latest")
        h1 = await store.put(desc, b"first")
        h2 = await store.put(desc, b"second")
        assert h1 != h2
        result = await store.get(desc)
        assert result == b"second"

    async def test_delete(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        await store.put(desc, b"data")
        assert await store.exists(desc) is True
        assert await store.delete(desc) is True
        assert await store.exists(desc) is False
        assert await store.get(desc) is None

    async def test_delete_nonexistent(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="noexist")
        assert await store.delete(desc) is False

    async def test_delete_by_hash(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        h = await store.put(desc, b"deletable")
        assert await store.delete_by_hash(h) is True
        assert await store.get_by_hash(h) is None

    async def test_exists(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        assert await store.exists(desc) is False
        await store.put(desc, b"data")
        assert await store.exists(desc) is True

    async def test_list_namespace(self):
        store = MockArtifactStore()
        await store.put(ArtifactDescriptor(namespace="bundle", name="a"), b"1")
        await store.put(ArtifactDescriptor(namespace="bundle", name="b"), b"2")
        await store.put(ArtifactDescriptor(namespace="global", name="c"), b"3")

        bundle = await store.list("bundle")
        assert len(bundle) == 2
        assert {m.name for m in bundle} == {"a", "b"}

    async def test_list_with_name_pattern(self):
        store = MockArtifactStore()
        await store.put(ArtifactDescriptor(namespace="bundle", name="test-results"), b"1")
        await store.put(ArtifactDescriptor(namespace="bundle", name="test-logs"), b"2")
        await store.put(ArtifactDescriptor(namespace="bundle", name="prod-config"), b"3")

        results = await store.list("bundle", name_pattern="test-*")
        assert len(results) == 2
        assert {m.name for m in results} == {"test-results", "test-logs"}

    async def test_total_size(self):
        store = MockArtifactStore()
        await store.put(ArtifactDescriptor(namespace="bundle", name="a"), b"12345")
        await store.put(ArtifactDescriptor(namespace="bundle", name="b"), b"67890")

        total = await store.total_size("bundle")
        assert total == 10

    async def test_get_metadata(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x", version="v2")
        h = await store.put(desc, b"metadata test")

        meta = await store.get_metadata(desc)
        assert meta is not None
        assert meta.hash == h
        assert meta.size_bytes == len(b"metadata test")
        assert meta.namespace == "bundle"
        assert meta.name == "x"

    async def test_put_failure_mode(self):
        store = MockArtifactStore()
        store.fail_on_put = True
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        with pytest.raises(OSError, match="Simulated put failure"):
            await store.put(desc, b"data")

    async def test_corruption_detection(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        await store.put(desc, b"original data")
        store.corrupt_on_fetch = True
        # Should return None because hash verification fails
        result = await store.get(desc)
        assert result is None

    async def test_put_delay(self):
        store = MockArtifactStore()
        store.put_delay = 0.01
        desc = ArtifactDescriptor(namespace="bundle", name="x")
        h = await store.put(desc, b"delayed")
        assert len(h) == 64


    async def test_republish_clears_tombstone(self):
        store = MockArtifactStore()
        desc = ArtifactDescriptor(namespace="bundle", name="results")
        await store.put(desc, b"v1")
        # Simulate GC having tombstoned the artifact
        meta = store._meta[store._key(desc)]
        meta["gc_d_at"] = 1000000
        meta["gc_eligible_at"] = 999999
        # Republish — should clear the tombstone
        h2 = await store.put(desc, b"v2")
        data = await store.get(desc)
        assert data == b"v2"
        assert h2 is not None

class TestSecretStore:
    def test_fetch_existing_secret(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "secret-value-123")
        store = SecretStore([{"name": "test-secret", "env_var": "TEST_SECRET"}])
        value, expires = store.fetch("test-secret")
        assert value == "secret-value-123"
        assert expires is None

    def test_fetch_missing_secret_name(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "val")
        store = SecretStore([{"name": "test-secret", "env_var": "TEST_SECRET"}])
        value, expires = store.fetch("nonexistent")
        assert value is None
        assert expires is None

    def test_fetch_env_var_unset(self):
        store = SecretStore([{"name": "test-secret", "env_var": "UNSET_VAR"}])
        value, expires = store.fetch("test-secret")
        assert value is None

    def test_exists(self, monkeypatch):
        monkeypatch.setenv("EXISTS_VAR", "val")
        store = SecretStore([{"name": "exists-secret", "env_var": "EXISTS_VAR"}])
        assert store.exists("exists-secret") is True
        assert store.exists("missing") is False

    def test_empty_config(self):
        store = SecretStore([])
        value, _ = store.fetch("anything")
        assert value is None

    def test_multiple_secrets(self, monkeypatch):
        monkeypatch.setenv("VAR_A", "val-a")
        monkeypatch.setenv("VAR_B", "val-b")
        store = SecretStore([
            {"name": "secret-a", "env_var": "VAR_A"},
            {"name": "secret-b", "env_var": "VAR_B"},
        ])
        assert store.fetch("secret-a")[0] == "val-a"
        assert store.fetch("secret-b")[0] == "val-b"


class TestArtifactEvent:
    def test_event_fields(self):
        event = ArtifactEvent(
            event_type="new_artifact",
            descriptor_json='{"namespace":"bundle","name":"x"}',
            published_at=1234567890,
            producer_node_id="bundle-1:node-3",
        )
        assert event.event_type == "new_artifact"
        assert event.producer_node_id == "bundle-1:node-3"


class TestArtifactMetadata:
    def test_from_row(self):
        row = {
            "id": 1, "namespace": "bundle", "name": "results", "version": "v1",
            "content_type": "application/json", "hash": "abcd1234", "size_bytes": 100,
            "inline_data": None, "producer_node_id": "b:1:n:1", "bundle_id": "b:1",
            "ref_count": 2, "created_at": 100, "published_at": 200,
        }
        meta = ArtifactMetadata.from_row(row)
        assert meta.id == 1
        assert meta.name == "results"
        assert meta.hash == "abcd1234"
        assert meta.ref_count == 2

    def test_to_descriptor(self):
        meta = ArtifactMetadata(
            namespace="global", name="config", version="v3",
            content_type="text/plain", hash="efgh5678",
        )
        desc = meta.to_descriptor()
        assert desc.namespace == "global"
        assert desc.name == "config"
        assert desc.version == "v3"
