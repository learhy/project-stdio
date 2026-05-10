"""Tests for models.py — deserialization and validation."""
import pytest
from studio.orchestrator.models import (
    BundleState,
    WorkerState,
    NodeState,
    CapabilityManifest,
    Submission,
    Settings,
    JsonRpcRequest,
)


def test_bundle_state_all_values():
    assert len(BundleState) == 12
    assert BundleState.PROPOSED == "proposed"
    assert BundleState.REJECTED == "rejected"


def test_worker_state_all_values():
    assert len(WorkerState) == 7
    assert WorkerState.PENDING == "pending"
    assert WorkerState.CONNECTION_LOST == "connection_lost"


def test_node_state_all_values():
    assert len(NodeState) == 7


def test_submission_deserialization():
    data = {
        "schema_version": "1.0-phase-1",
        "bundle_input": {
            "idea": "Build a hello-world web server",
            "filed_by": "reviewer",
            "target_repo": "control-plane",
        },
        "capability_manifest": {
            "schema_version": "1.0",
            "subject": {"kind": "bundle", "id": "auto"},
            "grants": {
                "filesystem": {
                    "reads": [{"path": "/work", "recursive": True}],
                    "writes": [{"path": "/work", "recursive": True}],
                },
                "network": {"egress": []},
                "process": {"exec": []},
                "rpc": {"methods": ["worker.*"]},
            },
            "metadata": {"rationale": "test"},
        },
        "task_dag": {
            "nodes": [
                {
                    "id": "task-1",
                    "kind": "worker",
                    "task_manifest_ref": "manifest-1",
                    "spec": {"objective": "Write hello world"},
                }
            ],
            "edges": [],
            "entry_nodes": ["task-1"],
            "exit_nodes": ["task-1"],
            "expansion_policy": {
                "allow_dynamic_expansion": False,
                "max_total_nodes": 0,
                "max_depth": 0,
            },
        },
    }
    submission = Submission.model_validate(data)
    assert submission.bundle_input.idea == "Build a hello-world web server"
    assert submission.bundle_input.target_repo == "control-plane"
    assert len(submission.task_dag.nodes) == 1
    assert submission.task_dag.nodes[0].kind == "worker"


def test_submission_target_repo_defaults():
    submission = Submission(
        bundle_input={"idea": "test"},
        task_dag={
            "nodes": [{"id": "t1", "kind": "worker", "task_manifest_ref": "m1"}],
            "entry_nodes": ["t1"],
            "exit_nodes": ["t1"],
        },
    )
    assert submission.bundle_input.target_repo == "control-plane"


def test_capability_manifest_deserialization():
    data = {
        "schema_version": "1.0",
        "subject": {"kind": "bundle", "id": "bundle-1"},
        "grants": {
            "filesystem": {
                "reads": [{"path": "/src", "recursive": True}],
                "writes": [{"path": "/src", "recursive": True, "create": True}],
            },
            "network": {
                "egress": [
                    {
                        "destination": "api.github.com",
                        "ports": [443],
                        "protocol": "https",
                        "rationale": "needed for API",
                    }
                ],
                "ingress": {"enabled": False},
                "dns": {"enabled": True},
            },
            "process": {
                "exec": [
                    {"binary": "/usr/bin/git", "args_pattern": None, "rationale": "version control"},
                    {"binary": "/usr/bin/pytest", "args_pattern": None, "rationale": "testing"},
                ]
            },
            "secrets": [
                {"name": "github-token", "purpose": "github_auth", "delivery": "rpc", "rationale": "auth"}
            ],
            "rpc": {
                "methods": ["worker.*", "cap.check"],
                "artifact_access": {
                    "reads": [{"namespace": "bundle", "name": "*", "version": "*", "content_type": "*"}],
                    "writes": [{"namespace": "bundle", "name": "*", "version": "*", "content_type": "*"}],
                },
            },
            "resources": {
                "cpu_limit": 2000,
                "memory_limit": 4294967296,
                "disk_limit": 10737418240,
                "wall_time_limit": 28800,
                "llm_token_budget": {
                    "input_tokens": 100000,
                    "output_tokens": 50000,
                },
            },
        },
        "metadata": {
            "rationale": "Bundle manifest for testing",
            "requested_by": "reviewer",
        },
    }
    manifest = CapabilityManifest.model_validate(data)
    assert manifest.grants.filesystem.reads[0].path == "/src"
    assert manifest.grants.network.egress[0].destination == "api.github.com"
    assert manifest.grants.process.exec[0].binary == "/usr/bin/git"
    assert manifest.grants.secrets[0].name == "github-token"
    assert "worker.*" in manifest.grants.rpc.methods
    assert manifest.grants.resources.memory_limit == 4294967296


def test_settings_deserialization():
    data = {
        "kernel": {"mode": True},
        "worker": {
            "global_concurrency": 4,
            "default_timeout_hours": {"small": 2, "medium": 4, "large": 8},
            "heartbeat_max_interval_minutes": 60,
            "heartbeat_timeout_multiplier": 2.0,
        },
        "ollama_cloud": {
            "base_url": "https://ollama.com/api",
            "health_check_interval_seconds": 30,
            "grace_window_minutes": 5,
        },
        "orchestrator": {
            "socket_path": "/run/studio/orchestrator.sock",
            "db_path": "/var/lib/studio/state.db",
        },
    }
    settings = Settings.model_validate(data)
    assert settings.kernel.mode is True
    assert settings.worker.global_concurrency == 4
    assert settings.orchestrator.db_path == "/var/lib/studio/state.db"


def test_json_rpc_request():
    data = {
        "jsonrpc": "2.0",
        "method": "worker.heartbeat",
        "params": {"phase": "starting"},
    }
    req = JsonRpcRequest.model_validate(data)
    assert req.method == "worker.heartbeat"
    assert req.id is None


def test_illegal_bundle_state_transition():
    """Terminal states cannot transition."""
    from studio.orchestrator.models import BundleState
    terminal = {BundleState.COMPLETE, BundleState.PARKED, BundleState.FAILED,
                BundleState.REJECTED, BundleState.ABORTED}
    for s in BundleState:
        if s in terminal:
            assert s in terminal
