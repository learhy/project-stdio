"""CLI entry point: 8 commands for bundle lifecycle management.

Talks to the orchestrator over its Unix domain socket via JSON-RPC 2.0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

from .display import (
    format_bundle_show,
    format_bundle_list,
    format_worker_show,
    format_health,
    format_status,
    format_calibration,
)


async def _send_rpc(socket_path: str, method: str, params: dict[str, Any] | None = None) -> dict:
    """Send a JSON-RPC request over the Unix socket and return the result."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        return {
            "error": {
                "code": -1,
                "message": f"Orchestrator not running (no socket at {socket_path}). "
                           f"Start it first: STUDIO_TEST_MODE=1 uv run python -m studio.orchestrator.main &",
            }
        }

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
    return os.environ.get("STUDIO_SOCKET_PATH", "/tmp/studio.sock")


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


async def cmd_list(state: str | None = None, tier: str | None = None, json_output: bool = False) -> int:
    """List non-terminal bundles."""
    params: dict[str, Any] = {}
    if state:
        params["state"] = state
    if tier:
        params["tier"] = tier

    resp = await _send_rpc(_get_socket_path(), "studio.list", params)
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    bundles = resp.get("result", {}).get("bundles", [])
    if json_output:
        print(json.dumps(bundles, indent=2))
    else:
        print(format_bundle_list(bundles))
    return 0


async def cmd_show(bundle_id: str, verbose: bool = False, json_output: bool = False) -> int:
    """Show bundle detail."""
    resp = await _send_rpc(_get_socket_path(), "studio.show",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    result = resp.get("result", {})
    if json_output:
        print(json.dumps(result, indent=2))
        return 0

    bundle = result.get("bundle", {})
    proposal = result.get("proposal", {})
    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    audit_entries = result.get("audit_entries", [])
    artifacts = result.get("artifacts", [])

    print(format_bundle_show(bundle, proposal, nodes, edges, audit_entries, artifacts, verbose=verbose))
    return 0


async def cmd_show_worker(worker_id: str) -> int:
    """Show worker detail."""
    resp = await _send_rpc(_get_socket_path(), "studio.show_worker",
                           {"worker_id": worker_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    worker = data.get("worker", {})
    node = data.get("node")
    cap_checks = data.get("cap_checks", {})

    print(format_worker_show(worker, node, cap_checks))
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


async def cmd_recall(bundle_id: str) -> int:
    """Recall a COMPLETE bundle within the 48h window."""
    resp = await _send_rpc(_get_socket_path(), "studio.recall",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    result = resp.get("result", {})
    if result.get("eligible"):
        print(f"Recall eligible. Submit a rollback bundle with idea: "
              f"'Revert bundle {bundle_id}'")
    else:
        print(f"Not eligible: {result.get('reason', 'unknown')}")
    return 0


async def cmd_audit(bundle_id: str) -> int:
    """Audit capability grants and usage for a bundle (Bundle 3.4)."""
    resp = await _send_rpc(_get_socket_path(), "studio.audit",
                           {"bundle_id": bundle_id})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        return 1

    print(f"Bundle: {data.get('bundle_id', bundle_id)}")
    print(f"State: {data.get('state', 'unknown')}")
    print()

    granted = data.get("granted", {})
    if granted:
        print("Granted capabilities:")
        for category, items in granted.items():
            print(f"  {category}:")
            for item in items:
                print(f"    - {item}")
    else:
        print("No capability manifest found in bundle proposal.")

    print()
    used = data.get("used_grants", [])
    if used:
        print(f"Used grants ({len(used)}):")
        for g in used:
            print(f"  - {g}")

    unused = data.get("unused_grants", [])
    if unused:
        print(f"\nUnused grants ({len(unused)}):")
        for g in unused:
            print(f"  - {g}")

    over = data.get("over_granted", [])
    if over:
        print(f"\nOver-granted ({len(over)}):")
        for g in over:
            print(f"  - {g}")

    denied = data.get("denied_operations", [])
    if denied:
        print(f"\nDenied operations ({len(denied)}):")
        for d in denied:
            print(f"  - {d}")

    used_secrets = data.get("used_secrets", [])
    if used_secrets:
        print(f"\nSecrets accessed ({len(used_secrets)}):")
        for s in used_secrets:
            print(f"  - {s}")
    return 0


async def cmd_rotate_secret(name: str) -> int:
    """Rotate a secret (Bundle 3.4)."""
    resp = await _send_rpc(_get_socket_path(), "studio.rotate_secret",
                           {"name": name})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        return 1

    print(f"Secret '{data.get('secret', name)}' rotated.")
    affected = data.get("affected_workers", [])
    if affected:
        print(f"Workers that previously accessed old value: {', '.join(affected)}")
    else:
        print("No workers had previously accessed this secret.")
    return 0


async def cmd_health() -> int:
    """Show orchestrator health dashboard."""
    resp = await _send_rpc(_get_socket_path(), "studio.health", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    print(format_health(data))
    return 0


async def cmd_status() -> int:
    """Show orchestrator status."""
    resp = await _send_rpc(_get_socket_path(), "studio.status", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    print(format_status(
        uptime=data.get("uptime", 0),
        worker_count=data.get("worker_count", 0),
        queue_depth=data.get("queue_depth", 0),
        listeners=data.get("listeners"),
    ))
    return 0


async def cmd_fleet_status() -> int:
    """Show remote fleet host status (Bundle 4.2)."""
    resp = await _send_rpc(_get_socket_path(), "studio.fleet_status", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    hosts = resp.get("result", {}).get("hosts", [])
    if not hosts:
        print("No fleet hosts configured. Use 'studio fleet-add <name> <addr>' to add one.")
        return 0

    print(f"{'Name':<16} {'Address':<22} {'Status':<10} {'Workers':<10} {'Last Ping':<12}")
    print("-" * 72)
    for h in hosts:
        last_ping = ""
        if h.get("last_ping"):
            ago = int(time.time() - h["last_ping"])
            last_ping = f"{ago}s ago"
        print(f"{h['name']:<16} {h['addr']:<22} {h['status']:<10} "
              f"{h.get('active_workers', 0)}/{h['max_workers']:<6} {last_ping:<12}")
    return 0


async def cmd_fleet_add(name: str, addr: str, args) -> int:
    """Add a host to the fleet registry (Bundle 4.2)."""
    params = {
        "name": name,
        "addr": addr,
        "ssh_user": getattr(args, "ssh_user", "studio"),
        "ssh_key_path": getattr(args, "ssh_key", ""),
        "capabilities": getattr(args, "capabilities", []),
        "max_concurrent_workers": getattr(args, "max_workers", 4),
    }
    resp = await _send_rpc(_get_socket_path(), "studio.fleet_add", params)
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        result = resp.get("error", {})
        if "message" in result:
            print(f"Error: {result['message']}", file=sys.stderr)
        else:
            print(f"Error: {resp['error']}", file=sys.stderr)
        return 1

    print(f"Host '{name}' ({addr}) added to fleet.")
    return 0


async def cmd_fleet_remove(name: str) -> int:
    """Remove a host from the fleet registry (Bundle 4.2)."""
    resp = await _send_rpc(_get_socket_path(), "studio.fleet_remove", {"name": name})
    if "error" in resp:
        result = resp.get("error", {})
        if isinstance(result, dict) and "message" in result:
            print(f"Error: {result['message']}", file=sys.stderr)
        else:
            print(f"Error: {resp['error']}", file=sys.stderr)
        return 1

    print(f"Host '{name}' removed from fleet.")
    return 0


async def cmd_k8s_status() -> int:
    """Show active Kubernetes Jobs for workers (Bundle 4.3)."""
    resp = await _send_rpc(_get_socket_path(), "studio.k8s_status", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    data = resp.get("result", {})
    jobs = data.get("jobs", [])
    namespace = data.get("namespace", "studio-workers")

    if not jobs:
        print(f"No active studio worker Jobs in namespace '{namespace}'.")
        return 0

    print(f"Active studio worker Jobs in '{namespace}':")
    print(f"{'Job Name':<36} {'Bundle ID':<28} {'Active':<8} {'Succeeded':<11} {'Failed':<8} {'Age':<8}")
    print("-" * 100)
    for j in jobs:
        age = f"{j.get('age', 0)}s" if j.get('age', 0) < 3600 else f"{j.get('age', 0) // 60}m"
        print(f"{j['name']:<36} {j.get('bundle_id', ''):<28} "
              f"{j.get('active', 0):<8} {j.get('succeeded', 0):<11} "
              f"{j.get('failed', 0):<8} {age:<8}")
    return 0


async def cmd_calibration_report() -> int:
    """Print calibration report from memory/calibration/."""
    resp = await _send_rpc(_get_socket_path(), "studio.calibration_report", {})
    if "error" in resp:
        print(f"Error: {resp['error']['message']}", file=sys.stderr)
        return 1

    print(format_calibration(resp.get("result", {})))
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
    p_list.add_argument("--tier", "-t", help="Filter by tier")
    p_list.add_argument("--json", "-j", action="store_true", help="Machine-readable output")

    # show
    p_show = sub.add_parser("show", help="Show bundle detail")
    p_show.add_argument("bundle_id", help="Bundle ID (ULID)")
    p_show.add_argument("--verbose", "-v", action="store_true", help="Show full detail")
    p_show.add_argument("--json", "-j", action="store_true", help="Machine-readable output")

    # show-worker
    p_sw = sub.add_parser("show-worker", help="Show worker detail")
    p_sw.add_argument("worker_id", help="Worker ID")

    # kill
    p_kill = sub.add_parser("kill", help="Kill a running bundle")
    p_kill.add_argument("bundle_id", help="Bundle ID (ULID)")

    # status
    sub.add_parser("status", help="Show orchestrator status")

    # recall
    p_recall = sub.add_parser("recall", help="Recall a COMPLETE bundle (48h window)")
    p_recall.add_argument("bundle_id", help="Bundle ID (ULID)")

    # health
    sub.add_parser("health", help="Show orchestrator health dashboard")

    # audit (Bundle 3.4)
    p_audit = sub.add_parser("audit", help="Audit capability grants and usage for a bundle")
    p_audit.add_argument("bundle_id", help="Bundle ID (ULID)")

    # rotate-secret (Bundle 3.4)
    p_rotate = sub.add_parser("rotate-secret", help="Rotate a secret")
    p_rotate.add_argument("name", help="Secret name")

    # calibration-report
    sub.add_parser("calibration-report", help="Print estimated-vs-actual scoring outcomes")

    # fleet-status (Bundle 4.2)
    sub.add_parser("fleet-status", help="Show remote fleet host status")

    # k8s-status (Bundle 4.3)
    sub.add_parser("k8s-status", help="Show active Kubernetes Jobs for workers")

    # fleet-add (Bundle 4.2)
    p_fadd = sub.add_parser("fleet-add", help="Add a host to the remote fleet")
    p_fadd.add_argument("name", help="Host name")
    p_fadd.add_argument("addr", help="Host address (hostname or IP)")
    p_fadd.add_argument("--ssh-user", default="studio", help="SSH username (default: studio)")
    p_fadd.add_argument("--ssh-key", default="", help="Path to SSH private key")
    p_fadd.add_argument("--capabilities", nargs="*", default=[], help="Worker capabilities (e.g. python node)")
    p_fadd.add_argument("--max-workers", type=int, default=4, help="Max concurrent workers (default: 4)")

    # fleet-remove (Bundle 4.2)
    p_fremove = sub.add_parser("fleet-remove", help="Remove a host from the remote fleet")
    p_fremove.add_argument("name", help="Host name to remove")

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
            exit_code = loop.run_until_complete(cmd_list(args.state, args.tier, args.json))
        elif args.command == "show":
            exit_code = loop.run_until_complete(cmd_show(args.bundle_id, args.verbose, args.json))
        elif args.command == "show-worker":
            exit_code = loop.run_until_complete(cmd_show_worker(args.worker_id))
        elif args.command == "kill":
            exit_code = loop.run_until_complete(cmd_kill(args.bundle_id))
        elif args.command == "status":
            exit_code = loop.run_until_complete(cmd_status())
        elif args.command == "recall":
            exit_code = loop.run_until_complete(cmd_recall(args.bundle_id))
        elif args.command == "health":
            exit_code = loop.run_until_complete(cmd_health())
        elif args.command == "audit":
            exit_code = loop.run_until_complete(cmd_audit(args.bundle_id))
        elif args.command == "rotate-secret":
            exit_code = loop.run_until_complete(cmd_rotate_secret(args.name))
        elif args.command == "calibration-report":
            exit_code = loop.run_until_complete(cmd_calibration_report())
        elif args.command == "fleet-status":
            exit_code = loop.run_until_complete(cmd_fleet_status())
        elif args.command == "k8s-status":
            exit_code = loop.run_until_complete(cmd_k8s_status())
        elif args.command == "fleet-add":
            exit_code = loop.run_until_complete(cmd_fleet_add(args.name, args.addr, args))
        elif args.command == "fleet-remove":
            exit_code = loop.run_until_complete(cmd_fleet_remove(args.name))
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            exit_code = 1
    finally:
        loop.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
