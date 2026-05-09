"""DAG executor: drives node lifecycle for linear Phase 1 DAGs.

Node lifecycle: pending -> ready -> running -> completed|failed.
FIFO scheduling with global worker cap, heartbeat-driven liveness.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from .models import NodeState, WorkerState, BundleState, CapabilityManifest

if TYPE_CHECKING:
    from .db import Database


class LinearDagExecutor:
    """Drives node lifecycle for linear (single-chain) Phase 1 DAGs."""

    def __init__(
        self,
        db: "Database",
        sm: Any,  # BundleStateMachine
        runner: Any,  # LocalBwrapWorkerRunner
        rpc_handlers: Any,  # RpcHandlers
        conn_mgr: Any,  # ConnectionManager
        global_concurrency: int = 4,
        heartbeat_timeout_multiplier: float = 2.0,
    ) -> None:
        self.db = db
        self.sm = sm
        self.runner = runner
        self.rpc_handlers = rpc_handlers
        self.conn_mgr = conn_mgr
        self.global_concurrency = global_concurrency
        self.heartbeat_timeout_multiplier = heartbeat_timeout_multiplier

        # Active tracking
        self._running_workers: dict[str, asyncio.subprocess.Process] = {}
        self._active_bundles: set[str] = set()

        # Wire callbacks
        self.rpc_handlers.set_on_final_report(self._on_final_report)
        self.rpc_handlers.set_on_heartbeat(self._on_heartbeat)

    @staticmethod
    def now() -> int:
        return int(time.time())

    # ── Bundle lifecycle ──────────────────────────────────────────────────

    async def start_bundle(self, bundle_id: str) -> None:
        """Called after transition 6 (APPROVED -> IN_PROGRESS).

        Reads DAG structure, marks entry nodes ready, dispatches.
        """
        self._active_bundles.add(bundle_id)

        # Mark entry nodes as ready
        nodes = await self.db.fetch_all(
            "SELECT id, node_id FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
        )

        # Find entry nodes: those with no incoming edges
        for node in nodes:
            edge = await self.db.fetch_one(
                "SELECT id FROM dag_edges WHERE bundle_id = ? AND to_node_id = ?",
                (bundle_id, node["node_id"]),
            )
            if edge is None:
                now = self.now()
                await self.db.execute(
                    "UPDATE dag_nodes SET state = ?, ready_at = ? WHERE id = ?",
                    (NodeState.READY, now, node["id"]),
                )
        await self.db.conn.commit()

        # Try to dispatch
        await self._dispatch_ready(bundle_id)

    async def stop_bundle(self, bundle_id: str) -> None:
        """Kill all running workers for a bundle and clean up tracking."""
        self._active_bundles.discard(bundle_id)
        # Kill any still-running workers for this bundle
        workers = await self.db.fetch_all(
            "SELECT id FROM workers WHERE bundle_id = ? AND state = ?",
            (bundle_id, WorkerState.RUNNING),
        )
        for w in workers:
            proc = self._running_workers.pop(w["id"], None)
            if proc and proc.returncode is None:
                await self.runner.kill_worker(proc)

    # ── Node lifecycle callbacks ──────────────────────────────────────────

    async def _on_final_report(self, bundle_id: str, node_id: str, worker_id: str, outcome: dict) -> None:
        """Callback from RpcHandlers when a worker sends final_report."""
        proc = self._running_workers.pop(worker_id, None)
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        node_state = outcome.get("node_state", NodeState.FAILED)

        if node_state == NodeState.COMPLETED:
            await self._advance_chain(bundle_id, node_id)
        elif node_state == NodeState.FAILED:
            await self._fail_bundle(bundle_id, node_id, worker_id, "worker reported failure")
        else:
            await self._fail_bundle(bundle_id, node_id, worker_id, f"unknown outcome: {node_state}")

        # Check if bundle is done (all exit nodes terminal) or try dispatch
        if bundle_id in self._active_bundles:
            await self._dispatch_ready(bundle_id)

    async def _on_heartbeat(self, worker_id: str, phase: str) -> None:
        """Callback from RpcHandlers on every heartbeat. Updates liveness tracking."""
        pass  # Heartbeat timestamps are handled by RpcHandlers directly

    # ── Chain advancement ─────────────────────────────────────────────────

    async def _advance_chain(self, bundle_id: str, completed_node_id: str) -> None:
        """When a node completes, mark its successor as ready."""
        # Find outgoing edges from this node
        edges = await self.db.fetch_all(
            "SELECT to_node_id FROM dag_edges WHERE bundle_id = ? AND from_node_id = ?",
            (bundle_id, completed_node_id),
        )

        now = self.now()
        for edge in edges:
            successor_node_id = f"{bundle_id}:{edge['to_node_id']}"
            # Mark edge as fired
            await self.db.execute(
                "UPDATE dag_edges SET fired = 1, fired_at = ? WHERE bundle_id = ? AND from_node_id = ? AND to_node_id = ?",
                (now, bundle_id, completed_node_id, edge["to_node_id"]),
            )
            # Mark successor as ready
            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, ready_at = ? WHERE id = ?",
                (NodeState.READY, now, successor_node_id),
            )

        await self.db.conn.commit()

        # Check if all exit nodes are terminal -> transition to VERIFYING
        await self._check_bundle_completion(bundle_id)

    async def _check_bundle_completion(self, bundle_id: str) -> None:
        """If all exit nodes are terminal, trigger transition 9 (IN_PROGRESS -> VERIFYING)."""
        status = await self._compute_bundle_status(bundle_id)
        if status == "all_terminal":
            # Check if any node failed
            failed_nodes = await self.db.fetch_all(
                "SELECT id FROM dag_nodes WHERE bundle_id = ? AND state = ?",
                (bundle_id, NodeState.FAILED),
            )
            if failed_nodes:
                # Some nodes failed — fail the bundle
                await self._fail_bundle(
                    bundle_id,
                    failed_nodes[0]["id"].split(":", 1)[1] if ":" in failed_nodes[0]["id"] else "",
                    None,
                    "node execution failed",
                )
            else:
                # All exit nodes completed — transition to VERIFYING
                try:
                    await self.sm.transition_9_to_verifying(bundle_id)
                    # Auto-transition to COMPLETE in Phase 1 (no actual verification)
                    await self.sm.transition_17_complete(bundle_id)
                    self._active_bundles.discard(bundle_id)
                except Exception:
                    pass

    async def _fail_bundle(
        self,
        bundle_id: str,
        node_id: str,
        worker_id: str | None,
        reason: str,
    ) -> None:
        """Fail the entire bundle. Skips downstream nodes."""
        # Skip all downstream nodes
        await self._skip_downstream(bundle_id, node_id)

        # Kill remaining workers
        await self.stop_bundle(bundle_id)

        # Record failure in audit log
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bundle_failed_during_execution", "bundle", bundle_id,
             json.dumps({"reason": reason, "failed_node": node_id, "worker_id": worker_id}),
             self.now()),
        )
        await self.db.conn.commit()

        # Transition bundle to FAILED
        try:
            await self.sm.transition_25_fail_execution(bundle_id, reason)
        except Exception:
            pass

    async def _skip_downstream(self, bundle_id: str, failed_node_id: str) -> None:
        """Mark all downstream nodes of a failed node as skipped."""
        # BFS from failed node along outgoing edges
        visited: set[str] = set()
        queue = [failed_node_id]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            edges = await self.db.fetch_all(
                "SELECT to_node_id FROM dag_edges WHERE bundle_id = ? AND from_node_id = ? AND fired = 0",
                (bundle_id, current),
            )
            for edge in edges:
                successor_id = f"{bundle_id}:{edge['to_node_id']}"
                if successor_id not in visited:
                    await self.db.execute(
                        "UPDATE dag_nodes SET state = ?, failure_reason = ? WHERE id = ?",
                        (NodeState.SKIPPED, f"upstream node {failed_node_id} failed", successor_id),
                    )
                    queue.append(edge["to_node_id"])

        await self.db.conn.commit()

    # ── Dispatch ──────────────────────────────────────────────────────────

    async def _dispatch_ready(self, bundle_id: str) -> int:
        """Dispatch ready nodes up to the global concurrency cap. Returns number dispatched."""
        # Count currently running workers
        running_count = await self._count_running_workers()
        available = self.global_concurrency - running_count
        if available <= 0:
            return 0

        # Get ready nodes for this bundle, FIFO by ready_at
        ready_nodes = await self.db.fetch_all(
            "SELECT id, node_id, spec_json FROM dag_nodes WHERE bundle_id = ? AND state = ? ORDER BY ready_at ASC LIMIT ?",
            (bundle_id, NodeState.READY, available),
        )

        dispatched = 0
        for node in ready_nodes:
            worker_id = f"w_{node['id'].replace(':', '_')}"

            # Get the bundle's manifest for capability grants
            bundle_row = await self.db.fetch_one(
                "SELECT proposal_json FROM bundles WHERE id = ?", (bundle_id,)
            )
            if bundle_row is None:
                continue

            # Parse manifest — for Phase 1, use a default manifest
            manifest = CapabilityManifest(
                schema_version="1.0",
                subject={"kind": "bundle", "id": bundle_id},
                grants={"filesystem": {"reads": [], "writes": []}, "network": {"egress": []},
                        "process": {"exec": []}, "rpc": {"methods": ["worker.*", "cap.*"]}, "resources": {}},
                metadata={"rationale": "auto-generated"},
            )

            try:
                spec = json.loads(node["spec_json"])
            except (json.JSONDecodeError, TypeError):
                spec = {}

            # Spawn the worker first (inserts worker row needed by FK)
            result = await self.runner.spawn_worker(
                worker_id=worker_id,
                bundle_id=bundle_id,
                node_id=node["node_id"],
                manifest=manifest,
                worktree_path="/tmp/worktree-placeholder",
                task_spec=spec.get("spec", spec),
            )

            # Now safe to reference worker_id in dag_nodes (FK satisfied)
            now = self.now()
            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, worker_id = ?, started_at = ? WHERE id = ?",
                (NodeState.RUNNING, worker_id, now, node["id"]),
            )
            await self.db.conn.commit()

            if result.process is not None:
                self._running_workers[worker_id] = result.process

            dispatched += 1

        return dispatched

    async def _count_running_workers(self) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM workers WHERE state = ?", (WorkerState.RUNNING,)
        )
        return row["cnt"] if row else 0

    async def _compute_bundle_status(self, bundle_id: str) -> str:
        """Check if all exit nodes for a bundle are in terminal states."""
        exit_nodes = await self.db.fetch_all(
            "SELECT dn.state FROM dag_nodes dn "
            "JOIN dag_edges de ON dn.node_id = de.to_node_id AND dn.bundle_id = de.bundle_id "
            "WHERE dn.bundle_id = ? AND de.to_node_id NOT IN "
            "(SELECT from_node_id FROM dag_edges WHERE bundle_id = ?)",
            (bundle_id, bundle_id),
        )

        if not exit_nodes:
            # No exit nodes found via edges — check if any nodes exist at all
            all_nodes = await self.db.fetch_all(
                "SELECT state FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
            )
            if not all_nodes:
                return "empty"
            exit_nodes = all_nodes

        terminal_states = {NodeState.COMPLETED, NodeState.FAILED, NodeState.SKIPPED, NodeState.CANCELLED}
        if all(n["state"] in terminal_states for n in exit_nodes):
            return "all_terminal"
        return "in_progress"

    # ── Heartbeat monitoring ──────────────────────────────────────────────

    async def check_heartbeat_timeouts(self) -> list[str]:
        """Check for wedged workers. Returns list of timed-out worker IDs."""
        now = self.now()
        timed_out: list[str] = []

        workers = await self.db.fetch_all(
            "SELECT id, bundle_id, node_id, last_heartbeat, manifest_json FROM workers WHERE state = ?",
            (WorkerState.RUNNING,),
        )

        for w in workers:
            last_hb = w["last_heartbeat"]
            if last_hb is None:
                continue

            # Get wall_time_limit from manifest
            timeout = 3600  # default 1 hour
            if w["manifest_json"]:
                try:
                    mf = json.loads(w["manifest_json"])
                    timeout = (
                        mf.get("grants", {})
                        .get("resources", {})
                        .get("wall_time_limit", 3600)
                    )
                except Exception:
                    pass

            threshold = timeout * self.heartbeat_timeout_multiplier
            if now - last_hb > threshold:
                timed_out.append(w["id"])
                node_db_id = f"{w['bundle_id']}:{w['node_id']}"

                # Kill the worker process
                proc = self._running_workers.pop(w["id"], None)
                if proc and proc.returncode is None:
                    await self.runner.kill_worker(proc)

                # Mark node and worker as failed
                await self.db.execute(
                    "UPDATE dag_nodes SET state = ?, failure_reason = ? WHERE id = ?",
                    (NodeState.FAILED, "heartbeat_timeout", node_db_id),
                )
                await self.db.execute(
                    "UPDATE workers SET state = ?, exit_reason = ? WHERE id = ?",
                    (WorkerState.FAILED, "heartbeat_timeout", w["id"]),
                )
                await self.db.conn.commit()

                # Fail the bundle
                await self._fail_bundle(
                    w["bundle_id"], w["node_id"], w["id"], "heartbeat_timeout"
                )

        return timed_out

    # ── Node state query ──────────────────────────────────────────────────

    async def get_node_state(self, bundle_id: str, node_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT state FROM dag_nodes WHERE id = ?", (f"{bundle_id}:{node_id}",)
        )
        return row["state"] if row else None
