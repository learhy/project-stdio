"""CLI display formatting — pure functions that render inspection output.

All functions accept structured data and return formatted strings.
No I/O, no RPC, no side effects — callers handle data fetching.
"""

from __future__ import annotations

import json
from typing import Any


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    h, remainder = divmod(seconds, 3600)
    m = remainder // 60
    return f"{h}h {m}m"


def _now() -> int:
    import time
    return int(time.time())


# ── studio show ────────────────────────────────────────────────────────────────

def format_bundle_show(
    bundle: dict[str, Any],
    proposal: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    audit_entries: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    verbose: bool = False,
) -> str:
    """Format a bundle for the review deck display."""
    lines: list[str] = []

    bundle_id = bundle.get("id", "unknown")
    state = bundle.get("state", "unknown")
    tier = bundle.get("tier", "unknown")
    created_at = bundle.get("created_at", 0)
    age_secs = _now() - created_at if created_at else 0
    age = _format_age(age_secs) if created_at else "unknown"

    p = proposal.get("proposal", {}) if isinstance(proposal, dict) else {}
    bundle_input = proposal.get("bundle_input", {})
    idea = bundle_input.get("idea", p.get("requirements_summary", ""))

    # Header
    lines.append(f"Bundle: {bundle_id}")
    lines.append(f"State: {state} ({tier}) — age {age}")
    lines.append(f"Idea: {idea}")
    lines.append("")

    # Scores
    complexity = p.get("complexity_score", "?")
    risk = p.get("risk_score", "?")
    irreversible = "yes" if bundle.get("irreversible") else "no"
    lines.append(f"Complexity: {complexity}/10    Risk: {risk}/10    Irreversible: {irreversible}")

    # Estimates
    est_loc = p.get("estimated_loc", "?")
    est_dur = p.get("estimated_duration_seconds", 0)
    est_dur_str = _format_duration(est_dur) if est_dur else "?"
    est_workers = p.get("estimated_worker_count", "?")
    est_tokens = p.get("estimated_tokens", "?")
    lines.append(f"Estimate: {est_loc} loc · {est_dur_str} · {est_workers} worker(s) · {est_tokens} tokens")

    # Implementation plan
    plan = p.get("implementation_plan", "")
    if plan:
        if not verbose and len(plan) > 200:
            plan = plan[:197] + "..."
        lines.append(f"Plan: {plan}")

    # Concerns
    concerns = p.get("concerns", [])
    if concerns:
        if verbose:
            for c in concerns:
                lines.append(f"Concern: {c}")
        else:
            lines.append(f"Concerns: {concerns[0]}")
            if len(concerns) > 1:
                lines.append(f"  (+ {len(concerns) - 1} more, use --verbose)")

    lines.append("")

    # Review findings (from artifacts)
    if artifacts:
        lines.append("Review findings:")
        for art in artifacts:
            name = art.get("name", "")
            if name in ("adversarial-findings", "security-findings", "verification-plan"):
                lines.append(f"  {name}: present ({art.get('size_bytes', 0)} bytes)")
        lines.append("")

    # DAG summary
    total = len(nodes)
    completed = sum(1 for n in nodes if n.get("state") == "completed")
    running = sum(1 for n in nodes if n.get("state") == "running")
    pending = sum(1 for n in nodes if n.get("state") in ("pending", "ready"))
    failed = sum(1 for n in nodes if n.get("state") in ("failed", "blocked"))
    parts = [f"{total} total"]
    if completed:
        parts.append(f"{completed} completed")
    if running:
        parts.append(f"{running} running")
    if pending:
        parts.append(f"{pending} pending")
    if failed:
        parts.append(f"{failed} failed")
    lines.append(f"DAG: {', '.join(parts)}")
    lines.append("")

    # Recent events
    lines.append("Recent events:")
    if audit_entries:
        for entry in audit_entries:
            event_type = entry.get("event_type", "unknown")
            created = entry.get("created_at", 0)
            event_age = _format_age(_now() - created) if created else "?"
            payload = _parse_json(entry.get("payload_json"))
            detail = _event_detail(event_type, payload)
            lines.append(f"  {event_age} ago  {event_type}{detail}")
    else:
        lines.append("  (none)")

    lines.append("")

    # Next action hint
    if state in ("in_review", "approved", "proposed"):
        lines.append(f"Approve: studio approve {bundle_id}")
    if state == "in_progress":
        lines.append(f"Kill:    studio kill {bundle_id}")

    # Verbose: full DAG node table and edges
    if verbose:
        lines.append("")
        lines.append("─" * 60)
        lines.append("DAG nodes:")
        lines.append(f"  {'NODE ID':<24} {'KIND':<14} {'STATE':<14}")
        lines.append(f"  {'-'*24} {'-'*14} {'-'*14}")
        for n in nodes:
            nid = n.get("node_id", "?")
            kind = n.get("kind", "?")
            nstate = n.get("state", "?")
            lines.append(f"  {nid:<24} {kind:<14} {nstate:<14}")

        if edges:
            lines.append("")
            lines.append("DAG edges:")
            for e in edges:
                frm = e.get("from_node_id", "?")
                to = e.get("to_node_id", "?")
                cond = e.get("condition_kind", "?")
                lines.append(f"  {frm} → {to}  ({cond})")

        lines.append("")
        lines.append("─" * 60)
        lines.append("Full audit trail:")
        for entry in audit_entries:
            event_type = entry.get("event_type", "unknown")
            created = entry.get("created_at", 0)
            event_age = _format_age(_now() - created) if created else "?"
            payload = _parse_json(entry.get("payload_json"))
            lines.append(f"  {event_age} ago  {event_type}")
            if payload:
                lines.append(f"           {json.dumps(payload)}")

    return "\n".join(lines)


# ── studio list ────────────────────────────────────────────────────────────────

def format_bundle_list(bundles: list[dict[str, Any]]) -> str:
    """Format bundle list as a table with truncated fields."""
    if not bundles:
        return "No bundles found."

    lines = [f"{'ID':<22} {'STATE':<14} {'TIER':<18} {'AGE':<6} IDEA"]
    for b in bundles:
        bid = b.get("id", "")
        if len(bid) > 20:
            bid = bid[:19] + "…"
        state = b.get("state", "")
        tier = b.get("tier", "")
        age = b.get("age", "")
        idea = b.get("idea", "")
        if len(idea) > 60:
            idea = idea[:57] + "…"
        lines.append(f"{bid:<22} {state:<14} {tier:<18} {age:<6} {idea}")
    return "\n".join(lines)


# ── studio show-worker ──────────────────────────────────────────────────────────

def format_worker_show(
    worker: dict[str, Any],
    node: dict[str, Any] | None,
    cap_checks: dict[str, int],
) -> str:
    """Format worker detail display."""
    lines: list[str] = []

    wid = worker.get("id", "unknown")
    bundle_id = worker.get("bundle_id", "unknown")
    node_id = worker.get("node_id", "unknown")
    state = worker.get("state", "unknown")
    phase = worker.get("current_phase") or "unknown"
    created_at = worker.get("created_at", 0)
    ended_at = worker.get("ended_at")
    last_hb = worker.get("last_heartbeat")

    lines.append(f"Worker: {wid}")
    lines.append(f"Bundle: {bundle_id}")
    lines.append(f"Node:   {node_id}")
    lines.append(f"State:  {state}")
    lines.append(f"Phase:  {phase}")
    runner_type = worker.get("runner_type")
    if runner_type:
        lines.append(f"Runner: {runner_type}")

    # Age / runtime
    age_secs = _now() - created_at if created_at else 0
    if ended_at:
        runtime = ended_at - created_at
        lines.append(f"Age:    {_format_age(age_secs)} (runtime {_format_duration(runtime)})")
    elif state in ("running", "pending"):
        lines.append(f"Age:    {_format_age(age_secs)} (running)")
    else:
        lines.append(f"Age:    {_format_age(age_secs)}")

    # Heartbeat
    if last_hb:
        hb_ago = _now() - last_hb
        lines.append(f"Heartbeat: {_format_age(hb_ago)} ago")
    elif state in ("running", "pending"):
        lines.append("Heartbeat: never")

    # Exit reason
    exit_reason = worker.get("exit_reason")
    if exit_reason:
        lines.append(f"Exit:   {exit_reason}")

    lines.append("")

    # Task spec summary
    if node and node.get("spec_json"):
        try:
            spec = json.loads(node["spec_json"])
            objective = spec.get("objective", "")
            if objective:
                if len(objective) > 120:
                    objective = objective[:117] + "..."
                lines.append(f"Task: {objective}")
        except (json.JSONDecodeError, TypeError):
            pass
    elif node_id == "bundler":
        lines.append("Task: [bundler — produces proposal + DAG from bundle_input]")

    # Capability manifest summary
    manifest_json = worker.get("manifest_json", "{}")
    try:
        manifest = json.loads(manifest_json) if isinstance(manifest_json, str) else manifest_json
        grants = manifest.get("grants", {})
        if grants:
            parts = []
            fs = grants.get("filesystem", {})
            if fs.get("reads"):
                parts.append(f"{len(fs['reads'])} reads")
            if fs.get("writes"):
                parts.append(f"{len(fs['writes'])} writes")
            net = grants.get("network", {})
            if net.get("egress"):
                parts.append(f"{len(net['egress'])} egress endpoints")
            proc = grants.get("process", {})
            if proc.get("exec"):
                parts.append(f"{len(proc['exec'])} exec binaries")
            rpc = grants.get("rpc", {})
            if rpc.get("methods"):
                parts.append(f"{len(rpc['methods'])} rpc methods")
            if parts:
                lines.append(f"Capabilities: {', '.join(parts)}")
            else:
                lines.append("Capabilities: minimal")
        else:
            lines.append("Capabilities: no manifest")
    except (json.JSONDecodeError, TypeError, AttributeError):
        lines.append("Capabilities: no manifest")

    # Capability checks
    allowed = cap_checks.get("allowed", 0)
    denied = cap_checks.get("denied", 0)
    if allowed or denied:
        lines.append(f"Cap checks: {allowed} allowed, {denied} denied")

    # Output summary (if terminal)
    if node and node.get("output_json"):
        try:
            output = json.loads(node["output_json"])
            if output.get("files_changed"):
                lines.append(f"Files changed: {len(output['files_changed'])}")
            if output.get("tests_run"):
                lines.append(f"Tests: {output.get('tests_passed', 0)}/{output['tests_run']} passed")
            if output.get("outcome"):
                lines.append(f"Outcome: {output['outcome']}")
        except (json.JSONDecodeError, TypeError):
            pass

    return "\n".join(lines)


# ── studio health ──────────────────────────────────────────────────────────────

def format_health(snap: dict[str, Any]) -> str:
    """Format health dashboard display."""
    lines: list[str] = []

    orch = "OK" if snap.get("orchestrator_ok") else "DEGRADED"
    db = "OK" if snap.get("db_ok") else "FAILED"
    uptime = snap.get("uptime_seconds", 0)
    uptime_str = _format_duration(int(uptime)) if uptime else "0s"

    lines.append(f"Orchestrator: {orch} | DB: {db} | Uptime: {uptime_str}")

    total = snap.get("total_bundles", 0)
    active = snap.get("active_bundles", 0)
    stalled = snap.get("stalled_bundles", 0)
    by_state = snap.get("by_state", {})
    in_progress = by_state.get("in_progress", 0)
    in_review = by_state.get("in_review", 0)

    lines.append(f"Bundles: {total} total, {in_progress} in_progress, {in_review} in_review, {stalled} stalled")

    # by_state / by_tier as aligned mini-tables
    by_tier = snap.get("by_tier", {})
    if by_state or by_tier:
        max_state_len = max((len(k) for k in by_state), default=0)
        max_tier_len = max((len(k) for k in by_tier), default=0)
        lines.append("")
        lines.append(f"  {'By state':<{max_state_len + 4}} {'By tier':<{max_tier_len + 4}}")
        lines.append(f"  {'-' * max_state_len}  -----  {'-' * max_tier_len}  ----")

        state_keys = sorted(by_state.keys())
        tier_keys = sorted(by_tier.keys())
        for i in range(max(len(state_keys), len(tier_keys))):
            s = f"  {state_keys[i]:<{max_state_len}}  {by_state.get(state_keys[i], 0):>5}" if i < len(state_keys) else ""
            t = f"  {tier_keys[i]:<{max_tier_len}}  {by_tier.get(tier_keys[i], 0):>4}" if i < len(tier_keys) else ""
            lines.append(f"{s}    {t}")

    # Calibration
    cal = snap.get("calibration", {})
    if cal:
        total_outcomes = cal.get("total_outcomes", 0)
        pass_rate = cal.get("pass_rate", "N/A")
        if isinstance(pass_rate, float):
            pass_rate_str = f"{pass_rate:.0%}"
        else:
            pass_rate_str = str(pass_rate)
        lines.append(f"\nCalibration: {total_outcomes} outcomes, pass rate {pass_rate_str}")

    # Recent errors
    errors = snap.get("recent_errors", [])
    if errors:
        lines.append("Recent errors:")
        for e in errors[-5:]:
            lines.append(f"  - {e}")
    else:
        lines.append("Recent errors: (none)")

    return "\n".join(lines)


# ── studio status ──────────────────────────────────────────────────────────────

def format_status(uptime: float, worker_count: int, queue_depth: int,
                  listeners: list[str] | None = None) -> str:
    """Format terse status line."""
    uptime_str = _format_duration(int(uptime)) if uptime else "0s"
    base = f"Orchestrator: running | Uptime: {uptime_str} | Workers: {worker_count} running | Queue: {queue_depth}"
    if listeners:
        base += "\nListeners: " + ", ".join(listeners)
    return base


# ── studio calibration-report ──────────────────────────────────────────────────

def format_calibration(data: dict[str, Any]) -> str:
    """Format calibration report."""
    lines: list[str] = []

    if not data.get("entries") and data.get("message"):
        return data["message"]

    total = data.get("total_entries", 0)
    diverged = data.get("entries_with_divergence", 0)
    lines.append(f"Calibration: {total} entries, {diverged} with divergence")

    recent = data.get("recent", [])
    if recent:
        lines.append("")
        lines.append("Recent:")
        for entry in recent[-10:]:
            bid = entry.get("bundle_id", "unknown")
            if len(bid) > 10:
                bid = bid[:8] + "…"

            est = entry.get("estimated", {})
            act = entry.get("actual", {})

            est_parts = []
            if est:
                if "loc" in est:
                    est_parts.append(f"loc={est['loc']}")
                if "duration_seconds" in est:
                    est_parts.append(f"dur={_format_duration(est['duration_seconds'])}")
                if "tokens" in est:
                    est_parts.append(f"tok={est['tokens']}")
            est_str = ", ".join(est_parts) if est_parts else "?"

            act_parts = []
            if act:
                if "loc" in act:
                    act_parts.append(f"loc={act['loc']}")
                if "duration_seconds" in act:
                    act_parts.append(f"dur={_format_duration(act['duration_seconds'])}")
                if "tokens" in act:
                    act_parts.append(f"tok={act['tokens']}")
            act_str = ", ".join(act_parts) if act_parts else "?"

            lines.append(f"  {bid}  est: {est_str}  act: {act_str}")

    # ── Bundle 5.4: Review quality ──────────────────────────────────────────

    rq = data.get("review_quality")
    if rq:
        lines.append("")
        lines.append("Review quality:")
        lines.append(f"  Intervention rate: {rq.get('intervention_rate', 0)}/bundle "
                     f"({rq.get('total_interventions', 0)} over {rq.get('total_bundles_with_interventions', 0)} bundles)")
        lines.append(f"  LLM answer rate: {rq.get('llm_answer_rate', 0)}%")

        avg_resp = rq.get("avg_escalation_response_minutes", 0)
        lines.append(f"  Avg escalation response time: {avg_resp} min")

        acc = rq.get("accuracy_rate")
        if acc is not None:
            lines.append(f"  Review accuracy: {acc}% good ({rq.get('good_count', 0)}/{rq.get('total_feedback', 0)} feedback)")
        else:
            lines.append("  Review accuracy: N/A (no feedback yet)")

        # Recommendations based on feedback signals (resolution #4)
        noisy_rate = rq.get("noisy_rate", 0)
        missed_rate = rq.get("missed_rate", 0)
        if noisy_rate > 0.5:
            lines.append("  Consider raising review.confidence_threshold (noisy rate: "
                         f"{noisy_rate:.0%})")
        if missed_rate > 0.3:
            lines.append("  Consider lowering review.confidence_threshold (missed rate: "
                         f"{missed_rate:.0%})")

    # ── Bundle 6.4: Code quality metrics ──────────────────────────────────

    cq = data.get("code_quality")
    if cq:
        lines.append("")
        lines.append("Code quality:")
        fapr = cq.get("first_attempt_pass_rate")
        if fapr is not None:
            lines.append(f"  First-attempt verification pass rate: {fapr}% (target: >80%)")
        else:
            lines.append("  First-attempt verification pass rate: N/A")
        afa = cq.get("avg_fix_attempts")
        if afa is not None:
            lines.append(f"  Average fix attempts before pass: {afa} (target: <2.0)")

        qac = cq.get("qa_criterion_pass_rate")
        if qac is not None:
            lines.append(f"  QA criterion pass rate: {qac}%")

        mcf = cq.get("most_common_failure_category")
        mcfp = cq.get("most_common_failure_pct", 0)
        if mcf is not None:
            cat_label = mcf.replace("_", " ").title()
            lines.append(f"  Most common failure category: {cat_label} ({mcfp}%)")

        scs = cq.get("spec_clarity_score")
        tv = cq.get("total_verified", 0)
        if scs is not None:
            lines.append(f"  Spec clarity score: {scs}% (based on {tv} bundles)")
        elif tv > 0:
            lines.append(f"  Spec clarity score: N/A (need 5+ bundles, have {tv})")

        recs = cq.get("recommendations", [])
        if recs:
            lines.append("")
            lines.append("Recommendations:")
            for r in recs:
                lines.append(f"  - {r}")

    return "\n".join(lines)


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_json(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _event_detail(event_type: str, payload: dict) -> str:
    """Return a human-readable suffix for an audit event."""
    if event_type == "bundle_input_received":
        state = payload.get("state", "")
        mode = payload.get("mode", "")
        detail = f" — {state}"
        if mode:
            detail += f" ({mode})"
        return detail
    if event_type in ("bundle_planning_complete", "pre_execution_review_started",
                       "execution_started", "all_exit_nodes_terminal",
                       "verification_passed", "bundle_failed_during_execution"):
        frm = payload.get("from_state", "")
        to = payload.get("to_state", "")
        if frm and to:
            return f" — {frm} → {to}"
        return ""
    return ""
