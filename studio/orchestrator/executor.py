"""DAG executor: drives full node lifecycle for Phase 2 DAGs.

Node lifecycle: pending -> ready -> running -> completed|failed|skipped|cancelled.
Supports worker nodes, gate nodes (artifact_property, rpc_query, human_approval),
aggregator nodes (all, any, quorum, first_success), full edge condition semantics,
and dynamic DAG expansion via cap.request.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from .models import (
    NodeState,
    WorkerState,
    BundleState,
    CapabilityManifest,
    GatePredicateKind,
    AggregatorJoinMode,
    AggregatorOutputStrategy,
    CapRequestParams,
)
from .expression import evaluate as eval_property
from .reducers import get_reducer

if TYPE_CHECKING:
    from .db import Database


class DagExecutor:
    """Drives full node lifecycle for Phase 2 DAGs with gates and aggregators."""

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
        self._artifact_events: asyncio.Queue[Any] = asyncio.Queue()
        self._artifact_store: Any = None

        # Wire callbacks
        self.rpc_handlers.set_on_final_report(self._on_final_report)
        self.rpc_handlers.set_on_heartbeat(self._on_heartbeat)
        self.rpc_handlers.set_on_cap_request(self._on_cap_request)

    def set_artifact_store(self, store: Any) -> None:
        self._artifact_store = store

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def process_artifact_events(self) -> None:
        """Drain the artifact event queue and re-evaluate pending successors.

        Called on each scheduler tick. When a new_artifact event arrives, queries
        artifact_refs for matching descriptors and tries to unblock pending nodes.
        """
        while not self._artifact_events.empty():
            try:
                event = self._artifact_events.get_nowait()
            except asyncio.QueueEmpty:
                break

            if event.event_type == "new_artifact":
                # Find all artifact_refs rows matching this artifact's descriptor
                refs = await self.db.fetch_all(
                    "SELECT bundle_id, producer_node_id FROM artifact_refs WHERE descriptor_json = ?",
                    (event.descriptor_json,),
                )
                for ref in refs:
                    # Find successor nodes of the producer
                    edges = await self.db.fetch_all(
                        "SELECT to_node_id FROM dag_edges "
                        "WHERE bundle_id = ? AND from_node_id = ? AND fired = 0",
                        (ref["bundle_id"], ref["producer_node_id"]),
                    )
                    for edge in edges:
                        await self._try_make_ready(ref["bundle_id"], edge["to_node_id"])

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
            await self._process_node_completion(bundle_id, node_id, NodeState.COMPLETED)
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

    async def _on_cap_request(self, bundle_id: str, node_id: str, params: dict) -> dict[str, Any]:
        """Callback from RpcHandlers for cap.request — delegate to handle_expansion_request."""
        try:
            cap_params = CapRequestParams.model_validate(params)
        except Exception as exc:
            return {"decision": "denied", "decision_id": None,
                    "reason": f"Invalid cap.request params: {exc}"}
        return await self.handle_expansion_request(bundle_id, node_id, cap_params)

    # ── Node completion and edge evaluation ────────────────────────────────

    async def _process_node_completion(
        self, bundle_id: str, node_id: str, final_state: str, spec_json: str | None = None
    ) -> None:
        """Evaluate outgoing edges from a completed/failed/skipped node.

        For each outgoing edge, evaluate its condition against the source's final state.
        Fire matching edges. Then check if any targets have become ready.
        """
        # Decrement ref_count on this node's input artifacts
        if spec_json:
            try:
                self_spec = json.loads(spec_json)
                await self._adjust_artifact_refs(self_spec, bundle_id, node_id, -1)
            except (json.JSONDecodeError, TypeError):
                pass

        edges = await self.db.fetch_all(
            "SELECT id, to_node_id, condition_kind, condition_expr "
            "FROM dag_edges WHERE bundle_id = ? AND from_node_id = ? AND fired = 0",
            (bundle_id, node_id),
        )

        now = self.now()
        for edge in edges:
            should_fire = await self._edge_condition_matches(
                edge["condition_kind"],
                edge["condition_expr"],
                final_state,
                bundle_id,
                node_id,
            )
            if should_fire:
                await self.db.execute(
                    "UPDATE dag_edges SET fired = 1, fired_at = ? WHERE id = ?",
                    (now, edge["id"]),
                )
                await self._try_make_ready(bundle_id, edge["to_node_id"])

        await self.db.conn.commit()
        await self._check_bundle_completion(bundle_id)

    async def _edge_condition_matches(
        self,
        condition_kind: str,
        condition_expr: str | None,
        source_state: str,
        bundle_id: str,
        node_id: str,
    ) -> bool:
        """Determine whether an edge condition matches the source node's final state."""
        if condition_kind == "always":
            return source_state in (
                NodeState.COMPLETED, NodeState.FAILED, NodeState.SKIPPED
            )
        elif condition_kind == "on_success":
            return source_state == NodeState.COMPLETED
        elif condition_kind == "on_failure":
            return source_state == NodeState.FAILED
        elif condition_kind == "on_property":
            if source_state != NodeState.COMPLETED:
                return False
            if not condition_expr:
                return False
            return await self._eval_on_property(bundle_id, node_id, condition_expr)
        return False

    async def _eval_on_property(self, bundle_id: str, node_id: str, expression: str) -> bool:
        """Evaluate an on_property expression against a node's output context."""
        node_db_id = f"{bundle_id}:{node_id}"
        row = await self.db.fetch_one(
            "SELECT output_json FROM dag_nodes WHERE id = ?", (node_db_id,)
        )
        if not row or not row["output_json"]:
            return False
        try:
            ctx = json.loads(row["output_json"])
        except (json.JSONDecodeError, TypeError):
            return False
        return eval_property(expression, ctx)

    async def _try_make_ready(self, bundle_id: str, target_node_id: str) -> None:
        """Check if a target node should become ready based on fired incoming edges."""
        target_db_id = f"{bundle_id}:{target_node_id}"

        target = await self.db.fetch_one(
            "SELECT kind, aggregator_config_json, state, spec_json FROM dag_nodes WHERE id = ?",
            (target_db_id,),
        )
        if target is None:
            return

        # Skip if already past pending
        if target["state"] != NodeState.PENDING:
            return

        # Count fired incoming edges
        fired = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM dag_edges "
            "WHERE bundle_id = ? AND to_node_id = ? AND fired = 1",
            (bundle_id, target_node_id),
        )
        total = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM dag_edges "
            "WHERE bundle_id = ? AND to_node_id = ?",
            (bundle_id, target_node_id),
        )
        fired_count = fired["cnt"] if fired else 0
        total_count = total["cnt"] if total else 0

        if target["kind"] == "aggregator":
            config = {}
            if target["aggregator_config_json"]:
                try:
                    config = json.loads(target["aggregator_config_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            join = config.get("join", AggregatorJoinMode.ALL)

            if join == AggregatorJoinMode.FIRST_SUCCESS:
                # Only count fired edges from successfully completed predecessors
                success_fired = await self.db.fetch_one(
                    "SELECT COUNT(*) as cnt FROM dag_edges de "
                    "JOIN dag_nodes dn ON dn.bundle_id = de.bundle_id AND dn.node_id = de.from_node_id "
                    "WHERE de.bundle_id = ? AND de.to_node_id = ? AND de.fired = 1 "
                    "AND dn.state = ?",
                    (bundle_id, target_node_id, NodeState.COMPLETED),
                )
                success_count = success_fired["cnt"] if success_fired else 0

                if success_count >= 1:
                    should_ready = True
                elif fired_count >= total_count:
                    # All predecessors fired (terminal), none succeeded → fail
                    await self.db.execute(
                        "UPDATE dag_nodes SET state = ?, failure_reason = ?, ended_at = ? WHERE id = ?",
                        (NodeState.FAILED,
                         "all predecessors failed, none succeeded for first_success",
                         self.now(), target_db_id),
                    )
                    await self.db.conn.commit()
                    await self._process_node_completion(bundle_id, target_node_id, NodeState.FAILED)
                    return
                else:
                    should_ready = False
            else:
                should_ready = self._check_aggregator_ready(target, fired_count, total_count)
        else:
            # Non-aggregator: all incoming edges must fire
            should_ready = fired_count >= total_count

        if should_ready:
            # Check artifact input dependencies before marking ready
            try:
                target_spec = target["spec_json"]
            except (KeyError, TypeError):
                target_spec = None
            if not await self._artifact_inputs_available(bundle_id, target_node_id, target_spec):
                return
                return

            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, ready_at = ? WHERE id = ?",
                (NodeState.READY, self.now(), target_db_id),
            )

    async def _artifact_inputs_available(self, bundle_id: str, node_id: str, spec_json: str | None) -> bool:
        """Check whether all artifact inputs declared in the node spec exist.

        Returns True if the node has no artifact inputs or all are satisfied.
        Returns False if any required artifact has not been published yet.
        """
        if not spec_json:
            return True

        try:
            spec = json.loads(spec_json)
        except (json.JSONDecodeError, TypeError):
            return True

        inputs = spec.get("inputs", {})
        artifacts = inputs.get("artifacts", [])
        if not artifacts:
            return True

        for art in artifacts:
            descriptor = art.get("ref", art)
            if not isinstance(descriptor, dict):
                return False

            ns = descriptor.get("namespace", "bundle")
            name = descriptor.get("name", "")
            version = descriptor.get("version", "")

            # Check artifact_refs first
            descriptor_json = json.dumps(descriptor)
            ref_row = await self.db.fetch_one(
                "SELECT 1 FROM artifact_refs WHERE bundle_id = ? AND descriptor_json = ?",
                (bundle_id, descriptor_json),
            )
            if ref_row is not None:
                continue  # This input is satisfied

            # Fallback: check artifact_metadata (for externally injected bundle inputs)
            # Only for entry nodes or global artifacts
            if ns == "global":
                meta_row = await self.db.fetch_one(
                    "SELECT 1 FROM artifact_metadata WHERE namespace=? AND name=? AND version=? AND gc_d_at IS NULL",
                    (ns, name, version),
                )
                if meta_row is not None:
                    continue

            # Not found — node stays pending
            return False

        return True

    async def _adjust_artifact_refs(self, spec: dict, bundle_id: str, node_id: str, delta: int) -> None:
        """Adjust ref_count for declared input artifacts by delta (+1 or -1)."""
        inputs = spec.get("inputs", {})
        artifacts = inputs.get("artifacts", [])
        if not artifacts:
            return

        for art in artifacts:
            descriptor = art.get("ref", art)
            if not isinstance(descriptor, dict):
                continue
            ns = descriptor.get("namespace", "bundle")
            name = descriptor.get("name", "")
            version = descriptor.get("version", "")

            await self.db.execute(
                "UPDATE artifact_metadata SET ref_count = MAX(0, ref_count + ?) "
                "WHERE namespace = ? AND name = ? AND version = ?",
                (delta, ns, name, version),
            )

    async def _check_bundle_completion(self, bundle_id: str) -> None:
        """If all exit nodes are terminal, trigger transition 9 (IN_PROGRESS -> VERIFYING)."""
        status = await self._compute_bundle_status(bundle_id)
        if status == "all_terminal":
            failed_nodes = await self.db.fetch_all(
                "SELECT id FROM dag_nodes WHERE bundle_id = ? AND state = ?",
                (bundle_id, NodeState.FAILED),
            )
            if failed_nodes:
                await self._fail_bundle(
                    bundle_id,
                    failed_nodes[0]["id"].split(":", 1)[1] if ":" in failed_nodes[0]["id"] else "",
                    None,
                    "node execution failed",
                )
            else:
                try:
                    await self.sm.transition_9_to_verifying(bundle_id)
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
        running_count = await self._count_running_workers()
        available = self.global_concurrency - running_count
        if available <= 0:
            return 0

        ready_nodes = await self.db.fetch_all(
            "SELECT id, node_id, kind, spec_json, gate_config_json, aggregator_config_json "
            "FROM dag_nodes WHERE bundle_id = ? AND state = ? ORDER BY ready_at ASC LIMIT ?",
            (bundle_id, NodeState.READY, available),
        )

        dispatched = 0
        for node in ready_nodes:
            node_kind = node["kind"]

            if node_kind == "gate":
                await self._dispatch_gate(bundle_id, node)
            elif node_kind == "aggregator":
                await self._dispatch_aggregator(bundle_id, node)
            else:
                await self._dispatch_worker(bundle_id, node)

            dispatched += 1

        return dispatched

    async def _dispatch_worker(self, bundle_id: str, node: Any) -> None:
        """Spawn a worker subprocess for a ready worker node."""
        worker_id = f"w_{node['id'].replace(':', '_')}"

        manifest = CapabilityManifest(
            schema_version="1.0",
            subject={"kind": "bundle", "id": bundle_id},
            grants={
                "filesystem": {"reads": [], "writes": []},
                "network": {"egress": []},
                "process": {"exec": []},
                "rpc": {"methods": ["worker.*", "cap.*"]},
                "resources": {},
            },
            metadata={"rationale": "auto-generated"},
        )

        try:
            spec = json.loads(node["spec_json"])
        except (json.JSONDecodeError, TypeError):
            spec = {}

        result = await self.runner.spawn_worker(
            worker_id=worker_id,
            bundle_id=bundle_id,
            node_id=node["node_id"],
            manifest=manifest,
            worktree_path="/tmp/worktree-placeholder",
            task_spec=spec.get("spec", spec),
        )

        now = self.now()
        await self.db.execute(
            "UPDATE dag_nodes SET state = ?, worker_id = ?, started_at = ? WHERE id = ?",
            (NodeState.RUNNING, worker_id, now, node["id"]),
        )
        # Increment ref_count on declared input artifacts
        await self._adjust_artifact_refs(spec, bundle_id, node["node_id"], +1)
        await self.db.conn.commit()

        if result.process is not None:
            self._running_workers[worker_id] = result.process

    # ── Gate dispatch ──────────────────────────────────────────────────────

    async def _dispatch_gate(self, bundle_id: str, node: Any) -> None:
        """Evaluate a gate node's predicate and transition the node accordingly."""
        gate_config = {}
        if node["gate_config_json"]:
            try:
                gate_config = json.loads(node["gate_config_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        predicate = gate_config.get("predicate", GatePredicateKind.HUMAN_APPROVAL)
        now = self.now()

        if predicate == GatePredicateKind.HUMAN_APPROVAL:
            # Create approval request and leave node in running state (waiting)
            request_id = f"ar_{node['id'].replace(':', '_')}"
            await self.db.execute(
                "INSERT INTO approval_requests (id, bundle_id, kind, subject_id, "
                "context_json, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    bundle_id,
                    "gate_human_approval",
                    node["node_id"],
                    json.dumps({"prompt": gate_config.get("human_prompt", ""),
                                "node_id": node["node_id"]}),
                    "pending",
                    now,
                ),
            )
            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, started_at = ? WHERE id = ?",
                (NodeState.RUNNING, now, node["id"]),
            )
            await self.db.conn.commit()
            return

        elif predicate == GatePredicateKind.ARTIFACT_PROPERTY:
            # Evaluate property expression against an artifact
            expr = gate_config.get("property_expression", "")
            artifact_descriptor = gate_config.get("artifact_descriptor", "")
            passed = self._eval_artifact_property(bundle_id, artifact_descriptor, expr)
            await self._complete_gate(bundle_id, node, passed, now)

        elif predicate == GatePredicateKind.RPC_QUERY:
            # RPC query gates are deferred in Phase 2 — auto-pass with warning
            await self._complete_gate(bundle_id, node, True, now)

    def _eval_artifact_property(self, bundle_id: str, descriptor: str, expression: str) -> bool:
        """Check if an artifact exists and its properties satisfy the expression."""
        if not descriptor or not expression:
            return False
        try:
            desc = json.loads(descriptor)
        except (json.JSONDecodeError, TypeError):
            desc = {"name": descriptor}

        import asyncio as _asyncio
        async def _fetch():
            conditions = []
            params = []
            if isinstance(desc, dict):
                for k, v in desc.items():
                    conditions.append(f"descriptor_json LIKE ?")
                    params.append(f'%"{k}":"{v}"%')
            else:
                conditions.append("descriptor_json LIKE ?")
                params.append(f"%{desc}%")

            where = " AND ".join(conditions) if conditions else "1=1"
            row = await self.db.fetch_one(
                f"SELECT descriptor_json FROM artifact_metadata "
                f"WHERE bundle_id = ? AND {where} ORDER BY created_at DESC LIMIT 1",
                (bundle_id, *params),
            )
            if not row:
                return False
            try:
                ctx = json.loads(row["descriptor_json"])
            except (json.JSONDecodeError, TypeError):
                return False
            return eval_property(expression, ctx)

        try:
            loop = _asyncio.get_event_loop()
            return loop.run_until_complete(_fetch())
        except RuntimeError:
            return False

    async def _complete_gate(self, bundle_id: str, node: Any, passed: bool, now: int) -> None:
        """Transition a gate node to completed or failed based on predicate result."""
        final_state = NodeState.COMPLETED if passed else NodeState.FAILED
        await self.db.execute(
            "UPDATE dag_nodes SET state = ?, ended_at = ? WHERE id = ?",
            (final_state, now, node["id"]),
        )
        await self.db.conn.commit()
        await self._process_node_completion(bundle_id, node["node_id"], final_state)

    # ── Aggregator dispatch ────────────────────────────────────────────────

    def _check_aggregator_ready(
        self, target: Any, fired_count: int, total_count: int
    ) -> bool:
        """Check if an aggregator is ready based on join mode and fired incoming edges."""
        if fired_count == 0:
            return False

        config = {}
        if target["aggregator_config_json"]:
            try:
                config = json.loads(target["aggregator_config_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        join = config.get("join", AggregatorJoinMode.ALL)

        if join == AggregatorJoinMode.ALL:
            return fired_count >= total_count
        elif join == AggregatorJoinMode.ANY:
            return fired_count >= 1
        elif join == AggregatorJoinMode.FIRST_SUCCESS:
            return fired_count >= 1
        elif join == AggregatorJoinMode.QUORUM:
            quorum = config.get("quorum_count")
            if quorum is not None:
                return fired_count >= quorum
            # Default: simple majority
            return fired_count > total_count / 2

        return fired_count >= total_count

    async def _dispatch_aggregator(self, bundle_id: str, node: Any) -> None:
        """Evaluate an aggregator node: collect predecessor outputs, apply strategy, complete."""
        config = {}
        if node["aggregator_config_json"]:
            try:
                config = json.loads(node["aggregator_config_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        join = config.get("join", AggregatorJoinMode.ALL)
        output_strategy = config.get("output_strategy", AggregatorOutputStrategy.COLLECT)
        reducer_name = config.get("reducer")
        cancel_remaining = config.get("cancel_remaining_on_quorum", True)
        now = self.now()

        # Collect predecessor outputs
        incoming = await self.db.fetch_all(
            "SELECT from_node_id FROM dag_edges WHERE bundle_id = ? AND to_node_id = ? AND fired = 1",
            (bundle_id, node["node_id"]),
        )

        predecessor_outputs: list[dict[str, Any]] = []
        for inc in incoming:
            pred_db_id = f"{bundle_id}:{inc['from_node_id']}"
            if join == AggregatorJoinMode.FIRST_SUCCESS:
                pred_state = await self.db.fetch_one(
                    "SELECT state, output_json FROM dag_nodes WHERE id = ?", (pred_db_id,)
                )
                if not pred_state or pred_state["state"] != NodeState.COMPLETED:
                    continue
                output_json = pred_state["output_json"]
            else:
                pred = await self.db.fetch_one(
                    "SELECT output_json FROM dag_nodes WHERE id = ?", (pred_db_id,)
                )
                output_json = pred["output_json"] if pred else None

            if output_json:
                try:
                    predecessor_outputs.append(json.loads(output_json))
                except (json.JSONDecodeError, TypeError):
                    predecessor_outputs.append({})

        # Apply output strategy
        output = self._apply_aggregator_output(predecessor_outputs, output_strategy, reducer_name, config)

        await self.db.execute(
            "UPDATE dag_nodes SET state = ?, ended_at = ?, output_json = ? WHERE id = ?",
            (NodeState.COMPLETED, now, json.dumps(output or {}), node["id"]),
        )

        # Cancel remaining siblings for first_success and quorum
        if cancel_remaining and join in (AggregatorJoinMode.FIRST_SUCCESS, AggregatorJoinMode.QUORUM):
            await self._cancel_aggregator_siblings(bundle_id, node, incoming)

        await self.db.conn.commit()
        await self._process_node_completion(bundle_id, node["node_id"], NodeState.COMPLETED)

    def _apply_aggregator_output(
        self,
        predecessor_outputs: list[dict[str, Any]],
        output_strategy: str,
        reducer_name: str | None,
        config: dict[str, Any],
    ) -> Any:
        """Apply the aggregator's output strategy to predecessor outputs."""
        if output_strategy == AggregatorOutputStrategy.FIRST:
            return predecessor_outputs[0] if predecessor_outputs else None
        elif output_strategy == AggregatorOutputStrategy.REDUCE:
            if reducer_name:
                reducer = get_reducer(reducer_name)
                if reducer:
                    return reducer(predecessor_outputs, config.get("reducer_config", {}))
            # Fallback: collect
            return predecessor_outputs
        else:  # collect
            return predecessor_outputs

    async def _cancel_aggregator_siblings(
        self, bundle_id: str, node: Any, fired_incoming: Any
    ) -> None:
        """Cancel still-running sibling predecessors of a satisfied aggregator."""
        fired_ids = {inc["from_node_id"] for inc in fired_incoming}

        all_incoming = await self.db.fetch_all(
            "SELECT from_node_id FROM dag_edges WHERE bundle_id = ? AND to_node_id = ?",
            (bundle_id, node["node_id"]),
        )

        for inc in all_incoming:
            if inc["from_node_id"] in fired_ids:
                continue
            pred_db_id = f"{bundle_id}:{inc['from_node_id']}"
            pred = await self.db.fetch_one(
                "SELECT state, worker_id FROM dag_nodes WHERE id = ?", (pred_db_id,)
            )
            if pred and pred["state"] in (NodeState.READY, NodeState.RUNNING):
                # Cancel the node
                await self.db.execute(
                    "UPDATE dag_nodes SET state = ?, failure_reason = ? WHERE id = ?",
                    (NodeState.CANCELLED, "aggregator satisfied; sibling cancelled", pred_db_id),
                )
                # Kill worker if running
                if pred["worker_id"]:
                    proc = self._running_workers.pop(pred["worker_id"], None)
                    if proc and proc.returncode is None:
                        await self.runner.kill_worker(proc)

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

    # ── Dynamic DAG expansion ──────────────────────────────────────────────

    async def handle_expansion_request(
        self, bundle_id: str, requesting_node_id: str, params: CapRequestParams
    ) -> dict[str, Any]:
        """Handle a cap.request for dynamic DAG expansion.

        Validates the fragment, checks for cycles, grafts if auto-approvable,
        escalates to human otherwise.
        """
        if params.request_type != "expansion" or not params.expansion:
            return {"decision": "denied", "decision_id": None}

        expansion = params.expansion
        fragment = expansion.fragment
        graft_point = expansion.graft_point
        graft_after = expansion.graft_after_node or requesting_node_id

        # Validate: every node in fragment has unique id not already in dag_nodes
        existing = await self.db.fetch_all(
            "SELECT node_id FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
        )
        existing_ids = {r["node_id"] for r in existing}

        for n in fragment.nodes:
            if n.id in existing_ids:
                return {"decision": "denied", "decision_id": None,
                        "reason": f"Node id '{n.id}' already exists in DAG"}

        # Cycle check: merge fragment into current DAG, run topological sort
        all_edges = await self.db.fetch_all(
            "SELECT from_node_id, to_node_id FROM dag_edges WHERE bundle_id = ?",
            (bundle_id,),
        )
        if self._would_create_cycle(
            existing_ids, all_edges, fragment.nodes, fragment.edges, graft_after
        ):
            return {"decision": "denied", "decision_id": None,
                    "reason": "Expansion would create a cycle"}

        # Validate against expansion_policy (from bundle's proposal)
        total_nodes = len(existing_ids) + len(fragment.nodes)
        # [PROVISIONAL] 50-node default; consider moving to settings.json
        if total_nodes > 50:
            return {"decision": "denied", "decision_id": None,
                    "reason": "Expansion would exceed max total nodes"}

        # TODO (required before Phase 2.6): Check expansion fragment nodes have capability
        # manifests that are subsets of the bundle-level approved grant. Without this, workers
        # can self-grant elevated capabilities via expansion. See: capability.is_subset()
        # If auto-approvable, apply graft in a transaction
        expansion_id = f"exp_{bundle_id}_{len(existing_ids)}"
        now = self.now()

        async with self.db.transaction():
            # Insert expansion record
            await self.db.execute(
                "INSERT INTO dag_expansions (id, bundle_id, parent_node_id, "
                "graft_point_node_id, fragment_json, rationale, state, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    expansion_id,
                    bundle_id,
                    requesting_node_id,
                    graft_point,
                    json.dumps(fragment.model_dump()),
                    expansion.rationale,
                    "applied",
                    now,
                ),
            )

            # Insert fragment nodes
            for n in fragment.nodes:
                node_db_id = f"{bundle_id}:{n.id}"
                await self.db.execute(
                    "INSERT INTO dag_nodes (id, bundle_id, node_id, kind, spec_json, state) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (node_db_id, bundle_id, n.id, n.kind.value if hasattr(n.kind, 'value') else n.kind,
                     json.dumps(n.spec.model_dump()), NodeState.PENDING),
                )

            # Insert fragment edges
            for e in fragment.edges:
                await self.db.execute(
                    "INSERT INTO dag_edges (bundle_id, from_node_id, to_node_id, condition_kind) "
                    "VALUES (?, ?, ?, ?)",
                    (bundle_id, e.from_ if hasattr(e, 'from_') else e.get('from'),
                     e.to if hasattr(e, 'to') else e.get('to'),
                     e.condition.get("kind", "on_success") if isinstance(e.condition, dict) else "on_success"),
                )

            # Insert the graft edge: graft_after -> first node in fragment
            if fragment.nodes:
                graft_edge_condition = "always"  # grafted nodes fire on always
                await self.db.execute(
                    "INSERT INTO dag_edges (bundle_id, from_node_id, to_node_id, condition_kind) "
                    "VALUES (?, ?, ?, ?)",
                    (bundle_id, graft_after, fragment.nodes[0].id, graft_edge_condition),
                )

            await self._audit_expansion(bundle_id, expansion_id, expansion.rationale)

        return {"decision": "auto_approved", "decision_id": expansion_id}

    def _would_create_cycle(
        self,
        existing_ids: set[str],
        existing_edges: list[Any],
        fragment_nodes: list[Any],
        fragment_edges: list[Any],
        graft_after: str,
    ) -> bool:
        """Check if grafting the fragment would create a cycle. Topological sort approach."""
        # Build adjacency list of merged graph
        adj: dict[str, list[str]] = {}

        for nid in existing_ids:
            adj.setdefault(nid, [])

        for e in existing_edges:
            adj.setdefault(e["from_node_id"], []).append(e["to_node_id"])

        for n in fragment_nodes:
            nid = n.id if hasattr(n, 'id') else n.get('id', '')
            adj.setdefault(nid, [])

        for e in fragment_edges:
            src = e.from_ if hasattr(e, 'from_') else e.get('from', '')
            dst = e.to if hasattr(e, 'to') else e.get('to', '')
            adj.setdefault(src, []).append(dst)

        # Add graft edge
        if fragment_nodes:
            first_id = fragment_nodes[0].id if hasattr(fragment_nodes[0], 'id') else fragment_nodes[0].get('id', '')
            adj.setdefault(graft_after, []).append(first_id)

        # Kahn's algorithm for topological sort
        in_degree: dict[str, int] = {n: 0 for n in adj}
        for src, dsts in adj.items():
            for dst in dsts:
                in_degree[dst] = in_degree.get(dst, 0) + 1

        queue = [n for n, d in in_degree.items() if d == 0]
        visited = 0

        while queue:
            node = queue.pop(0)
            visited += 1
            for dst in adj.get(node, []):
                in_degree[dst] -= 1
                if in_degree[dst] == 0:
                    queue.append(dst)

        return visited != len(adj)

    async def _audit_expansion(self, bundle_id: str, expansion_id: str, rationale: str) -> None:
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dag_expansion_applied", "bundle", bundle_id,
             json.dumps({"expansion_id": expansion_id, "rationale": rationale}),
             self.now()),
        )

    # ── Mermaid rendering ──────────────────────────────────────────────────

    async def render_mermaid(self, bundle_id: str) -> str:
        """Render a bundle's DAG as a Mermaid diagram string."""
        from .visualizer import render_dag

        nodes = await self.db.fetch_all(
            "SELECT node_id, kind, state, spec_json FROM dag_nodes WHERE bundle_id = ?",
            (bundle_id,),
        )
        edges = await self.db.fetch_all(
            "SELECT from_node_id, to_node_id, condition_kind, condition_expr "
            "FROM dag_edges WHERE bundle_id = ?",
            (bundle_id,),
        )

        # Find entry/exit nodes
        all_node_ids = {n["node_id"] for n in nodes}
        from_ids = {e["from_node_id"] for e in edges}
        to_ids = {e["to_node_id"] for e in edges}

        entry_nodes = list(all_node_ids - to_ids)  # no incoming edges
        exit_nodes = list(all_node_ids - from_ids)  # no outgoing edges

        # Get grafted node ids from dag_expansions
        expansions = await self.db.fetch_all(
            "SELECT fragment_json FROM dag_expansions WHERE bundle_id = ? AND state = ?",
            (bundle_id, "applied"),
        )
        grafted_ids: set[str] = set()
        for exp in expansions:
            try:
                frag = json.loads(exp["fragment_json"])
                for n in frag.get("nodes", []):
                    grafted_ids.add(n.get("id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

        node_dicts = [
            {
                "node_id": n["node_id"],
                "kind": n["kind"],
                "state": n["state"],
                "spec": json.loads(n["spec_json"]) if n["spec_json"] else {},
            }
            for n in nodes
        ]
        edge_dicts = [
            {
                "from_node_id": e["from_node_id"],
                "to_node_id": e["to_node_id"],
                "condition_kind": e["condition_kind"],
                "condition_expr": e["condition_expr"],
            }
            for e in edges
        ]

        return render_dag(node_dicts, edge_dicts, entry_nodes, exit_nodes, grafted_ids)

    # ── Node state query ──────────────────────────────────────────────────

    async def get_node_state(self, bundle_id: str, node_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT state FROM dag_nodes WHERE id = ?", (f"{bundle_id}:{node_id}",)
        )
        return row["state"] if row else None
