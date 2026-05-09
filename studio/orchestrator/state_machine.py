"""Bundle state machine: 12 enum values, 8 transition handlers, IllegalTransitionError.

Phase 1 implements transitions: 1, 1a, 1b, 6, 9, 17, 19, 25.
Transitions 1a and 1b are PHASE-1-ONLY (gated on kernel_mode).
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .models import BundleState

if TYPE_CHECKING:
    from .db import Database

# Legal (from_state, to_state) pairs for Phase 1
_LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("(none)", BundleState.PROPOSED),      # 1: submit
    (BundleState.PROPOSED, BundleState.IN_REVIEW),    # 2: start pre-execution review
    (BundleState.PROPOSED, BundleState.APPROVED),    # 1a: kernel approve (bootstrapping)
    (BundleState.PROPOSED, BundleState.REJECTED),    # 1b: kernel reject (bootstrapping)
    (BundleState.IN_REVIEW, BundleState.APPROVED),    # 4: review passed
    (BundleState.IN_REVIEW, BundleState.REJECTED),    # rejection during review
    (BundleState.APPROVED, BundleState.IN_PROGRESS), # 6: execution start
    (BundleState.IN_PROGRESS, BundleState.PAUSED),    # pause execution
    (BundleState.PAUSED, BundleState.IN_PROGRESS),    # resume execution
    (BundleState.PAUSED, BundleState.REDIRECTING),    # redirect paused bundle
    (BundleState.REDIRECTING, BundleState.IN_PROGRESS), # redirect complete
    (BundleState.IN_PROGRESS, BundleState.VERIFYING), # 9: all exit nodes terminal
    (BundleState.VERIFYING, BundleState.COMPLETE),    # 17: verification passed
    (BundleState.VERIFYING, BundleState.FAILED),      # 19: verification failed
    (BundleState.IN_PROGRESS, BundleState.FAILED),    # 25: execution failure
})

TERMINAL_STATES: frozenset[str] = frozenset({
    BundleState.COMPLETE,
    BundleState.PARKED,
    BundleState.FAILED,
    BundleState.REJECTED,
    BundleState.ABORTED,
})


class IllegalTransitionError(Exception):
    """Raised when a transition is not in the legal set."""

    def __init__(self, current_state: str, attempted_transition: str, reason: str) -> None:
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        self.reason = reason
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"Illegal transition: {self.current_state} -> {self.attempted_transition}: {self.reason}"

    def to_jsonrpc_error(self, request_id: int | str | None = None) -> dict:
        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32001,
                "message": "illegal_transition",
                "data": {
                    "current_state": self.current_state,
                    "attempted_transition": self.attempted_transition,
                    "reason": self.reason,
                },
            },
            "id": request_id,
        }


def _validate_linear_dag(nodes: list[dict], edges: list[dict]) -> None:
    """Reject non-linear DAGs: Phase 1 only supports single-chain pipelines.

    A linear DAG has at most one outgoing edge and at most one incoming edge
    per node. Fan-out and fan-in are rejected.
    """
    if not nodes:
        return

    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}

    for n in nodes:
        node_id = n.get("node_id", n.get("id", ""))
        out_degree[node_id] = 0
        in_degree[node_id] = 0

    for e in edges:
        src = e.get("from_node_id", e.get("from", ""))
        dst = e.get("to_node_id", e.get("to", ""))
        out_degree[src] = out_degree.get(src, 0) + 1
        in_degree[dst] = in_degree.get(dst, 0) + 1

    for node_id, count in out_degree.items():
        if count > 1:
            raise IllegalTransitionError(
                "(none)", "proposed",
                f"Non-linear DAG not supported in Phase 1: node '{node_id}' has {count} outgoing edges (fan-out)."
            )

    for node_id, count in in_degree.items():
        if count > 1:
            raise IllegalTransitionError(
                "(none)", "proposed",
                f"Non-linear DAG not supported in Phase 1: node '{node_id}' has {count} incoming edges (fan-in)."
            )


def _check_legal(from_state: str, to_state: str) -> None:
    if (from_state, to_state) not in _LEGAL_TRANSITIONS:
        if from_state in TERMINAL_STATES:
            raise IllegalTransitionError(
                from_state, to_state,
                f"Bundle is {from_state}; transitions from terminal states are not allowed."
            )
        raise IllegalTransitionError(
            from_state, to_state,
            f"Transition {from_state} -> {to_state} is not legal in Phase 1."
        )


class BundleStateMachine:
    """Drives bundle lifecycle transitions with SQLite persistence."""

    def __init__(self, db: "Database", kernel_mode: bool = False) -> None:
        self.db = db
        self.kernel_mode = kernel_mode

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def _audit(
        self, event_type: str, subject_type: str, subject_id: str, payload: dict | None = None
    ) -> None:
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, subject_type, subject_id, json.dumps(payload or {}), self.now()),
        )

    # ── Transition 1: submit (bundle_input_received) ────────────────────

    async def transition_1_submit(
        self,
        bundle_id: str,
        repo: str,
        proposal_json: dict,
        dag_nodes: list[dict],
        dag_edges: list[dict],
    ) -> None:
        """Transition 1: (none) -> PROPOSED. Trigger: bundle_input_received."""
        _check_legal("(none)", BundleState.PROPOSED)
        _validate_linear_dag(dag_nodes, dag_edges)

        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO bundles (id, repo, state, tier, proposal_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bundle_id,
                    repo,
                    BundleState.PROPOSED,
                    "full_review",
                    json.dumps(proposal_json),
                    self.now(),
                ),
            )

            for node in dag_nodes:
                node_id = f"{bundle_id}:{node['node_id']}"
                await self.db.execute(
                    "INSERT INTO dag_nodes (id, bundle_id, node_id, kind, spec_json, state) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        bundle_id,
                        node["node_id"],
                        node.get("kind", "worker"),
                        json.dumps(node.get("spec", {})),
                        "pending",
                    ),
                )

            for edge in dag_edges:
                await self.db.execute(
                    "INSERT INTO dag_edges (bundle_id, from_node_id, to_node_id, condition_kind) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        bundle_id,
                        edge["from_node_id"],
                        edge["to_node_id"],
                        edge.get("condition", {}).get("kind", "on_success"),
                    ),
                )

            await self._audit("bundle_input_received", "bundle", bundle_id, {"state": BundleState.PROPOSED})

    # ── Transition 1-idea: submit idea-only (bundle_input_received, no DAG) ──

    async def transition_1_submit_idea(self, bundle_id: str, bundle_input: dict) -> None:
        """Transition 1 (idea-only): (none) -> PROPOSED. No DAG nodes/edges yet.

        The bundler worker will produce the DAG later; this just creates the bundle row
        with the raw bundle_input as proposal_json so the bundler has context.
        """
        _check_legal("(none)", BundleState.PROPOSED)

        repro = bundle_input.get("target_repo", "control-plane")
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO bundles (id, repo, state, tier, proposal_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bundle_id,
                    repro,
                    BundleState.PROPOSED,
                    "pending_review",
                    json.dumps({"bundle_input": bundle_input}),
                    self.now(),
                ),
            )
            await self._audit("bundle_input_received", "bundle", bundle_id,
                            {"state": BundleState.PROPOSED, "mode": "idea_only"})

    # ── Transition 1a: kernel approve (PROPOSED -> APPROVED) [PHASE-1-ONLY]

    async def transition_1a_approve(self, bundle_id: str, approved_by: str) -> None:
        """Transition 1a: PROPOSED -> APPROVED. Trigger: kernel_direct_approval."""
        if not self.kernel_mode:
            raise IllegalTransitionError(
                BundleState.PROPOSED, BundleState.APPROVED,
                "kernel_direct_approval requires kernel_mode=true."
            )

        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.APPROVED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.APPROVED)

        now = self.now()
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, approved_at = ?, approved_by = ? WHERE id = ?",
                (BundleState.APPROVED, now, approved_by, bundle_id),
            )
            await self.db.execute(
                "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (bundle_id, "approved", "cli", approved_by, now),
            )
            await self._audit("kernel_direct_approval", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.APPROVED, "by": approved_by})

    # ── Transition 1b: kernel reject (PROPOSED -> REJECTED) [PHASE-1-ONLY]

    async def transition_1b_reject(self, bundle_id: str, rejected_by: str, reason: str = "rejected via CLI") -> None:
        """Transition 1b: PROPOSED -> REJECTED. Trigger: kernel_direct_rejection."""
        if not self.kernel_mode:
            raise IllegalTransitionError(
                BundleState.PROPOSED, BundleState.REJECTED,
                "kernel_direct_rejection requires kernel_mode=true."
            )

        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.REJECTED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.REJECTED)

        now = self.now()
        outcome = {"status": "rejected", "rationale": reason}
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, completed_at = ?, outcome_json = ? WHERE id = ?",
                (BundleState.REJECTED, now, json.dumps(outcome), bundle_id),
            )
            await self.db.execute(
                "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bundle_id, "rejected", "cli", rejected_by, reason, now),
            )
            await self._audit("kernel_direct_rejection", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.REJECTED, "reason": reason})

    # ── Transition 2: start pre-execution review (PROPOSED -> IN_REVIEW) ───

    async def transition_2_start_review(self, bundle_id: str) -> None:
        """Transition 2: PROPOSED -> IN_REVIEW. Trigger: pre_execution_review_started."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.IN_REVIEW, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.IN_REVIEW)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.IN_REVIEW, bundle_id),
            )
            await self._audit("pre_execution_review_started", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.IN_REVIEW})

    # ── Transition: bundler planning complete -> IN_REVIEW ───────────────

    async def transition_complete_bundler_planning(
        self, bundle_id: str, proposal: dict
    ) -> None:
        """Bundler completed planning: insert DAG, merge proposal, PROPOSED -> IN_REVIEW."""
        row = await self.db.fetch_one("SELECT state, proposal_json FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.IN_REVIEW, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.IN_REVIEW)

        task_dag = proposal.get("task_dag", {})
        dag_nodes_raw = task_dag.get("nodes", [])
        dag_edges_raw = task_dag.get("edges", [])

        async with self.db.transaction():
            # Merge bundle_input with bundler-produced proposal
            existing = json.loads(row["proposal_json"] or "{}")
            merged = {**existing, "proposal": proposal}
            await self.db.execute(
                "UPDATE bundles SET proposal_json = ? WHERE id = ?",
                (json.dumps(merged), bundle_id),
            )

            # Insert DAG nodes from bundler output
            for node in dag_nodes_raw:
                node_id = f"{bundle_id}:{node['id']}"
                await self.db.execute(
                    "INSERT INTO dag_nodes (id, bundle_id, node_id, kind, spec_json, state) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        bundle_id,
                        node["id"],
                        node.get("kind", "worker"),
                        json.dumps(node.get("spec", {})),
                        "pending",
                    ),
                )

            # Insert DAG edges
            for edge in dag_edges_raw:
                await self.db.execute(
                    "INSERT INTO dag_edges (bundle_id, from_node_id, to_node_id, condition_kind) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        bundle_id,
                        edge.get("from", edge.get("from_node_id", "")),
                        edge.get("to", edge.get("to_node_id", "")),
                        edge.get("condition", {}).get("kind", "on_success"),
                    ),
                )

            # Fire transition to IN_REVIEW
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.IN_REVIEW, bundle_id),
            )
            await self._audit("bundle_planning_complete", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.IN_REVIEW})

    # ── Transition 4: review passed (IN_REVIEW -> APPROVED) ────────────

    async def transition_4_approve_from_review(self, bundle_id: str, approved_by: str) -> None:
        """Transition 4: IN_REVIEW -> APPROVED. Trigger: review_approved."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.APPROVED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.APPROVED)

        now = self.now()
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, approved_at = ?, approved_by = ? WHERE id = ?",
                (BundleState.APPROVED, now, approved_by, bundle_id),
            )
            await self.db.execute(
                "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (bundle_id, "approved", "cli", approved_by, now),
            )
            await self._audit("review_approved", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.APPROVED, "by": approved_by})

    # ── Transition: reject from IN_REVIEW ─────────────────────────────

    async def transition_reject_from_review(self, bundle_id: str, rejected_by: str, reason: str = "") -> None:
        """IN_REVIEW -> REJECTED. Trigger: review_rejected."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.REJECTED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.REJECTED)

        now = self.now()
        outcome = {"status": "rejected", "rationale": reason}
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, completed_at = ?, outcome_json = ? WHERE id = ?",
                (BundleState.REJECTED, now, json.dumps(outcome), bundle_id),
            )
            await self.db.execute(
                "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bundle_id, "rejected", "cli", rejected_by, reason, now),
            )
            await self._audit("review_rejected", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.REJECTED, "reason": reason})

    # ── Pause / Resume / Redirect ─────────────────────────────────────

    async def transition_pause(self, bundle_id: str, reason: str = "") -> None:
        """IN_PROGRESS -> PAUSED. Trigger: bundle_paused."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.PAUSED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.PAUSED)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.PAUSED, bundle_id),
            )
            await self._audit("bundle_paused", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.PAUSED, "reason": reason})

    async def transition_resume(self, bundle_id: str) -> None:
        """PAUSED -> IN_PROGRESS. Trigger: bundle_resumed."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.IN_PROGRESS, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.IN_PROGRESS)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.IN_PROGRESS, bundle_id),
            )
            await self._audit("bundle_resumed", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.IN_PROGRESS})

    async def transition_redirect(self, bundle_id: str, reason: str = "") -> None:
        """PAUSED -> REDIRECTING. Trigger: bundle_redirecting."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.REDIRECTING, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.REDIRECTING)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.REDIRECTING, bundle_id),
            )
            await self._audit("bundle_redirecting", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.REDIRECTING, "reason": reason})

    async def transition_redirect_complete(self, bundle_id: str) -> None:
        """REDIRECTING -> IN_PROGRESS. Trigger: redirect_complete."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.IN_PROGRESS, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.IN_PROGRESS)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.IN_PROGRESS, bundle_id),
            )
            await self._audit("redirect_complete", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.IN_PROGRESS})

    # ── Transition 6: execution start (APPROVED -> IN_PROGRESS) ────────

    async def transition_6_start_execution(self, bundle_id: str) -> None:
        """Transition 6: APPROVED -> IN_PROGRESS. Trigger: execution_started."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.IN_PROGRESS, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.IN_PROGRESS)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.IN_PROGRESS, bundle_id),
            )
            await self._audit("execution_started", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.IN_PROGRESS})

    # ── Transition 9: all exit nodes terminal (IN_PROGRESS -> VERIFYING)

    async def transition_9_to_verifying(self, bundle_id: str) -> None:
        """Transition 9: IN_PROGRESS -> VERIFYING. Trigger: all_exit_nodes_terminal."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.VERIFYING, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.VERIFYING)

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ? WHERE id = ?",
                (BundleState.VERIFYING, bundle_id),
            )
            await self._audit("all_exit_nodes_terminal", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.VERIFYING})

    # ── Transition 17: verification passed (VERIFYING -> COMPLETE) ─────

    async def transition_17_complete(self, bundle_id: str, outcome: dict | None = None) -> None:
        """Transition 17: VERIFYING -> COMPLETE. Trigger: verification_passed."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.COMPLETE, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.COMPLETE)

        now = self.now()
        outcome = outcome or {"status": "shipped"}
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, completed_at = ?, outcome_json = ? WHERE id = ?",
                (BundleState.COMPLETE, now, json.dumps(outcome), bundle_id),
            )
            await self._audit("verification_passed", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.COMPLETE})

    # ── Transition 19: verification failed (VERIFYING -> FAILED) ───────

    async def transition_19_fail_verification(self, bundle_id: str, reason: str = "") -> None:
        """Transition 19: VERIFYING -> FAILED. Trigger: verification_failed_no_rollback."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.FAILED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.FAILED)

        now = self.now()
        outcome = {"status": "failed_verification", "rationale": reason}
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, completed_at = ?, outcome_json = ? WHERE id = ?",
                (BundleState.FAILED, now, json.dumps(outcome), bundle_id),
            )
            await self._audit("verification_failed_no_rollback", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.FAILED, "reason": reason})

    # ── Transition 25: execution failure (IN_PROGRESS -> FAILED) ───────

    async def transition_25_fail_execution(self, bundle_id: str, reason: str = "unrecoverable DAG failure") -> None:
        """Transition 25: IN_PROGRESS -> FAILED. Trigger: bundle_failed_during_execution."""
        row = await self.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            raise IllegalTransitionError("(missing)", BundleState.FAILED, f"Bundle {bundle_id} not found")
        current = row["state"]
        _check_legal(current, BundleState.FAILED)

        now = self.now()
        outcome = {"status": "failed", "rationale": reason}
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE bundles SET state = ?, completed_at = ?, outcome_json = ? WHERE id = ?",
                (BundleState.FAILED, now, json.dumps(outcome), bundle_id),
            )
            await self._audit("bundle_failed_during_execution", "bundle", bundle_id,
                            {"from_state": current, "to_state": BundleState.FAILED, "reason": reason})
