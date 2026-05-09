"""Tests for Mermaid DAG renderer."""
import pytest
from studio.orchestrator.visualizer import render_dag, _shape_for_kind


class TestShapeForKind:
    def test_worker_rectangle(self):
        assert _shape_for_kind("worker") == ("[", "]")

    def test_gate_diamond(self):
        assert _shape_for_kind("gate") == ("{", "}")

    def test_aggregator_hexagon(self):
        assert _shape_for_kind("aggregator") == ("{{", "}}")


class TestRenderDag:
    def test_empty_nodes(self):
        result = render_dag([], [])
        assert "empty" in result.lower()
        assert "```mermaid" in result

    def test_single_worker_node(self):
        nodes = [{"node_id": "task-1", "kind": "worker", "state": "pending", "spec": {}}]
        result = render_dag(nodes, [])
        assert "task-1[" in result
        assert "```mermaid" in result

    def test_gate_node_renders_as_diamond(self):
        nodes = [{"node_id": "gate-1", "kind": "gate", "state": "pending", "spec": {}}]
        result = render_dag(nodes, [])
        assert "gate-1{" in result
        assert "GATE:" in result

    def test_aggregator_node_renders_as_hexagon(self):
        nodes = [{"node_id": "agg-1", "kind": "aggregator", "state": "pending", "spec": {}}]
        result = render_dag(nodes, [])
        assert "agg-1{{" in result
        assert "AGG:" in result

    def test_state_colors_applied(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}}]
        result = render_dag(nodes, [])
        assert "#d4edda" in result  # green fill for completed

    def test_failed_state_red(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "failed", "spec": {}}]
        result = render_dag(nodes, [])
        assert "#f8d7da" in result  # red fill for failed

    def test_running_state_blue(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "running", "spec": {}}]
        result = render_dag(nodes, [])
        assert "#cce5ff" in result

    def test_multiple_nodes(self):
        nodes = [
            {"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}},
            {"node_id": "n2", "kind": "gate", "state": "running", "spec": {}},
            {"node_id": "n3", "kind": "aggregator", "state": "pending", "spec": {}},
        ]
        result = render_dag(nodes, [])
        assert "n1[" in result
        assert "n2{" in result
        assert "n3{{" in result

    def test_on_success_edge(self):
        nodes = [
            {"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}},
            {"node_id": "n2", "kind": "worker", "state": "pending", "spec": {}},
        ]
        edges = [{"from_node_id": "n1", "to_node_id": "n2", "condition_kind": "on_success"}]
        result = render_dag(nodes, edges)
        assert "n1 --> n2" in result

    def test_on_failure_edge(self):
        nodes = [
            {"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}},
            {"node_id": "n2", "kind": "worker", "state": "pending", "spec": {}},
        ]
        edges = [{"from_node_id": "n1", "to_node_id": "n2", "condition_kind": "on_failure"}]
        result = render_dag(nodes, edges)
        assert "-.->" in result
        assert "failure" in result

    def test_always_edge(self):
        nodes = [
            {"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}},
            {"node_id": "n2", "kind": "worker", "state": "pending", "spec": {}},
        ]
        edges = [{"from_node_id": "n1", "to_node_id": "n2", "condition_kind": "always"}]
        result = render_dag(nodes, edges)
        assert "==>" in result

    def test_on_property_edge_with_label(self):
        nodes = [
            {"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}},
            {"node_id": "n2", "kind": "worker", "state": "pending", "spec": {}},
        ]
        edges = [{
            "from_node_id": "n1", "to_node_id": "n2",
            "condition_kind": "on_property",
            "condition_expr": "exit_code == 0",
        }]
        result = render_dag(nodes, edges)
        assert "exit_code == 0" in result

    def test_entry_node_marker(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "pending", "spec": {}}]
        result = render_dag(nodes, [], entry_nodes=["n1"])
        assert "entry_n1" in result
        assert "entry_n1 --> n1" in result

    def test_exit_node_marker(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "completed", "spec": {}}]
        result = render_dag(nodes, [], exit_nodes=["n1"])
        assert "exit_n1" in result
        assert "n1 --> exit_n1" in result

    def test_grafted_node_style(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "pending", "spec": {}}]
        result = render_dag(nodes, [], grafted_node_ids={"n1"})
        assert "[grafted]" in result
        assert "stroke-width: 3px" in result

    def test_objective_in_label(self):
        nodes = [{
            "node_id": "n1", "kind": "worker", "state": "pending",
            "spec": {"objective": "Build the thing"},
        }]
        result = render_dag(nodes, [])
        assert "Build the thing" in result

    def test_long_objective_truncated(self):
        nodes = [{
            "node_id": "n1", "kind": "worker", "state": "pending",
            "spec": {"objective": "A" * 60},
        }]
        result = render_dag(nodes, [])
        assert "..." in result

    def test_cancelled_state_dashed_border(self):
        nodes = [{"node_id": "n1", "kind": "worker", "state": "cancelled", "spec": {}}]
        result = render_dag(nodes, [])
        assert "stroke-dasharray" in result
