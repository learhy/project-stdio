"""CLI entry point: 8 commands for bundle lifecycle management.

Talks to the orchestrator over its Unix domain socket via JSON-RPC 2.0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any


async def _send_rpc(socket_path: str, method: str, params: dict[str, Any] | None = None) -> dict:
    """Send a JSON-RPC request over the Unix socket and return the result."""
    reader, writer = await asyncio.open_unix_connection(socket_path)

    msg = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }
    writer.write((json.dumps(msg) + "\n").encode())
    await writer.drain()

    line = await reader.readline()
    writer.close()
    await writer.wait_closed()

    if not line:
        return {"error": {"code": -1, "message": "No response from orchestrator"}}

    return json.loads(line.decode("utf-8"))


def _get_socket_path() -> str:
    return os.environ.get("STUDIO_SOCKET_PATH", "/run/studio/orchestrator.sock")


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_submit(path: str) -> int:
    """Submit a bundle JSON file."""
    try:
        raw = open(path).read()
        submission = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading submission file: {e}", file=sys.stderr)
        return 1

    resp = await _send_rpc(_get_socket_path(), "studio.submit",
                           {"submission": submission})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    bundle_id = resp.get("result", {}).get("bundle_id", "unknown")
    print(f"Bundle submitted: {bundle_id}")
    return 0


async def cmd_approve(bundle_id: str) -> int:
    """Approve a bundle in PROPOSED state."""
    resp = await _send_rpc(_get_socket_path(), "studio.approve",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    print(f"Bundle {bundle_id} approved. Starting execution.")
    return 0


async def cmd_reject(bundle_id: str, reason: str = "") -> int:
    """Reject a bundle in PROPOSED state."""
    resp = await _send_rpc(_get_socket_path(), "studio.reject",
                           {"bundle_id": bundle_id, "reason": reason or "rejected via CLI"})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    print(f"Bundle {bundle_id} rejected.")
    return 0


async def cmd_list(state: str | None = None, json_output: bool = False) -> int:
    """List non-terminal bundles."""
    params: dict[str, Any] = {}
    if state:
        params["state"] = state

    resp = await _send_rpc(_get_socket_path(), "studio.list", params)
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    bundles = resp.get("result", {}).get("bundles", [])
    if json_output:
        print(json.dumps(bundles, indent=2))
    else:
        if not bundles:
            print("No bundles found.")
        else:
            print(f"{'ID':<28} {'STATE':<14} {'AGE':<8} IDEA")
            for b in bundles:
                print(f"{b['id']:<28} {b['state']:<14} {b.get('age', ''):<8} {b.get('idea', '')}")
    return 0


async def cmd_show(bundle_id: str) -> int:
    """Show bundle detail."""
    resp = await _send_rpc(_get_socket_path(), "studio.show",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    print(f"Bundle: {data.get('bundle_id', bundle_id)}")
    print(f"State: {data.get('state', 'unknown')}")
    if data.get("idea"):
        print(f"Idea: {data['idea']}")
    if data.get("nodes"):
        nodes = data["nodes"]
        total = len(nodes)
        completed = sum(1 for n in nodes if n["state"] == "completed")
        running = sum(1 for n in nodes if n["state"] == "running")
        pending = sum(1 for n in nodes if n["state"] in ("pending", "ready"))
        print(f"Nodes: {total} total, {completed} completed, {running} running, {pending} pending")
    return 0


async def cmd_show_worker(worker_id: str) -> int:
    """Show worker detail."""
    resp = await _send_rpc(_get_socket_path(), "studio.show_worker",
                           {"worker_id": worker_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    print(f"Worker:       {data.get('worker_id', worker_id)}")
    print(f"Bundle:       {data.get('bundle_id', 'unknown')}")
    print(f"State:        {data.get('state', 'unknown')}")
    print(f"Phase:        {data.get('phase', 'unknown')}")
    print(f"Last hb:      {data.get('last_heartbeat_ago', 'unknown')}")
    logs = data.get("recent_logs", [])
    if logs:
        print("Log (last 20 lines):")
        for log in logs:
            print(f"  [{log.get('level', 'info')}] {log.get('message', '')}")
    return 0


async def cmd_kill(bundle_id: str) -> int:
    """Kill a running bundle."""
    resp = await _send_rpc(_get_socket_path(), "studio.kill",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    worker_count = data.get("workers_killed", 0)
    print(f"Sending SIGTERM to {worker_count} worker(s)...")
    print(f"Bundle {bundle_id} failed.")
    return 0


async def cmd_deck(bundle_id: str) -> int:
    """Print the full review deck for a bundle."""
    resp = await _send_rpc(_get_socket_path(), "studio.deck",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    deck = resp.get("result", {})
    _print_deck(deck)
    return 0


def _print_deck(deck: dict) -> None:
    """Render a review deck to the terminal."""
    print(f"══════════════════════════════════════════════════════════════")
    print(f"  REVIEW DECK: {deck.get('bundle_id', 'unknown')}")
    print(f"══════════════════════════════════════════════════════════════")
    print(f"  Tier:       {deck.get('tier', 'unknown')}")
    print(f"  Auto-ship:  {deck.get('auto_ship', False)}")
    print(f"  State:      {deck.get('state', 'unknown')}")
    print(f"  Cooldown:   {deck.get('cooldown', 'none')}")
    print()

    proposal = deck.get("proposal", {})
    print(f"── Proposal ──────────────────────────────────────────────")
    print(f"  Idea: {proposal.get('idea', '(no idea)')}")
    print()

    rec = deck.get("recommendation", {})
    print(f"── Recommendation + Confidence ───────────────────────────")
    print(f"  Complexity: {rec.get('complexity_score', '?')}/10  Risk: {rec.get('risk_score', '?')}/10")
    print(f"  Confidence: {rec.get('confidence_pct', '?')}%")
    print(f"  Estimated LOC: {rec.get('estimated_loc', '?')}")
    print(f"  Estimated duration: {rec.get('estimated_duration', '?')}")
    print()

    if deck.get("counter_case"):
        print(f"── Counter-case ──────────────────────────────────────────")
        print(f"  {deck['counter_case']}")
        print()

    if deck.get("biggest_risk"):
        print(f"── Biggest Risk ─────────────────────────────────────────")
        print(f"  {deck['biggest_risk']}")
        print()

    if deck.get("stakes_line"):
        print(f"── Stakes Line ───────────────────────────────────────────")
        print(f"  {deck['stakes_line']}")
        print()

    if deck.get("cost"):
        cost = deck["cost"]
        print(f"── Cost Estimate ────────────────────────────────────────")
        print(f"  Tokens: {cost.get('estimated_tokens', '?')}")
        print(f"  Workers: {cost.get('estimated_worker_count', '?')}")
        print()

    findings = deck.get("findings", {})
    if findings:
        adv = findings.get("adversarial", [])
        sec = findings.get("security", [])
        qa = findings.get("qa", {})
        if adv:
            print(f"── Adversarial Critique Findings ({len(adv)}) ───")
            for f in adv[:5]:
                print(f"  [{f.get('severity', '?')}] {f.get('finding', '?')}")
        if sec:
            print(f"── Security Review Findings ({len(sec)}) ───")
            for f in sec[:5]:
                print(f"  [{f.get('severity', '?')}] {f.get('finding', '?')}")
        if qa:
            vp = qa.get("verification_plan", {})
            if vp:
                criteria = vp.get("acceptance_criteria", [])
                print(f"── Verification Plan ({len(criteria)} criteria) ───")
                for c in criteria[:5]:
                    print(f"  - {c}")
        print()

    print(f"══════════════════════════════════════════════════════════════")


async def cmd_pending() -> int:
    """List all bundles waiting for PM action."""
    resp = await _send_rpc(_get_socket_path(), "studio.pending", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    bundles = resp.get("result", {}).get("bundles", [])
    if not bundles:
        print("No bundles pending review.")
        return 0

    print(f"{'ID':<28} {'TIER':<22} {'STATE':<12} {'AGE':<8} {'STATUS':<14} IDEA")
    for b in bundles:
        status = b.get("status", "")
        print(f"{b['id']:<28} {b.get('tier', '?'):<22} {b.get('state', '?'):<12} "
              f"{b.get('age', '?'):<8} {status:<14} {b.get('idea', '')}")
    return 0


async def cmd_status() -> int:
    """Show orchestrator status."""
    resp = await _send_rpc(_get_socket_path(), "studio.status", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    if data.get("uptime") is not None:
        print(f"Orchestrator: running (uptime: {data['uptime']}s)")
    bundles = data.get("bundles", [])
    for b in bundles:
        print(f"{b['id']:<28} {b['state']:<14} {b.get('idea', '')}")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="studio", description="Studio kernel CLI")
    sub = parser.add_subparsers(dest="command")

    # submit
    p_submit = sub.add_parser("submit", help="Submit a bundle JSON file")
    p_submit.add_argument("path", help="Path to submission JSON file")

    # approve
    p_approve = sub.add_parser("approve", help="Approve a bundle")
    p_approve.add_argument("bundle_id", help="Bundle ID (ULID)")

    # reject
    p_reject = sub.add_parser("reject", help="Reject a bundle")
    p_reject.add_argument("bundle_id", help="Bundle ID (ULID)")
    p_reject.add_argument("--reason", "-r", default="", help="Rejection reason")

    # list
    p_list = sub.add_parser("list", help="List non-terminal bundles")
    p_list.add_argument("--state", "-s", help="Filter by state")
    p_list.add_argument("--json", "-j", action="store_true", help="Machine-readable output")

    # show
    p_show = sub.add_parser("show", help="Show bundle detail")
    p_show.add_argument("bundle_id", help="Bundle ID (ULID)")

    # show-worker
    p_sw = sub.add_parser("show-worker", help="Show worker detail")
    p_sw.add_argument("worker_id", help="Worker ID")

    # kill
    p_kill = sub.add_parser("kill", help="Kill a running bundle")
    p_kill.add_argument("bundle_id", help="Bundle ID (ULID)")

    # deck
    p_deck = sub.add_parser("deck", help="Print the review deck for a bundle")
    p_deck.add_argument("bundle_id", help="Bundle ID (ULID)")

    # pending
    sub.add_parser("pending", help="List bundles waiting for PM action")

    # status
    sub.add_parser("status", help="Show orchestrator status")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    loop = asyncio.new_event_loop()
    try:
        if args.command == "submit":
            exit_code = loop.run_until_complete(cmd_submit(args.path))
        elif args.command == "approve":
            exit_code = loop.run_until_complete(cmd_approve(args.bundle_id))
        elif args.command == "reject":
            exit_code = loop.run_until_complete(cmd_reject(args.bundle_id, args.reason))
        elif args.command == "list":
            exit_code = loop.run_until_complete(cmd_list(args.state, args.json))
        elif args.command == "show":
            exit_code = loop.run_until_complete(cmd_show(args.bundle_id))
        elif args.command == "show-worker":
            exit_code = loop.run_until_complete(cmd_show_worker(args.worker_id))
        elif args.command == "kill":
            exit_code = loop.run_until_complete(cmd_kill(args.bundle_id))
        elif args.command == "deck":
            exit_code = loop.run_until_complete(cmd_deck(args.bundle_id))
        elif args.command == "pending":
            exit_code = loop.run_until_complete(cmd_pending())
        elif args.command == "status":
            exit_code = loop.run_until_complete(cmd_status())
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            exit_code = 1
    finally:
        loop.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
