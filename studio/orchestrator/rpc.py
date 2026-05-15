"""JSON-RPC 2.0 dispatcher and Unix domain socket connection manager.

16 Worker RPC methods: 11 full implementations, 5 stubbed (-32000).
Every method call is capability-checked against the worker's rpc.methods grant.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from .artifact import glob_match
from .capability import check_op, _rpc_method_covered
from .models import (
    BundleState,
    NodeState,
    WorkerState,
    HeartbeatPhase,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    HeartbeatParams,
    LogParams,
    ProgressReportParams,
    FinalReportParams,
    CapCheckParams,
    CapCheckResult,
    AskQuestionParams,
    ReportCheckpointParams,
    RespondToQueryParams,
    InjectContextParams,
)

if TYPE_CHECKING:
    from .db import Database
    from .artifact import ArtifactStore, SecretStore

# ── JSON-RPC error codes ──────────────────────────────────────────────────────

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
METHOD_NOT_IMPLEMENTED = -32000
CAPABILITY_DENIED = -32001

_STUB_METHODS: frozenset[str] = frozenset({
    "worker.pause",
    "worker.resume",
    "worker.cancel",
})

_INFRASTRUCTURE_METHODS: frozenset[str] = frozenset({
    "worker.ask_question",
    "worker.report_checkpoint",
    "worker.respond_to_query",
})


def _make_error(code: int, message: str, data: dict | None = None, req_id: Any = None) -> dict:
    resp: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    }
    if data:
        resp["error"]["data"] = data
    return resp


def _make_result(result: dict, req_id: Any = None) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


# ── Worker connection state ───────────────────────────────────────────────────

class WorkerBinding:
    """Tracks an active worker connection and its cached manifest."""

    def __init__(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        rpc_methods: list[str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.worker_id = worker_id
        self.bundle_id = bundle_id
        self.node_id = node_id
        self.rpc_methods = rpc_methods
        self.reader = reader
        self.writer = writer
        self.manifest_cache: dict[str, Any] | None = None


class SystemBinding:
    """Tracks a trusted system connection (MCP server) — no worker token, no capability checks."""

    def __init__(self, role: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.role = role
        self.reader = reader
        self.writer = writer


# ── RPC Dispatcher ────────────────────────────────────────────────────────────

# Handler accepts either WorkerBinding or SystemBinding
Handler = Callable[[WorkerBinding | SystemBinding, dict[str, Any], Any], Awaitable[dict]]


class RpcDispatcher:
    """JSON-RPC 2.0 method dispatcher with capability enforcement."""

    def __init__(self, db: "Database", sm: Any = None) -> None:
        self.db = db
        self.sm = sm  # BundleStateMachine reference, set after construction
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    async def dispatch(
        self, binding: WorkerBinding | SystemBinding, raw: bytes
    ) -> bytes | None:
        """Parse and dispatch a JSON-RPC message. Returns the response to send, or None for notifications."""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            resp = _make_error(PARSE_ERROR, "Parse error")
            return (json.dumps(resp) + "\n").encode()

        # Extract method, params, id
        method = body.get("method")
        params = body.get("params", {})
        req_id = body.get("id")

        # Validate basic structure
        if not isinstance(method, str) or not isinstance(params, dict):
            resp = _make_error(INVALID_REQUEST, "Invalid Request", req_id=req_id)
            return (json.dumps(resp) + "\n").encode()

        is_notification = req_id is None

        # ── Capability check (skipped for SystemBinding and infrastructure methods) ──
        if not isinstance(binding, SystemBinding) and method not in _INFRASTRUCTURE_METHODS:
            cap_ok, cap_reason = self._check_rpc_method(binding, method)
            if not cap_ok:
                if is_notification:
                    return None
                resp = _make_error(
                    CAPABILITY_DENIED,
                    f"capability_denied: {cap_reason}",
                    data={
                        "method": method,
                        "worker_rpc_methods": binding.rpc_methods,
                    },
                    req_id=req_id,
                )
                return (json.dumps(resp) + "\n").encode()

            # ── Stub check (only for WorkerBinding) ──
            if method in _STUB_METHODS:
                if is_notification:
                    return None
                resp = _make_error(
                    METHOD_NOT_IMPLEMENTED,
                    f"method_not_implemented: {method}",
                    data={"method": method, "phase": "Phase 1 stub"},
                    req_id=req_id,
                )
                return (json.dumps(resp) + "\n").encode()

        # ── Dispatch ──
        handler = self._handlers.get(method)
        if handler is None:
            if is_notification:
                return None
            resp = _make_error(METHOD_NOT_FOUND, f"Method not found: {method}", req_id=req_id)
            return (json.dumps(resp) + "\n").encode()

        try:
            result = await handler(binding, params, req_id)
        except Exception as exc:
            if is_notification:
                return None
            resp = _make_error(
                INTERNAL_ERROR,
                f"Internal error: {exc}",
                req_id=req_id,
            )
            return (json.dumps(resp) + "\n").encode()

        if is_notification:
            return None

        return (json.dumps(_make_result(result, req_id)) + "\n").encode()

    def _check_rpc_method(self, binding: WorkerBinding, method: str) -> tuple[bool, str]:
        """Check if the worker is allowed to call this RPC method."""
        for pattern in binding.rpc_methods:
            if _rpc_method_covered(method, pattern):
                return (True, "")
        return (False, f"no rpc.methods pattern covers '{method}'")


# ── Standard handler implementations ──────────────────────────────────────────

class RpcHandlers:
    """Full and stub implementations for all Worker RPC methods."""

    def __init__(self, db: "Database") -> None:
        self.db = db
        self._on_final_report: Callable[[str, str, str, dict], Awaitable[None]] | None = None
        self._on_heartbeat: Callable[[str, str], Awaitable[None]] | None = None
        self._on_cap_request: Callable[[str, str, dict], Awaitable[dict[str, Any]]] | None = None
        self._on_bundler_report: Callable[[str, dict], Awaitable[None]] | None = None
        self._on_bundler_failure: Callable[[str, str], Awaitable[None]] | None = None
        self._on_review_complete: Callable[[str, str, dict], Awaitable[None]] | None = None
        self._on_review_blocking: Callable[[str, str], Awaitable[None]] | None = None
        self._on_qa_pass: Callable[[str, dict], Awaitable[None]] | None = None
        self._on_qa_fail: Callable[[str, str, dict], Awaitable[None]] | None = None
        self._on_inject_context: Callable[[str, str, str, str, str | None], Awaitable[None]] | None = None
        self._artifact_store: "ArtifactStore | None" = None
        self._secret_store: "SecretStore | None" = None

    def set_on_final_report(self, cb: Callable[[str, str, str, dict], Awaitable[None]]) -> None:
        """Callback: on_final_report(bundle_id, node_id, worker_id, outcome)."""
        self._on_final_report = cb

    def set_on_heartbeat(self, cb: Callable[[str, str], Awaitable[None]]) -> None:
        """Callback: on_heartbeat(worker_id, phase). Called on every heartbeat."""
        self._on_heartbeat = cb

    def set_on_cap_request(self, cb: Callable[[str, str, dict], Awaitable[dict[str, Any]]]) -> None:
        """Callback: on_cap_request(bundle_id, node_id, request_params_dict)."""
        self._on_cap_request = cb

    def set_on_bundler_report(self, cb: Callable[[str, dict], Awaitable[None]]) -> None:
        """Callback: on_bundler_report(bundle_id, proposal_dict)."""
        self._on_bundler_report = cb

    def set_on_bundler_failure(self, cb: Callable[[str, str], Awaitable[None]]) -> None:
        """Callback: on_bundler_failure(bundle_id, reason)."""
        self._on_bundler_failure = cb

    def set_on_review_complete(self, cb: Callable[[str, str, dict], Awaitable[None]]) -> None:
        """Callback: on_review_complete(bundle_id, role, findings_dict)."""
        self._on_review_complete = cb

    def set_on_review_blocking(self, cb: Callable[[str, str], Awaitable[None]]) -> None:
        """Callback: on_review_blocking(bundle_id, blocking_reason)."""
        self._on_review_blocking = cb

    def set_on_qa_pass(self, cb: Callable[[str, dict], Awaitable[None]]) -> None:
        """Callback: on_qa_pass(bundle_id, verification_report)."""
        self._on_qa_pass = cb

    def set_on_qa_fail(self, cb: Callable[[str, str, dict], Awaitable[None]]) -> None:
        """Callback: on_qa_fail(bundle_id, reason, verification_report)."""
        self._on_qa_fail = cb

    def set_on_inject_context(self, cb: Callable[[str, str, str, str, str | None], Awaitable[None]]) -> None:
        """Callback: on_inject_context(worker_id, injection_id, type, content, question_id)."""
        self._on_inject_context = cb

    def set_artifact_store(self, store: "ArtifactStore") -> None:
        self._artifact_store = store

    def set_secret_store(self, store: "SecretStore") -> None:
        self._secret_store = store

    def set_sm(self, sm: Any) -> None:
        """Set state machine reference for MCP handlers."""
        self.sm = sm

    @staticmethod
    def now() -> int:
        import time
        return int(time.time())

    # ── worker.heartbeat (full) ──────────────────────────────────────────

    async def handle_heartbeat(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        phase = params.get("phase", "starting")
        progress = params.get("progress", "")
        current_step = params.get("current_step")
        estimated = params.get("estimated_completion_seconds")

        now = self.now()

        # Check current worker state
        row = await self.db.fetch_one("SELECT state FROM workers WHERE id = ?", (binding.worker_id,))
        if row is None:
            return {"accepted": False, "reason": "worker not found"}

        current_state = row["state"]

        if current_state == "pending":
            await self.db.execute(
                "UPDATE workers SET state = ?, last_heartbeat = ?, current_phase = ? WHERE id = ?",
                (WorkerState.RUNNING, now, phase, binding.worker_id),
            )
        else:
            await self.db.execute(
                "UPDATE workers SET last_heartbeat = ?, current_phase = ? WHERE id = ?",
                (now, phase, binding.worker_id),
            )
        await self.db.conn.commit()

        if self._on_heartbeat:
            await self._on_heartbeat(binding.worker_id, phase)

        return {"accepted": True, "phase": phase, "state": WorkerState.RUNNING}

    # ── worker.log (full) ────────────────────────────────────────────────

    async def handle_log(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        level = params.get("level", "info")
        message = params.get("message", "")
        structured_data = params.get("structured_data")

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"worker.log.{level}",
                "worker",
                binding.worker_id,
                json.dumps({"message": message, "structured_data": structured_data}),
                self.now(),
            ),
        )
        await self.db.conn.commit()
        return {"logged": True}

    # ── worker.progress_report (full) ────────────────────────────────────

    async def handle_progress_report(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        stage = params.get("stage", "")
        percent = params.get("percent", 0)
        message = params.get("message", "")

        await self.db.execute(
            "UPDATE dag_nodes SET output_json = ? WHERE id = ?",
            (json.dumps({"progress": {"stage": stage, "percent": percent, "message": message}}),
             f"{binding.bundle_id}:{binding.node_id}"),
        )
        await self.db.conn.commit()
        return {"accepted": True}

    # ── worker.request_human_input (full) ─────────────────────────────────

    async def handle_request_human_input(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        """Create an approval_requests row and return the request_id for polling."""
        import ulid
        request_id = str(ulid.ULID())
        now = self.now()

        await self.db.execute(
            "INSERT INTO approval_requests (id, bundle_id, kind, subject_id, context_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                binding.bundle_id,
                "human_input",
                binding.worker_id,
                json.dumps({
                    "question": params.get("question", ""),
                    "context": params.get("context", ""),
                    "options": params.get("options"),
                }),
                "pending",
                now,
            ),
        )
        await self.db.conn.commit()

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "worker.human_input_requested",
                "bundle",
                binding.bundle_id,
                json.dumps({"request_id": request_id, "worker_id": binding.worker_id}),
                now,
            ),
        )
        await self.db.conn.commit()

        return {"request_id": request_id, "state": "pending"}

    # ── worker.poll_human_input (full) ────────────────────────────────────

    async def handle_poll_human_input(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        request_id = params.get("request_id", "")
        row = await self.db.fetch_one(
            "SELECT id, state, decision, decided_at, decided_by FROM approval_requests WHERE id = ?",
            (request_id,),
        )
        if row is None:
            return {"pending": True, "error": "request not found"}

        if row["state"] == "pending":
            return {"pending": True}

        return {
            "pending": False,
            "response": row["decision"] or "",
            "responded_at": row["decided_at"],
            "responded_by": row["decided_by"] or "",
        }

    # ── worker.final_report (full) ───────────────────────────────────────

    async def handle_final_report(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        outcome = params.get("outcome", "failure")
        summary = params.get("summary", "")
        now = self.now()

        # ── Bundler worker: no dag_node, produces proposal + DAG ──
        if binding.node_id == "bundler":
            proposal = params.get("proposal", {})
            await self.db.execute(
                "UPDATE workers SET state = ?, ended_at = ? WHERE id = ?",
                (
                    WorkerState.COMPLETE if outcome == "success" else WorkerState.FAILED,
                    now,
                    binding.worker_id,
                ),
            )
            await self.db.conn.commit()

            if outcome == "success":
                if self._on_bundler_report:
                    await self._on_bundler_report(binding.bundle_id, proposal)
            elif self._on_bundler_failure:
                await self._on_bundler_failure(binding.bundle_id, summary)

            return {"accepted": True, "bundler": True}

        # ── Review track workers (adversarial, security, qa) ──
        if binding.node_id in ("adversarial", "security", "qa"):
            findings = params.get("findings", [])
            blocking = params.get("blocking_issue", False)
            blocking_reason = params.get("blocking_reason", "")
            threat_model = params.get("threat_model")
            verification_plan = params.get("verification_plan")

            await self.db.execute(
                "UPDATE workers SET state = ?, ended_at = ? WHERE id = ?",
                (WorkerState.COMPLETE if outcome == "success" else WorkerState.FAILED,
                 now, binding.worker_id),
            )

            # Store findings in dag_nodes.output_json for aggregator
            output = {"findings": findings, "role": binding.node_id, "outcome": outcome}
            if threat_model:
                output["threat_model"] = threat_model
            if verification_plan:
                output["verification_plan"] = verification_plan

            node_db_id = f"{binding.bundle_id}:{binding.node_id}"
            await self.db.execute(
                "UPDATE dag_nodes SET state = ?, output_json = ? WHERE id = ?",
                (NodeState.COMPLETED if outcome == "success" else NodeState.FAILED,
                 json.dumps(output), node_db_id),
            )
            await self.db.conn.commit()

            # Blocking issue: fire Transition 3 (IN_REVIEW -> PROPOSED)
            if blocking and self._on_review_blocking:
                await self._on_review_blocking(binding.bundle_id, blocking_reason)

            # Publish findings as bundle-scoped artifact
            if self._artifact_store and findings:
                artifact_name = {
                    "adversarial": "adversarial-findings",
                    "security": "security-findings",
                    "qa": "verification-plan",
                }.get(binding.node_id, f"{binding.node_id}-findings")
                try:
                    await self._artifact_store.publish(
                        namespace="bundle",
                        name=artifact_name,
                        version=binding.bundle_id,
                        content_type="application/json",
                        data=json.dumps(output).encode("utf-8"),
                        bundle_id=binding.bundle_id,
                    )
                except Exception:
                    pass  # artifact publish is best-effort; findings are also in output_json

            if self._on_review_complete and outcome == "success":
                await self._on_review_complete(binding.bundle_id, binding.node_id, findings)

            return {"accepted": True, "review": True, "role": binding.node_id}

        # ── Post-execution QA verification worker ──
        if binding.node_id == "qa-verification":
            verification_report = params.get("verification_report", {})

            await self.db.execute(
                "UPDATE workers SET state = ?, ended_at = ? WHERE id = ?",
                (
                    WorkerState.COMPLETE if outcome == "success" else WorkerState.FAILED,
                    now,
                    binding.worker_id,
                ),
            )
            await self.db.conn.commit()

            if outcome == "success" and self._on_qa_pass:
                await self._on_qa_pass(binding.bundle_id, verification_report)
            elif self._on_qa_fail:
                await self._on_qa_fail(binding.bundle_id, summary, verification_report)

            return {"accepted": True, "qa": True}

        # ── DAG worker ──
        files_changed = params.get("files_changed", [])
        tests_run = params.get("tests_run", 0)
        tests_passed = params.get("tests_passed", 0)
        tests_failed = params.get("tests_failed", 0)
        errors = params.get("errors", [])

        node_id = f"{binding.bundle_id}:{binding.node_id}"

        if outcome == "success":
            new_node_state = NodeState.COMPLETED
        elif outcome == "paused":
            new_node_state = NodeState.FAILED
        else:
            new_node_state = NodeState.FAILED

        await self.db.execute(
            "UPDATE dag_nodes SET state = ?, ended_at = ?, output_json = ? WHERE id = ?",
            (new_node_state, now, json.dumps({
                "outcome": outcome,
                "files_changed": files_changed,
                "tests_run": tests_run,
                "tests_passed": tests_passed,
                "tests_failed": tests_failed,
                "errors": errors,
                "summary": summary,
            }), node_id),
        )

        await self.db.execute(
            "UPDATE workers SET state = ?, ended_at = ?, exit_reason = ? WHERE id = ?",
            (
                WorkerState.COMPLETE if outcome == "success" else WorkerState.FAILED,
                now,
                None if outcome == "success" else "worker_reported_failure",
                binding.worker_id,
            ),
        )
        await self.db.conn.commit()

        if self._on_final_report:
            await self._on_final_report(binding.bundle_id, binding.node_id, binding.worker_id, {
                "outcome": outcome,
                "node_state": new_node_state,
            })

        return {"accepted": True, "node_state": new_node_state}

    # ── worker.query_status (full) ───────────────────────────────────────

    async def handle_query_status(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        node_id = f"{binding.bundle_id}:{binding.node_id}"
        node_row = await self.db.fetch_one(
            "SELECT state, output_json FROM dag_nodes WHERE id = ?", (node_id,)
        )
        worker_row = await self.db.fetch_one(
            "SELECT state, last_heartbeat, current_phase FROM workers WHERE id = ?",
            (binding.worker_id,),
        )
        return {
            "node_state": node_row["state"] if node_row else "unknown",
            "worker_state": worker_row["state"] if worker_row else "unknown",
            "last_heartbeat": worker_row["last_heartbeat"] if worker_row else None,
            "current_phase": worker_row["current_phase"] if worker_row else None,
        }

    # ── cap.request (full) ───────────────────────────────────────────────

    async def handle_cap_request(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        """Handle cap.request for dynamic DAG expansion."""
        if not self._on_cap_request:
            return {"decision": "denied", "decision_id": None,
                    "reason": "cap.request not wired to executor"}

        try:
            return await self._on_cap_request(
                binding.bundle_id, binding.node_id, params
            )
        except Exception as exc:
            return {"decision": "denied", "decision_id": None,
                    "reason": str(exc)}

    # ── cap.check (full) ─────────────────────────────────────────────────

    async def handle_cap_check(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        op_descriptor = params.get("op_descriptor", "")

        # Load manifest from DB if not cached
        if binding.manifest_cache is None:
            row = await self.db.fetch_one(
                "SELECT manifest_json FROM workers WHERE id = ?", (binding.worker_id,)
            )
            if row and row["manifest_json"]:
                binding.manifest_cache = json.loads(row["manifest_json"])

        if binding.manifest_cache is None:
            await self._audit_cap_check(binding, op_descriptor, False)
            return {"allowed": False, "capability_id": None}

        # Use the pure check_op function — need to import the model
        from .models import CapabilityManifest
        try:
            manifest = CapabilityManifest.model_validate(binding.manifest_cache)
        except Exception:
            await self._audit_cap_check(binding, op_descriptor, False)
            return {"allowed": False, "capability_id": None}

        allowed, _ = check_op(op_descriptor, manifest)
        await self._audit_cap_check(binding, op_descriptor, allowed)
        return {"allowed": allowed, "capability_id": None}

    async def _audit_cap_check(self, binding: WorkerBinding, op: str, allowed: bool) -> None:
        await self.db.execute(
            "INSERT INTO capability_checks (worker_id, bundle_id, requested_op, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (binding.worker_id, binding.bundle_id, op,
             "allowed" if allowed else "denied", self.now()),
        )
        await self.db.conn.commit()

    # ── mcp.* handlers (system surface, no capability check) ────────────────
    # Called by the MCP server process over the trusted Unix socket.

    async def handle_mcp_approve_bundle(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        comment = params.get("comment", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        try:
            await self.sm.transition_4_approve_from_review(bundle_id, "mcp")
        except Exception as exc:
            return {"error": "ILLEGAL_TRANSITION", "detail": str(exc)}
        if comment:
            await self._audit_mcp("mcp_approve_comment", "bundle", bundle_id,
                                 {"comment": comment, "actor": "mcp"})
        return {"transition": "APPROVED", "bundle_id": bundle_id, "new_state": "approved"}

    async def handle_mcp_reject_bundle(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        reason = params.get("reason", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        if not reason:
            return {"error": "INVALID_PARAMS", "detail": "reason is required"}
        try:
            await self.sm.transition_reject_from_review(bundle_id, "mcp", reason)
        except Exception as exc:
            return {"error": "ILLEGAL_TRANSITION", "detail": str(exc)}
        return {"transition": "REJECTED", "bundle_id": bundle_id, "new_state": "rejected"}

    async def handle_mcp_request_modification(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        instructions = params.get("instructions", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        if not instructions:
            return {"error": "INVALID_PARAMS", "detail": "instructions is required"}
        try:
            await self.sm.transition_3_return_to_proposed(bundle_id, instructions)
        except Exception as exc:
            return {"error": "ILLEGAL_TRANSITION", "detail": str(exc)}
        return {"transition": "PROPOSED", "bundle_id": bundle_id, "new_state": "proposed",
                "message": "Bundler will revise based on instructions."}

    async def handle_mcp_escalate_bundle(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        reason = params.get("reason", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        row = await self.db.fetch_one("SELECT tier FROM bundles WHERE id = ?", (bundle_id,))
        if row is None:
            return {"error": "NOT_FOUND", "detail": f"Bundle {bundle_id} does not exist"}
        current_tier = row["tier"]
        tier_order = ["auto", "auto_notify", "summary", "full_review", "full_review_cooldown"]
        try:
            idx = tier_order.index(current_tier)
        except ValueError:
            idx = -1
        if idx < 0 or idx >= len(tier_order) - 1:
            return {"error": "ILLEGAL_TRANSITION",
                    "detail": f"Bundle is already at highest tier: {current_tier}"}
        new_tier = tier_order[idx + 1]
        now = self.now()
        await self.db.execute("UPDATE bundles SET tier = ? WHERE id = ?", (new_tier, bundle_id))
        await self.db.execute(
            "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bundle_id, "escalated", "mcp", "mcp", reason, now),
        )
        await self.db.conn.commit()
        await self._audit_mcp("bundle_escalated", "bundle", bundle_id,
                              {"previous_tier": current_tier, "new_tier": new_tier, "reason": reason})
        return {"bundle_id": bundle_id, "new_tier": new_tier, "previous_tier": current_tier,
                "message": f"Bundle escalated to {new_tier}."}

    async def handle_mcp_pause_bundle(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        try:
            await self.sm.transition_pause(bundle_id)
        except Exception as exc:
            return {"error": "ILLEGAL_TRANSITION", "detail": str(exc)}
        workers_row = await self.db.fetch_all(
            "SELECT COUNT(*) as cnt FROM workers WHERE bundle_id = ? AND state IN ('running','pending')",
            (bundle_id,),
        )
        workers_waiting = workers_row[0]["cnt"] if workers_row else 0
        return {"transition": "PAUSED", "bundle_id": bundle_id, "new_state": "paused",
                "workers_waiting": workers_waiting}

    async def handle_mcp_resume_bundle(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        bundle_id = params.get("id", "")
        note = params.get("note", "")
        if not bundle_id:
            return {"error": "INVALID_PARAMS", "detail": "id is required"}
        try:
            await self.sm.transition_resume(bundle_id)
        except Exception as exc:
            return {"error": "ILLEGAL_TRANSITION", "detail": str(exc)}
        if note:
            await self._audit_mcp("bundle_resumed_note", "bundle", bundle_id, {"note": note})
        return {"transition": "RESUMED", "bundle_id": bundle_id, "new_state": "in_progress"}

    async def handle_mcp_kill_worker(self, binding: SystemBinding, params: dict, req_id: Any) -> dict:
        worker_id = params.get("worker_id", "")
        reason = params.get("reason", "")
        if not worker_id:
            return {"error": "INVALID_PARAMS", "detail": "worker_id is required"}
        now = self.now()
        await self.db.execute(
            "UPDATE workers SET state = ?, ended_at = ?, exit_reason = ? WHERE id = ?",
            (WorkerState.KILLED, now, f"killed via mcp: {reason}" if reason else "killed via mcp", worker_id),
        )
        await self.db.conn.commit()
        await self._audit_mcp("worker_killed", "worker", worker_id,
                              {"reason": reason, "surface": "mcp"})
        return {"worker_id": worker_id, "action": "cancel_dispatched",
                "message": "Cancel sent to worker."}

    async def _audit_mcp(self, event_type: str, subject_type: str, subject_id: str,
                          payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, subject_type, subject_id, json.dumps(payload), self.now()),
        )
        await self.db.conn.commit()

    # ── artifact.publish (full) ────────────────────────────────────────────

    async def _load_manifest(self, binding: WorkerBinding) -> dict[str, Any] | None:
        if binding.manifest_cache is not None:
            return binding.manifest_cache
        row = await self.db.fetch_one(
            "SELECT manifest_json FROM workers WHERE id = ?", (binding.worker_id,)
        )
        if row and row["manifest_json"]:
            binding.manifest_cache = json.loads(row["manifest_json"])
        return binding.manifest_cache

    async def _check_artifact_capability(
        self, binding: WorkerBinding, descriptor: dict, access: str
    ) -> str | None:
        """Check whether descriptor matches any of the worker's access patterns.

        Returns None if allowed, or an error code string if denied.
        """
        manifest = await self._load_manifest(binding)
        if manifest is None:
            return "manifest_not_found"

        grants = manifest.get("grants", {})
        rpc_grants = grants.get("rpc", {})
        artifact_access = rpc_grants.get("artifact_access", {})
        patterns = artifact_access.get(access, [])

        for pattern in patterns:
            ns_match = pattern.get("namespace", "*") in ("*", descriptor.get("namespace", ""))
            name_match = glob_match(pattern.get("name", "*"), descriptor.get("name", ""))
            version_pattern = pattern.get("version") or "*"
            version_match = glob_match(version_pattern, descriptor.get("version") or "")
            ct_match = glob_match(pattern.get("content_type", "*"), descriptor.get("content_type", ""))

            if ns_match and name_match and version_match and ct_match:
                return None  # allowed

        return "capability_denied"

    async def handle_artifact_publish(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        from .artifact import ArtifactDescriptor
        if self._artifact_store is None:
            return _make_error(INTERNAL_ERROR, "artifact store not configured", req_id=req_id)

        descriptor_raw = params.get("descriptor", {})
        data_b64 = params.get("data", "")

        if not data_b64 or not descriptor_raw:
            return _make_error(INVALID_PARAMS, "Missing descriptor or data", req_id=req_id)

        try:
            data = base64.b64decode(data_b64)
        except Exception:
            return _make_error(INVALID_PARAMS, "Invalid base64 data", req_id=req_id)

        descriptor = ArtifactDescriptor.from_dict(descriptor_raw)

        # Validate descriptor
        if descriptor.namespace not in ("bundle", "global", "task"):
            return _make_error(-32004, f"Invalid descriptor namespace: {descriptor.namespace}", req_id=req_id)
        if not descriptor.name:
            return _make_error(-32004, "Descriptor name is empty", req_id=req_id)

        # Capability check
        error = await self._check_artifact_capability(binding, descriptor_raw, "writes")
        if error:
            return _make_error(CAPABILITY_DENIED, "Worker lacks write capability for this descriptor",
                             req_id=req_id, data={"descriptor": descriptor_raw})

        # Namespace write restrictions
        if descriptor.namespace == "global":
            manifest = await self._load_manifest(binding)
            writes = manifest.get("grants", {}).get("rpc", {}).get("artifact_access", {}).get("writes", [])
            has_global = any(p.get("namespace") == "global" for p in writes)
            if not has_global:
                return _make_error(-32005, "Global namespace requires explicit global write grant",
                                 req_id=req_id)
        elif descriptor.namespace == "bundle":
            pass  # bundle namespace resolves to worker's own bundle at store level

        try:
            h = await self._artifact_store.put(descriptor, data)
            return _make_result({
                "published": True,
                "hash": h,
                "size_bytes": len(data),
            }, req_id)
        except Exception as exc:
            return _make_error(INTERNAL_ERROR, str(exc), req_id=req_id)

    async def handle_artifact_fetch(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        from .artifact import ArtifactDescriptor
        if self._artifact_store is None:
            return _make_error(INTERNAL_ERROR, "artifact store not configured", req_id=req_id)

        descriptor_raw = params.get("descriptor", {})
        if not descriptor_raw:
            return _make_error(INVALID_PARAMS, "Missing descriptor", req_id=req_id)

        descriptor = ArtifactDescriptor.from_dict(descriptor_raw)

        # Capability check
        error = await self._check_artifact_capability(binding, descriptor_raw, "reads")
        if error:
            return _make_error(CAPABILITY_DENIED, "Worker lacks read capability for this descriptor",
                             req_id=req_id, data={"descriptor": descriptor_raw})

        meta = await self._artifact_store.get_metadata(descriptor)
        if meta is None:
            return _make_error(-32006, "artifact_not_found", req_id=req_id)

        if meta.gc_d_at is not None:
            return _make_error(-32007, "artifact_gc_d",
                             req_id=req_id,
                             data={"gc_d_at": meta.gc_d_at, "reason": "gc_d"})

        data = await self._artifact_store.get(descriptor)
        if data is None:
            return _make_error(-32008, "verification_failed", req_id=req_id)

        return _make_result({
            "data": base64.b64encode(data).decode(),
            "hash": meta.hash,
            "size_bytes": meta.size_bytes,
        }, req_id)

    async def handle_artifact_list(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        if self._artifact_store is None:
            return _make_error(INTERNAL_ERROR, "artifact store not configured", req_id=req_id)

        # Check RPC methods grant for artifact.list
        manifest = await self._load_manifest(binding)
        if manifest is None:
            return _make_error(CAPABILITY_DENIED, "missing manifest", req_id=req_id)

        rpc_methods = manifest.get("grants", {}).get("rpc", {}).get("methods", [])
        if "artifact.list" not in rpc_methods and "artifact.*" not in rpc_methods:
            method_covered = any(
                m == "artifact.list" or m == "artifact.*" or m == "*"
                for m in rpc_methods
            )
            if not method_covered:
                return _make_error(CAPABILITY_DENIED, "Worker lacks artifact.list method grant",
                                 req_id=req_id)

        namespace = params.get("namespace") or binding.bundle_id
        name_pattern = params.get("name_pattern")

        # Validate name_pattern is a valid glob
        if name_pattern:
            try:
                re.compile(name_pattern.replace("*", ".*"))
            except re.error:
                return _make_error(-32004, f"Invalid name_pattern glob: {name_pattern}", req_id=req_id)

        results = await self._artifact_store.list(namespace, name_pattern)

        # Filter to only those matching worker's read patterns
        reads = manifest.get("grants", {}).get("rpc", {}).get("artifact_access", {}).get("reads", [])
        if reads:
            filtered = []
            for meta in results:
                for pattern in reads:
                    if (glob_match(pattern.get("namespace", "*"), meta.namespace)
                            and glob_match(pattern.get("name", "*"), meta.name)
                            and glob_match(pattern.get("version", "*"), meta.version)
                            and glob_match(pattern.get("content_type", "*"), meta.content_type)):
                        filtered.append(meta)
                        break
            results = filtered

        artifacts = [{
            "descriptor": {
                "namespace": m.namespace,
                "name": m.name,
                "version": m.version,
                "content_type": m.content_type,
            },
            "hash": m.hash,
            "size_bytes": m.size_bytes,
            "published_at": m.published_at,
        } for m in results]

        return _make_result({"artifacts": artifacts}, req_id)

    async def handle_secrets_fetch(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        if self._secret_store is None:
            return _make_error(INTERNAL_ERROR, "secret store not configured", req_id=req_id)

        name = params.get("name", "")
        if not name:
            return _make_error(INVALID_PARAMS, "Missing name", req_id=req_id)

        # Check capability — worker must have a secrets grant matching this name
        manifest = await self._load_manifest(binding)
        if manifest is None:
            return _make_error(CAPABILITY_DENIED, "missing manifest", req_id=req_id)

        secrets_grants = manifest.get("grants", {}).get("secrets", [])
        matching = [g for g in secrets_grants if g.get("name") == name]
        if not matching:
            return _make_error(CAPABILITY_DENIED, f"Worker lacks secrets grant for '{name}'", req_id=req_id)

        value, expires_at = self._secret_store.fetch(name)
        if value is None:
            return _make_error(-32010, f"secret_not_found: {name}", req_id=req_id)

        # Audit trail: both DB audit_log and file-based credential-use log
        grant = matching[0]
        audit_payload = {
            "worker_id": binding.worker_id,
            "bundle_id": binding.bundle_id,
            "task_id": binding.node_id,
            "secret_name": name,
            "purpose": grant.get("purpose", "custom"),
            "method": "secrets.fetch",
            "timestamp": self.now(),
        }
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("secret_access", "secret", name, json.dumps(audit_payload), self.now()),
        )
        await self.db.conn.commit()

        audit_dir = Path("memory/audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        with open(audit_dir / "credential-use.jsonl", "a") as f:
            f.write(json.dumps(audit_payload) + "\n")

        return _make_result({
            "value": value,
            "expires_at": expires_at,
        }, req_id)

    # ── worker.ask_question (Bundle 5.1) ──────────────────────────────────

    async def handle_ask_question(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        question_id = params.get("question_id", "")
        question = params.get("question", "")
        context = params.get("context", "")
        blocking = params.get("blocking", False)
        urgency = params.get("urgency", "medium")

        if not question_id or not question:
            return {"status": "rejected", "reason": "question_id and question are required"}

        now = self.now()

        # Increment questions_asked counter
        await self.db.execute(
            "UPDATE workers SET questions_asked = questions_asked + 1 WHERE id = ?",
            (binding.worker_id,),
        )
        await self.db.conn.commit()

        # Fetch updated count
        row = await self.db.fetch_one(
            "SELECT questions_asked FROM workers WHERE id = ?", (binding.worker_id,)
        )
        questions_asked = row["questions_asked"] if row else 0

        # Insert question record
        await self.db.execute(
            "INSERT INTO worker_questions (question_id, worker_id, bundle_id, question, context, "
            "blocking, urgency, status, asked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (question_id, binding.worker_id, binding.bundle_id, question, context,
             int(blocking), urgency, "pending", now),
        )
        await self.db.conn.commit()

        # Route question (LLM or escalate)
        await self._route_question(
            binding.worker_id, binding.bundle_id, question_id, question, context,
            blocking, questions_asked
        )

        return {"status": "received", "question_id": question_id}

    async def _route_question(self, worker_id: str, bundle_id: str, question_id: str,
                               question: str, context: str, blocking: bool,
                               questions_asked: int) -> None:
        now = self.now()
        max_questions = 10

        if questions_asked > max_questions:
            # Rate limit exceeded — escalate directly
            await self.db.execute(
                "UPDATE worker_questions SET status = ? WHERE question_id = ?",
                ("escalated", question_id),
            )
            await self.db.conn.commit()
            await self.db.execute(
                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("worker.question_rate_limited", "worker", worker_id,
                 json.dumps({"question_id": question_id, "questions_asked": questions_asked}), now),
            )
            await self.db.conn.commit()
            return

        # Attempt LLM answer
        answer, confidence = await self._call_question_llm(bundle_id, question, context)

        if confidence == "high" and answer:
            await self.db.execute(
                "UPDATE worker_questions SET status = ?, answer = ?, answered_at = ? WHERE question_id = ?",
                ("answered", answer, now, question_id),
            )
            await self.db.conn.commit()

            # Deliver answer via inject_context
            if self._on_inject_context:
                import ulid
                injection_id = str(ulid.ULID())
                await self._on_inject_context(worker_id, injection_id, "answer", answer, question_id)
        else:
            # Low confidence — escalate
            await self.db.execute(
                "UPDATE worker_questions SET status = ? WHERE question_id = ?",
                ("escalated", question_id),
            )
            await self.db.conn.commit()
            await self.db.execute(
                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("worker.question_escalated", "worker", worker_id,
                 json.dumps({"question_id": question_id, "confidence": confidence}), now),
            )
            await self.db.conn.commit()

    async def _call_question_llm(self, bundle_id: str, question: str, context: str) -> tuple[str | None, str]:
        """Basic one-shot LLM call for question answering. Stub implementation for Bundle 5.1."""
        import asyncio
        try:
            # Fetch bundle proposal for context
            bundle_row = await self.db.fetch_one(
                "SELECT proposal_json FROM bundles WHERE id = ?", (bundle_id,)
            )
            proposal = json.loads(bundle_row["proposal_json"]) if bundle_row and bundle_row["proposal_json"] else {}

            # Build a simple prompt and attempt LLM call
            prompt = (
                f"You are a helpful coding assistant supervisor. A worker is executing a task and needs guidance.\n\n"
                f"Task context: {json.dumps(proposal.get('implementation_plan', proposal.get('requirements_summary', '')))}\n\n"
                f"Worker question: {question}\n\n"
                f"Additional context from worker: {context}\n\n"
                f"Provide a concise, helpful answer. If you cannot answer confidently, say 'UNCERTAIN'."
            )

            # Run in thread to avoid blocking
            result = await asyncio.to_thread(self._ollama_cloud_call, prompt)
            if result and "UNCERTAIN" not in result:
                return (result.strip(), "high")
            return (result.strip() if result else None, "medium")
        except Exception:
            return (None, "low")

    def _ollama_cloud_call(self, prompt: str) -> str | None:
        """Synchronous HTTP call to Ollama Cloud. Extracted for asyncio.to_thread."""
        import os
        import urllib.request
        api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
        if not api_key:
            return None
        try:
            req = urllib.request.Request(
                "https://ollama.com/api/chat",
                data=json.dumps({
                    "model": "llama3.2",
                    "messages": [{"role": "user", "content": prompt}],
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
                return body.get("message", {}).get("content", "")
        except Exception:
            return None

    # ── worker.report_checkpoint (Bundle 5.1) ─────────────────────────────

    async def handle_report_checkpoint(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        checkpoint_id = params.get("checkpoint_id", "")
        phase_completed = params.get("phase_completed", "")
        phase_starting = params.get("phase_starting", "")
        summary = params.get("summary", "")
        concerns = params.get("concerns", [])
        estimated_remaining = params.get("estimated_remaining", {})

        if not checkpoint_id:
            return {"accepted": False, "reason": "checkpoint_id is required"}

        now = self.now()
        await self.db.execute(
            "INSERT INTO worker_checkpoints (checkpoint_id, worker_id, bundle_id, phase_completed, "
            "phase_starting, summary, concerns_json, estimated_remaining_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (checkpoint_id, binding.worker_id, binding.bundle_id, phase_completed,
             phase_starting, summary, json.dumps(concerns), json.dumps(estimated_remaining), now),
        )
        await self.db.conn.commit()

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker.checkpoint_reported", "worker", binding.worker_id,
             json.dumps({"checkpoint_id": checkpoint_id, "phase_completed": phase_completed}), now),
        )
        await self.db.conn.commit()

        return {"accepted": True, "checkpoint_id": checkpoint_id}

    # ── worker.respond_to_query (Bundle 5.1) ──────────────────────────────

    async def handle_respond_to_query(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        injection_id = params.get("injection_id", "")
        response = params.get("response", {})

        if not injection_id:
            return {"accepted": False, "reason": "injection_id is required"}

        # The pending call is resolved by ConnectionManager via _pending_calls
        return {"accepted": True, "injection_id": injection_id}

    # ── worker.inject_context (Bundle 5.1 — promoted from stub) ───────────

    async def handle_inject_context(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        """Process inject_context acknowledgement from worker."""
        injection_id = params.get("injection_id", "")
        acknowledged = params.get("acknowledged", True)
        worker_response = params.get("worker_response", "")

        now = self.now()
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker.inject_context_ack", "worker", binding.worker_id,
             json.dumps({"injection_id": injection_id, "acknowledged": acknowledged,
                         "worker_response": worker_response[:500]}), now),
        )
        await self.db.conn.commit()

        return {"accepted": True, "injection_id": injection_id}


class ConnectionManager:
    """Unix domain socket server that accepts worker connections and dispatches RPC."""

    def __init__(
        self,
        socket_path: str,
        dispatcher: RpcDispatcher,
        handlers: RpcHandlers,
        db: "Database",
    ) -> None:
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self.handlers = handlers
        self.db = db
        self._bindings: dict[str, WorkerBinding] = {}
        self._by_worker_id: dict[str, WorkerBinding] = {}
        self._pending_calls: dict[str, asyncio.Future] = {}
        self._server: asyncio.AbstractServer | None = None

    @property
    def bindings(self) -> dict[str, WorkerBinding]:
        return self._bindings

    async def start(self) -> None:
        # Clean up stale socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=self.socket_path
        )
        # Set permissions
        os.chmod(self.socket_path, 0o660)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Close all worker connections
        for binding in list(self._by_worker_id.values()):
            try:
                binding.writer.close()
            except Exception:
                pass
        self._bindings.clear()
        self._by_worker_id.clear()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    async def send_to_worker(self, worker_id: str, message: dict) -> None:
        """Send a JSON-RPC message to a connected worker."""
        binding = self._by_worker_id.get(worker_id)
        if binding is None:
            raise ValueError(f"Worker {worker_id} not connected")
        data = (json.dumps(message) + "\n").encode()
        binding.writer.write(data)
        await binding.writer.drain()

    async def call_worker(self, worker_id: str, method: str, params: dict,
                           timeout: float = 30.0) -> dict | None:
        """Send a JSON-RPC request to a connected worker and await the response.

        Returns the result dict, or None on timeout.
        """
        import logging
        _logger = logging.getLogger(__name__)

        binding = self._by_worker_id.get(worker_id)
        if binding is None:
            raise ValueError(f"Worker {worker_id} not connected")

        corr_id = params.get("injection_id", "")
        if not corr_id:
            import ulid
            corr_id = str(ulid.ULID())

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_calls[corr_id] = future

        self._req_counter = getattr(self, '_req_counter', 0) + 1
        setattr(self, '_req_counter', self._req_counter)
        req_id = self._req_counter

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": req_id,
        }
        data = (json.dumps(message) + "\n").encode()
        binding.writer.write(data)
        await binding.writer.drain()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            _logger.warning("call_worker timeout for worker %s method %s (%.1fs)",
                            worker_id, method, timeout)
            self._pending_calls.pop(corr_id, None)
            return None

    async def _audit_security(self, event_type: str, subject_type: str,
                               subject_id: str, payload: dict) -> None:
        import time
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, subject_type, subject_id, json.dumps(payload), int(time.time())),
        )
        await self.db.conn.commit()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a new Unix socket connection — authenticate via token (worker) or role (system)."""
        binding: WorkerBinding | SystemBinding | None = None

        try:
            # First message must be the auth token
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line:
                return

            try:
                auth_msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                writer.write((json.dumps(_make_error(PARSE_ERROR, "Parse error", req_id=None)) + "\n").encode())
                await writer.drain()
                return

            method = auth_msg.get("method", "")
            auth_params = auth_msg.get("params", {})
            role = auth_params.get("role", "")
            token = auth_params.get("token", "")

            # ── MCP system auth: trusted by socket file permissions ──
            if method == "auth" and role == "mcp":
                binding = SystemBinding(role="mcp", reader=reader, writer=writer)
                writer.write((json.dumps(_make_result(
                    {"bound": True, "role": "mcp"}, auth_msg.get("id")
                )) + "\n").encode())
                await writer.drain()

                # Read loop — dispatch without capability checks
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    response = await self.dispatcher.dispatch(binding, line)
                    if response is not None:
                        writer.write(response)
                        await writer.drain()
                return

            if method != "auth" or not token:
                writer.write((json.dumps(_make_error(
                    INVALID_REQUEST, "First message must be auth with token", req_id=auth_msg.get("id")
                )) + "\n").encode())
                await writer.drain()
                return

            # Validate token and bind to worker
            row = await self.db.fetch_one(
                "SELECT id, bundle_id, node_id, token, token_expires_at, manifest_json FROM workers WHERE token = ?",
                (token,),
            )
            if row is None:
                await self._audit_security("worker_auth_failure", "worker", "",
                                          {"reason": "invalid_token", "token_prefix": token[:8]})
                writer.write((json.dumps(_make_error(
                    CAPABILITY_DENIED, "Invalid or expired worker token", req_id=auth_msg.get("id")
                )) + "\n").encode())
                await writer.drain()
                return

            # Check token expiry
            token_expires_at = row["token_expires_at"]
            if token_expires_at is not None and self.handlers.now() > token_expires_at:
                await self._audit_security("worker_auth_failure", "worker", row["id"],
                                          {"reason": "token_expired", "expires_at": token_expires_at})
                writer.write((json.dumps(_make_error(
                    CAPABILITY_DENIED, "Worker token expired", req_id=auth_msg.get("id")
                )) + "\n").encode())
                await writer.drain()
                return

            worker_id = row["id"]
            bundle_id = row["bundle_id"]
            node_id = row["node_id"]

            # Extract rpc methods from stored manifest
            rpc_methods: list[str] = ["worker.*"]
            if row["manifest_json"]:
                try:
                    mf = json.loads(row["manifest_json"])
                    rpc_methods = mf.get("grants", {}).get("rpc", {}).get("methods", ["worker.*"])
                except Exception:
                    pass

            binding = WorkerBinding(
                worker_id=worker_id,
                bundle_id=bundle_id,
                node_id=node_id,
                rpc_methods=rpc_methods,
                reader=reader,
                writer=writer,
            )

            self._bindings[f"{bundle_id}:{node_id}"] = binding
            self._by_worker_id[worker_id] = binding

            # Ack auth
            writer.write((json.dumps(_make_result({"bound": True, "worker_id": worker_id}, auth_msg.get("id"))) + "\n").encode())
            await writer.drain()

            # Read loop — process RPC messages; detect responses to pending calls
            while True:
                line = await reader.readline()
                if not line:
                    break

                # Check if this is a response to a pending orchestrator-initiated call
                try:
                    body = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response = await self.dispatcher.dispatch(binding, line)
                    if response is not None:
                        writer.write(response)
                        await writer.drain()
                    continue

                if "method" not in body and "id" in body:
                    # This is a response to an orchestrator-initiated request
                    # Try to match via worker.respond_to_query params
                    result = body.get("result", {})
                    injection_id = result.get("injection_id", "")
                    if not injection_id:
                        # Try matching by raw response — check all pending calls
                        pass
                    future = self._pending_calls.pop(injection_id, None)
                    if future is not None and not future.done():
                        future.set_result(body)
                    # Also check _pending_calls for any request id match
                    req_id = body.get("id")
                    if req_id is not None:
                        for key, fut in list(self._pending_calls.items()):
                            if not fut.done():
                                fut.set_result(body)
                                del self._pending_calls[key]
                                break
                    continue

                response = await self.dispatcher.dispatch(binding, line)
                if response is not None:
                    writer.write(response)
                    await writer.drain()

        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            if binding and isinstance(binding, WorkerBinding):
                self._bindings.pop(f"{binding.bundle_id}:{binding.node_id}", None)
                self._by_worker_id.pop(binding.worker_id, None)
            try:
                writer.close()
            except Exception:
                pass


# ── Factory ───────────────────────────────────────────────────────────────────

def create_rpc_system(
    db: "Database",
    socket_path: str,
    sm: Any = None,
) -> tuple[RpcDispatcher, RpcHandlers, ConnectionManager]:
    """Create and wire up the full RPC system.

    Returns (dispatcher, handlers, connection_manager).
    """
    handlers = RpcHandlers(db)
    if sm is not None:
        handlers.set_sm(sm)
    dispatcher = RpcDispatcher(db, sm)

    # Register all handlers
    dispatcher.register("worker.heartbeat", handlers.handle_heartbeat)
    dispatcher.register("worker.log", handlers.handle_log)
    dispatcher.register("worker.progress_report", handlers.handle_progress_report)
    dispatcher.register("worker.final_report", handlers.handle_final_report)
    dispatcher.register("worker.query_status", handlers.handle_query_status)
    dispatcher.register("cap.check", handlers.handle_cap_check)
    dispatcher.register("cap.request", handlers.handle_cap_request)
    dispatcher.register("artifact.publish", handlers.handle_artifact_publish)
    dispatcher.register("artifact.fetch", handlers.handle_artifact_fetch)
    dispatcher.register("artifact.list", handlers.handle_artifact_list)
    dispatcher.register("secrets.fetch", handlers.handle_secrets_fetch)
    # human input (implemented in Bundle 2.6)
    dispatcher.register("worker.request_human_input", handlers.handle_request_human_input)
    dispatcher.register("worker.poll_human_input", handlers.handle_poll_human_input)
    # Bundle 5.1: bidirectional introspection protocol
    dispatcher.register("worker.ask_question", handlers.handle_ask_question)
    dispatcher.register("worker.report_checkpoint", handlers.handle_report_checkpoint)
    dispatcher.register("worker.respond_to_query", handlers.handle_respond_to_query)
    dispatcher.register("worker.inject_context", handlers.handle_inject_context)
    # Stub methods also registered so dispatcher finds them (but they return -32000)
    dispatcher.register("worker.pause", _make_stub_handler("worker.pause"))
    dispatcher.register("worker.resume", _make_stub_handler("worker.resume"))
    dispatcher.register("worker.cancel", _make_stub_handler("worker.cancel"))
    # mcp.* handlers (system surface, called by MCP server process)
    dispatcher.register("mcp.approve_bundle", handlers.handle_mcp_approve_bundle)
    dispatcher.register("mcp.reject_bundle", handlers.handle_mcp_reject_bundle)
    dispatcher.register("mcp.request_modification", handlers.handle_mcp_request_modification)
    dispatcher.register("mcp.escalate_bundle", handlers.handle_mcp_escalate_bundle)
    dispatcher.register("mcp.pause_bundle", handlers.handle_mcp_pause_bundle)
    dispatcher.register("mcp.resume_bundle", handlers.handle_mcp_resume_bundle)
    dispatcher.register("mcp.kill_worker", handlers.handle_mcp_kill_worker)

    connection_manager = ConnectionManager(socket_path, dispatcher, handlers, db)
    return dispatcher, handlers, connection_manager


def _make_stub_handler(method: str) -> Handler:
    async def stub(_binding: WorkerBinding, _params: dict, _req_id: Any) -> dict:
        # Should never be reached — dispatcher intercepts stubs before handler call
        return {}
    return stub
