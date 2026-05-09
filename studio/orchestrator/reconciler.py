"""Crash recovery: kill-all policy for Phase 1.

6-step reconciliation protocol, idempotent.
Phase 1 notes:
- Step 1: Kill workers in 'running' or 'paused' (paused code path never exercised)
- Step 4: Approval replay is a no-op (zero rows in Phase 1)
- Step 5: Bundles in 'verifying' re-ticked
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .models import WorkerState, NodeState, BundleState

if TYPE_CHECKING:
    from .db import Database


class Reconciler:
    """Crash recovery reconciler. Idempotent — safe to run multiple times."""

    def __init__(
        self,
        db: "Database",
        sm: Any,  # BundleStateMachine
        executor: Any,  # DagExecutor
    ) -> None:
        self.db = db
        self.sm = sm
        self.executor = executor

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def reconcile(self) -> dict[str, int]:
        """Run the full 6-step reconciliation protocol.

        Returns counts of actions taken.
        """
        counts: dict[str, int] = {
            "workers_killed": 0,
            "nodes_failed": 0,
            "bundles_recovered": 0,
        }

        # Step 1: Scan for workers in 'running' or 'paused' — kill them
        orphan_workers = await self.db.fetch_all(
            "SELECT id, bundle_id, node_id FROM workers WHERE state IN (?, ?)",
            (WorkerState.RUNNING, WorkerState.PAUSED),
        )

        for w in orphan_workers:
            node_db_id = f"{w['bundle_id']}:{w['node_id']}"

            # Decrement ref_count on input artifacts before marking as failed
            nrow = await self.db.fetch_one(
                "SELECT spec_json FROM dag_nodes WHERE id = ?", (node_db_id,)
            )
            if nrow and nrow["spec_json"]:
                try:
                    from .executor import DagExecutor
                    spec = json.loads(nrow["spec_json"])
                    await self.executor._adjust_artifact_refs(spec, w["bundle_id"], w["node_id"], -1)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Mark worker as failed
            await self.db.execute(
                "UPDATE workers SET state = ?, exit_reason = ?, ended_at = ? WHERE id = ?",
                (WorkerState.FAILED, "orchestrator_crash", self.now(), w["id"]),
            )
            # Mark node as failed
            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, failure_reason = ?, ended_at = ? WHERE id = ?",
                (NodeState.FAILED, "worker_killed_on_crash", self.now(), node_db_id),
            )
            counts["workers_killed"] += 1
            counts["nodes_failed"] += 1

        await self.db.conn.commit()

        # Step 2: For each affected bundle, kill remaining workers
        affected_bundles: set[str] = set()
        for w in orphan_workers:
            affected_bundles.add(w["bundle_id"])

            # Skip downstream nodes
            await self._skip_downstream(w["bundle_id"], w["node_id"])

        # Step 3: Fail affected bundles
        for bundle_id in affected_bundles:
            try:
                await self.sm.transition_25_fail_execution(
                    bundle_id, "orchestrator crashed during execution"
                )
            except Exception:
                pass

        await self.db.conn.commit()

        # Step 4: Replay unread approval decisions — no-op in Phase 1
        # (approval_requests table unused in Phase 1)

        # Step 5: Re-tick bundles in 'verifying'
        verifying_bundles = await self.db.fetch_all(
            "SELECT id FROM bundles WHERE state = ?", (BundleState.VERIFYING,)
        )
        for b in verifying_bundles:
            try:
                await self.executor.start_bundle(b["id"])
                counts["bundles_recovered"] += 1
            except Exception:
                pass

        # Step 6: Record reconciliation event
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("reconciler_run", "system", "orchestrator",
             json.dumps(counts), self.now()),
        )
        await self.db.conn.commit()

        return counts

    async def _skip_downstream(self, bundle_id: str, failed_node_id: str) -> None:
        """Mark downstream nodes as skipped, BFS from failed node."""
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
