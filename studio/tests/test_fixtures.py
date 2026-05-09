"""Validate that test fixture JSON files conform to the Submission schema."""
import json
from pathlib import Path

import pytest

from studio.orchestrator.models import Submission
from studio.orchestrator.state_machine import _validate_linear_dag, IllegalTransitionError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _nodes_and_edges(sub: Submission) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"node_id": n.id, "kind": n.kind, "spec": n.spec.model_dump()}
        for n in sub.task_dag.nodes
    ]
    edges = [
        {"from_node_id": e.from_, "to_node_id": e.to,
         "condition": {"kind": e.condition.get("kind", "on_success")}}
        for e in sub.task_dag.edges
    ]
    return nodes, edges


class TestFixtures:
    def test_hello_world_parses(self):
        data = _load("hello-world.json")
        sub = Submission.model_validate(data)
        assert sub.bundle_input.idea == "Add a hello-world endpoint to the API"
        assert len(sub.task_dag.nodes) == 1
        assert sub.task_dag.nodes[0].id == "implement-hello"

    def test_linear_three_node_parses(self):
        data = _load("linear-three-node.json")
        sub = Submission.model_validate(data)
        assert len(sub.task_dag.nodes) == 3
        assert len(sub.task_dag.edges) == 2
        assert sub.task_dag.entry_nodes == ["extract"]
        assert sub.task_dag.exit_nodes == ["load"]
        edge_froms = {e.from_ for e in sub.task_dag.edges}
        edge_tos = {e.to for e in sub.task_dag.edges}
        assert edge_froms == {"extract", "transform"}
        assert edge_tos == {"transform", "load"}

    def test_failing_worker_parses(self):
        data = _load("failing-worker.json")
        sub = Submission.model_validate(data)
        assert sub.bundle_input.priority_hint == "low"
        assert sub.task_dag.nodes[0].id == "doomed-task"
        assert sub.capability_manifest.grants.resources.wall_time_limit == 60

    def test_invalid_manifest_parses(self):
        """The invalid-manifest fixture parses as a Submission (overbroad grants are
        structurally valid; rejection happens at the policy/enforcement layer)."""
        data = _load("invalid-manifest.json")
        sub = Submission.model_validate(data)
        fs = sub.capability_manifest.grants.filesystem
        assert len(fs.writes) == 2
        root_writes = [w for w in fs.writes if w.path == "/"]
        assert len(root_writes) == 1

    def test_non_linear_dag_rejected_at_submit(self):
        """The non-linear fixture parses as valid JSON but must be REJECTED by the
        Phase 1 linear-DAG validator at submit time (before any DB writes)."""
        data = _load("non-linear-dag-rejected.json")
        sub = Submission.model_validate(data)
        assert len(sub.task_dag.nodes) == 4
        assert len(sub.task_dag.edges) == 4

        nodes, edges = _nodes_and_edges(sub)
        with pytest.raises(IllegalTransitionError, match="Non-linear DAG not supported"):
            _validate_linear_dag(nodes, edges)

    def test_all_fixtures_have_phase1_schema(self):
        """Every fixture must declare schema_version '1.0-phase-1'."""
        for name in [
            "hello-world.json",
            "linear-three-node.json",
            "failing-worker.json",
            "invalid-manifest.json",
            "non-linear-dag-rejected.json",
        ]:
            data = _load(name)
            assert data["schema_version"] == "1.0-phase-1", f"{name} has wrong schema_version"
