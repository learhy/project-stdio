"""Tests for capability.py — subset checking, op_descriptor, glob matching.

Comprehensive coverage of: filesystem path containment, network CIDR
subsumption, RPC wildcards, resource limit comparison, empty grant sets.
"""
import pytest
from studio.orchestrator.capability import (
    is_subset,
    check_op,
    glob_match,
    OpDescriptor,
    _path_contained,
    _rpc_method_covered,
    _glob_covers,
    _artifact_pattern_covers,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    FilesystemGrants,
    NetworkGrants,
    EgressGrant,
    ProcessGrants,
    ExecGrant,
    RpcGrants,
    ResourceGrants,
    LlmTokenBudget,
    Grants,
    SecretGrant,
    ArtifactAccessConfig,
    ArtifactAccessPattern,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_manifest(**overrides) -> CapabilityManifest:
    """Create a manifest with overrides for testing."""
    return CapabilityManifest(
        schema_version="1.0",
        subject={"kind": "bundle", "id": "test-bundle"},
        grants={
            "filesystem": {
                "reads": [{"path": "/work", "recursive": True}],
                "writes": [{"path": "/work", "recursive": True, "create": True}],
            },
            "network": {"egress": []},
            "process": {"exec": []},
            "rpc": {"methods": ["worker.*"]},
            **overrides.get("grants", {}),
        },
        metadata={"rationale": "test"},
    )


# ── Path containment ─────────────────────────────────────────────────────────

def test_path_contained_exact():
    assert _path_contained("/work/src", "/work/src")


def test_path_contained_subdirectory():
    assert _path_contained("/work/src/main.py", "/work")


def test_path_contained_not_contained():
    assert not _path_contained("/etc/passwd", "/work")


def test_path_contained_sibling():
    assert not _path_contained("/work2/src", "/work/src")


# ── Filesystem subset ────────────────────────────────────────────────────────

def test_filesystem_subset_exact():
    task = make_manifest()
    bundle = make_manifest()
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_filesystem_subset_task_more_specific():
    task = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work/src", "recursive": False}],
            "writes": [{"path": "/work/src", "recursive": False, "create": True}],
        },
    })
    bundle = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": True}],
            "writes": [{"path": "/work", "recursive": True, "create": True}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_filesystem_subset_task_exceeds_bundle():
    task = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/etc", "recursive": True}],
            "writes": [{"path": "/work", "recursive": True, "create": True}],
        },
    })
    bundle = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": True}],
            "writes": [{"path": "/work", "recursive": True, "create": True}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert not ok
    assert "filesystem" in reason


def test_filesystem_subset_task_recursive_bundle_not():
    task = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": True}],
            "writes": [{"path": "/work", "recursive": False, "create": True}],
        },
    })
    bundle = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": False}],
            "writes": [{"path": "/work", "recursive": False, "create": True}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert not ok


# ── Network subset ───────────────────────────────────────────────────────────

def test_network_subset_exact_hostname():
    task = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "https"}],
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "https"}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_network_subset_protocol_subsumption():
    """Bundle allows tcp, task asks for https — tcp subsumes https."""
    task = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "https"}],
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "tcp"}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_network_subset_task_asks_more_ports():
    task = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [80, 443], "protocol": "tcp"}],
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "tcp"}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert not ok


def test_network_subset_cidr_containment():
    task = make_manifest(grants={
        "network": {
            "egress": [{"destination": "10.0.0.0/24", "ports": [], "protocol": "tcp"}],
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "egress": [{"destination": "10.0.0.0/16", "ports": [], "protocol": "tcp"}],
        },
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_network_subset_ingress():
    task = make_manifest(grants={
        "network": {
            "ingress": {"enabled": True},
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "ingress": {"enabled": False},
        },
    })
    ok, reason = is_subset(task, bundle)
    assert not ok


def test_network_subset_dns():
    task = make_manifest(grants={
        "network": {
            "dns": {"enabled": True},
        },
    })
    bundle = make_manifest(grants={
        "network": {
            "dns": {"enabled": False},
        },
    })
    ok, reason = is_subset(task, bundle)
    assert not ok


# ── RPC subset ────────────────────────────────────────────────────────────────

def test_rpc_method_covered_exact():
    assert _rpc_method_covered("artifact.publish", "artifact.publish")


def test_rpc_method_covered_wildcard():
    assert _rpc_method_covered("artifact.publish", "artifact.*")


def test_rpc_method_covered_wildcard_nested():
    assert _rpc_method_covered("artifact.*", "artifact.*")


def test_rpc_method_covered_no_match():
    assert not _rpc_method_covered("artifact.publish", "worker.*")


def test_rpc_method_covered_different_prefix():
    assert not _rpc_method_covered("artifact.publish", "art.*")


def test_rpc_subset_bundle_wildcard_covers_task_specific():
    task = make_manifest(grants={
        "rpc": {"methods": ["artifact.publish"]},
    })
    bundle = make_manifest(grants={
        "rpc": {"methods": ["artifact.*"]},
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


# ── Artifact pattern coverage ────────────────────────────────────────────────

def test_artifact_pattern_covers_exact():
    bundle = ArtifactAccessPattern(namespace="bundle", name="test-*", version="*", content_type="*")
    task = ArtifactAccessPattern(namespace="bundle", name="test-results-*", version="*", content_type="*")
    assert _artifact_pattern_covers(bundle, task)


def test_glob_match_star():
    assert glob_match("test-*", "test-results")


def test_glob_match_exact():
    assert glob_match("hello", "hello")


def test_glob_match_no_match():
    assert not glob_match("test-*", "prod-results")


def test_glob_match_double_star():
    assert glob_match("**", "anything/goes/here")


def test_glob_match_question():
    assert glob_match("file.???", "file.txt")
    assert not glob_match("file.??", "file.pyc")  # ?? = 2 chars, pyc = 3


def test_glob_match_charclass():
    assert glob_match("file.[tj]s", "file.ts")
    assert glob_match("file.[tj]s", "file.js")
    assert not glob_match("file.[tj]s", "file.py")


# ── Resources subset ─────────────────────────────────────────────────────────

def test_resources_subset_budget_within():
    task = make_manifest(grants={
        "resources": {
            "wall_time_limit": 3600,
            "llm_token_budget": {"input_tokens": 50000, "output_tokens": 25000},
        },
    })
    bundle = make_manifest(grants={
        "resources": {
            "wall_time_limit": 7200,
            "llm_token_budget": {"input_tokens": 100000, "output_tokens": 50000},
        },
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


def test_resources_subset_exceeds():
    task = make_manifest(grants={
        "resources": {"wall_time_limit": 7200},
    })
    bundle = make_manifest(grants={
        "resources": {"wall_time_limit": 3600},
    })
    ok, reason = is_subset(task, bundle)
    assert not ok
    assert "resources" in reason


def test_resources_zero_bundle_means_unlimited():
    """A bundle limit of 0 means no explicit limit, so task can set any value."""
    task = make_manifest(grants={
        "resources": {"cpu_limit": 2000},
    })
    bundle = make_manifest(grants={
        "resources": {"cpu_limit": 0},
    })
    ok, reason = is_subset(task, bundle)
    assert ok, reason


# ── op_descriptor parsing ───────────────────────────────────────────────────

def test_op_descriptor_filesystem():
    op = OpDescriptor("filesystem.write:/work/src/main.py")
    assert op.category == "filesystem"
    assert op.operation == "write"
    assert op.resource == "/work/src/main.py"


def test_op_descriptor_network():
    op = OpDescriptor("network.egress:api.github.com:443")
    assert op.category == "network"
    assert op.operation == "egress"
    assert op.resource == "api.github.com:443"


def test_op_descriptor_rpc():
    op = OpDescriptor("rpc.method:artifact.publish")
    assert op.category == "rpc"
    assert op.operation == "method"
    assert op.resource == "artifact.publish"


def test_op_descriptor_no_resource():
    op = OpDescriptor("worker.heartbeat")
    assert op.category == "worker"
    assert op.operation == "heartbeat"
    assert op.resource is None


# ── check_op integration ────────────────────────────────────────────────────

def test_check_op_filesystem_read_allowed():
    manifest = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": True}],
        },
    })
    allowed, _ = check_op("filesystem.read:/work/src/main.py", manifest)
    assert allowed


def test_check_op_filesystem_read_denied():
    manifest = make_manifest(grants={
        "filesystem": {
            "reads": [{"path": "/work", "recursive": True}],
        },
    })
    allowed, _ = check_op("filesystem.read:/etc/passwd", manifest)
    assert not allowed


def test_check_op_filesystem_write_denied_readonly():
    manifest = make_manifest(grants={
        "filesystem": {
            "writes": [{"path": "/work", "recursive": True, "create": False}],
        },
    })
    allowed, _ = check_op("filesystem.write:/work/src/main.py", manifest)
    assert not allowed


def test_check_op_network_egress_allowed():
    manifest = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "https"}],
        },
    })
    allowed, _ = check_op("network.egress:api.github.com:443", manifest)
    assert allowed


def test_check_op_network_egress_wrong_port():
    manifest = make_manifest(grants={
        "network": {
            "egress": [{"destination": "api.github.com", "ports": [443], "protocol": "https"}],
        },
    })
    allowed, _ = check_op("network.egress:api.github.com:80", manifest)
    assert not allowed


def test_check_op_process_exec_allowed():
    manifest = make_manifest(grants={
        "process": {
            "exec": [{"binary": "/usr/bin/git"}, {"binary": "/usr/bin/pytest"}],
        },
    })
    allowed, _ = check_op("process.exec:/usr/bin/git", manifest)
    assert allowed


def test_check_op_process_exec_denied():
    manifest = make_manifest(grants={
        "process": {
            "exec": [{"binary": "/usr/bin/git"}],
        },
    })
    allowed, _ = check_op("process.exec:/usr/bin/curl", manifest)
    assert not allowed


def test_check_op_rpc_method_allowed():
    manifest = make_manifest(grants={
        "rpc": {"methods": ["artifact.*"]},
    })
    allowed, _ = check_op("rpc.method:artifact.publish", manifest)
    assert allowed


def test_check_op_rpc_method_denied():
    manifest = make_manifest(grants={
        "rpc": {"methods": ["artifact.*"]},
    })
    allowed, _ = check_op("rpc.method:worker.heartbeat", manifest)
    assert not allowed


def test_check_op_empty_grants():
    """Empty grants deny everything."""
    manifest = make_manifest(grants={
        "filesystem": {},
        "network": {"egress": []},
        "process": {"exec": []},
        "rpc": {"methods": []},
    })
    allowed, _ = check_op("filesystem.read:/tmp/test", manifest)
    assert not allowed
    allowed, _ = check_op("network.egress:example.com:80", manifest)
    assert not allowed


def test_check_op_invalid_descriptor():
    manifest = make_manifest()
    allowed, _ = check_op("not a valid descriptor", manifest)
    assert not allowed


def test_check_op_unknown_category():
    manifest = make_manifest()
    allowed, _ = check_op("unknown.op:resource", manifest)
    assert not allowed


# ── Empty grant set tests ───────────────────────────────────────────────────

def test_empty_grants_deny_all():
    """With no grants defined, all checks should fail."""
    manifest = CapabilityManifest(
        schema_version="1.0",
        subject={"kind": "bundle", "id": "empty"},
        grants={},
        metadata={"rationale": "test"},
    )
    # All default grants are empty, so everything is denied
    for op in [
        "filesystem.read:/etc/passwd",
        "network.egress:example.com:443",
        "process.exec:/bin/ls",
        "secrets.fetch:secret-token",
        "rpc.method:any.method",
    ]:
        allowed, _ = check_op(op, manifest)
        assert not allowed, f"{op} should be denied with empty grants"
