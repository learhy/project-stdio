"""Mermaid DAG renderer — pure function of DAG state, no side effects.

Rendering rules from the spec:
  Workers: rectangles, Gates: diamonds, Aggregators: hexagons.
  State colors: pending=white, ready=yellow, running=blue, completed=green,
                failed=red, skipped=grey, cancelled=grey+dashed.
  Edge: on_success=solid, on_failure=dashed+red, always=thick+solid,
         on_property=solid+label.
  Grafted nodes: doubled border (via CSS class).
  Entry nodes preceded by filled circle; exit nodes followed by ringed circle.
"""
from __future__ import annotations

from typing import Any

_STATE_FILL: dict[str, str] = {
    "pending": "#ffffff",
    "ready": "#ffffcc",
    "running": "#cce5ff",
    "completed": "#d4edda",
    "failed": "#f8d7da",
    "skipped": "#e2e3e5",
    "cancelled": "#e2e3e5",
}

_STATE_BORDER: dict[str, str] = {
    "pending": "#999999",
    "ready": "#ffc107",
    "running": "#007bff",
    "completed": "#28a745",
    "failed": "#dc3545",
    "skipped": "#6c757d",
    "cancelled": "#6c757d",
}

_CONDITION_EDGE_STYLE: dict[str, dict[str, str]] = {
    "on_success": {"style": "solid", "color": "#333333", "label": ""},
    "on_failure": {"style": "dashed", "color": "#dc3545", "label": "failure"},
    "always": {"style": "bold", "color": "#333333", "label": "always"},
    "on_property": {"style": "solid", "color": "#333333", "label": ""},
}


def render_dag(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    entry_nodes: list[str] | None = None,
    exit_nodes: list[str] | None = None,
    grafted_node_ids: set[str] | None = None,
) -> str:
    """Render a bundle DAG as a Mermaid flowchart.

    Each node dict: {node_id, kind, state, spec?}
    Each edge dict: {from_node_id, to_node_id, condition_kind, condition_expr?}
    """
    if not nodes:
        return "```mermaid\ngraph TD\n  empty[No nodes]\n```"

    entry_set = set(entry_nodes or [])
    exit_set = set(exit_nodes or [])
    grafted = grafted_node_ids or set()

    lines = ["```mermaid", "graph TD"]

    # Render nodes
    for n in nodes:
        nid = n.get("node_id", n.get("id", "?"))
        kind = n.get("kind", "worker")
        state = n.get("state", "pending")
        label = n.get("node_id", n.get("id", "?"))
        spec = n.get("spec", {})
        objective = spec.get("objective", "") if isinstance(spec, dict) else ""

        fill = _STATE_FILL.get(state, "#ffffff")
        border = _STATE_BORDER.get(state, "#999999")
        stroke_dash = "stroke-dasharray: 5 5;" if state == "cancelled" else ""
        is_grafted = nid in grafted
        graft_style = "stroke-width: 3px;" if is_grafted else ""

        shape_open, shape_close = _shape_for_kind(kind)
        display_label = _make_label(label, objective, kind, is_grafted)

        lines.append(
            f'  {nid}{shape_open}["{display_label}"]{shape_close}'
        )
        lines.append(
            f'  style {nid} fill:{fill},stroke:{border},{stroke_dash}{graft_style}'
        )

    # Entry/exit markers
    for en in entry_set:
        marker_id = f"entry_{en}"
        lines.append(f'  {marker_id}(( ))')
        lines.append(f'  style {marker_id} fill:#333333,stroke:#333333')
        lines.append(f'  {marker_id} --> {en}')

    for ex in exit_set:
        marker_id = f"exit_{ex}"
        lines.append(f'  {marker_id}(( ))')
        lines.append(f'  style {marker_id} fill:#ffffff,stroke:#333333,stroke-width:2px')
        lines.append(f'  {ex} --> {marker_id}')

    # Render edges
    for e in edges:
        src = e.get("from_node_id", e.get("from", "?"))
        dst = e.get("to_node_id", e.get("to", "?"))
        cond_kind = e.get("condition_kind", "on_success")
        cond_expr = e.get("condition_expr", "")

        edge_conf = _CONDITION_EDGE_STYLE.get(cond_kind, _CONDITION_EDGE_STYLE["on_success"])
        edge_style = edge_conf["style"]
        edge_color = edge_conf["color"]
        edge_label = edge_conf["label"]

        if cond_kind == "on_property" and cond_expr:
            # Truncate long expressions
            short_expr = cond_expr[:40] + ("..." if len(cond_expr) > 40 else "")
            edge_label = short_expr

        if edge_style == "bold":
            arrow = "==>"
        elif edge_style == "dashed":
            arrow = "-.->"
        else:
            arrow = "-->"

        if edge_label:
            lines.append(f'  {src} {arrow}|"{edge_label}"| {dst}')
        else:
            lines.append(f"  {src} {arrow} {dst}")

        if cond_kind == "on_failure":
            lines.append(f"  linkStyle {len(lines) - len([l for l in lines if '-->' in l or '-.->' in l or '==>' in l])} stroke:{edge_color},stroke-dasharray: 5 5")

    lines.append("```")
    return "\n".join(lines)


def _shape_for_kind(kind: str) -> tuple[str, str]:
    if kind == "gate":
        return ("{", "}")  # diamond
    elif kind == "aggregator":
        return ("{{", "}}")  # hexagon
    return ("[", "]")  # rectangle


def _make_label(node_id: str, objective: str, kind: str, is_grafted: bool) -> str:
    prefix = {"gate": "GATE: ", "aggregator": "AGG: "}.get(kind, "")
    label = prefix + node_id
    if objective:
        short = (objective[:50] + "...") if len(objective) > 50 else objective
        label += f"\\n{short}"
    if is_grafted:
        label += " [grafted]"
    return label
