"""JSON-RPC 2.0 dispatcher and Unix domain socket connection manager.

14 Worker RPC methods: 5 full implementations, 9 stubbed (-32000).
Every method call is capability-checked against the worker's rpc.methods grant.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any, Callable, Awaitable

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
)

if TYPE_CHECKING:
    from .db import Database

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
    "worker.inject_context",
    "worker.prepare_handoff",
    "artifact.request",
    "worker.request_human_input",
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


# ── RPC Dispatcher ────────────────────────────────────────────────────────────

Handler = Callable[[WorkerBinding, dict[str, Any], Any], Awaitable[dict]]


class RpcDispatcher:
    """JSON-RPC 2.0 method dispatcher with capability enforcement."""

    def __init__(self, db: "Database", sm: Any = None) -> None:
        self.db = db
        self.sm = sm  # BundleStateMachine reference, set after construction
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    async def dispatch(
        self, binding: WorkerBinding, raw: bytes
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

        # ── Capability check ──
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

        # ── Stub check ──
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
    """Full and stub implementations for all 14 Worker RPC methods."""

    def __init__(self, db: "Database") -> None:
        self.db = db
        self._on_final_report: Callable[[str, str, str, dict], Awaitable[None]] | None = None
        self._on_heartbeat: Callable[[str, str], Awaitable[None]] | None = None
        self._on_cap_request: Callable[[str, str, dict], Awaitable[dict[str, Any]]] | None = None

    def set_on_final_report(self, cb: Callable[[str, str, str, dict], Awaitable[None]]) -> None:
        """Callback: on_final_report(bundle_id, node_id, worker_id, outcome)."""
        self._on_final_report = cb

    def set_on_heartbeat(self, cb: Callable[[str, str], Awaitable[None]]) -> None:
        """Callback: on_heartbeat(worker_id, phase). Called on every heartbeat."""
        self._on_heartbeat = cb

    def set_on_cap_request(self, cb: Callable[[str, str, dict], Awaitable[dict[str, Any]]]) -> None:
        """Callback: on_cap_request(bundle_id, node_id, request_params_dict)."""
        self._on_cap_request = cb

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

    # ── worker.final_report (full) ───────────────────────────────────────

    async def handle_final_report(self, binding: WorkerBinding, params: dict, req_id: Any) -> dict:
        outcome = params.get("outcome", "failure")
        files_changed = params.get("files_changed", [])
        tests_run = params.get("tests_run", 0)
        tests_passed = params.get("tests_passed", 0)
        tests_failed = params.get("tests_failed", 0)
        errors = params.get("errors", [])
        summary = params.get("summary", "")

        node_id = f"{binding.bundle_id}:{binding.node_id}"
        now = self.now()

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
            return {"allowed": False, "capability_id": None}

        # Use the pure check_op function — need to import the model
        from .models import CapabilityManifest
        try:
            manifest = CapabilityManifest.model_validate(binding.manifest_cache)
        except Exception:
            return {"allowed": False, "capability_id": None}

        allowed, _ = check_op(op_descriptor, manifest)
        return {"allowed": allowed, "capability_id": None}


# ── Connection manager ────────────────────────────────────────────────────────

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

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a new Unix socket connection — authenticate via token, then read/write RPC."""
        binding: WorkerBinding | None = None

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

            token = auth_msg.get("token", "")
            method = auth_msg.get("method", "")

            if method != "auth" or not token:
                writer.write((json.dumps(_make_error(
                    INVALID_REQUEST, "First message must be auth with token", req_id=auth_msg.get("id")
                )) + "\n").encode())
                await writer.drain()
                return

            # Validate token and bind to worker
            row = await self.db.fetch_one(
                "SELECT id, bundle_id, node_id, token, manifest_json FROM workers WHERE token = ?",
                (token,),
            )
            if row is None:
                writer.write((json.dumps(_make_error(
                    CAPABILITY_DENIED, "Invalid or expired worker token", req_id=auth_msg.get("id")
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

            # Read loop — process RPC messages
            while True:
                line = await reader.readline()
                if not line:
                    break

                response = await self.dispatcher.dispatch(binding, line)
                if response is not None:
                    writer.write(response)
                    await writer.drain()

        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            if binding:
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
    dispatcher = RpcDispatcher(db, sm)

    # Register all 14 handlers
    dispatcher.register("worker.heartbeat", handlers.handle_heartbeat)
    dispatcher.register("worker.log", handlers.handle_log)
    dispatcher.register("worker.progress_report", handlers.handle_progress_report)
    dispatcher.register("worker.final_report", handlers.handle_final_report)
    dispatcher.register("worker.query_status", handlers.handle_query_status)
    dispatcher.register("cap.check", handlers.handle_cap_check)
    # Stub methods also registered so dispatcher finds them (but they return -32000)
    dispatcher.register("worker.pause", _make_stub_handler("worker.pause"))
    dispatcher.register("worker.resume", _make_stub_handler("worker.resume"))
    dispatcher.register("worker.cancel", _make_stub_handler("worker.cancel"))
    dispatcher.register("worker.inject_context", _make_stub_handler("worker.inject_context"))
    dispatcher.register("worker.prepare_handoff", _make_stub_handler("worker.prepare_handoff"))
    dispatcher.register("cap.request", handlers.handle_cap_request)
    dispatcher.register("artifact.request", _make_stub_handler("artifact.request"))
    dispatcher.register("worker.request_human_input", _make_stub_handler("worker.request_human_input"))

    connection_manager = ConnectionManager(socket_path, dispatcher, handlers, db)
    return dispatcher, handlers, connection_manager


def _make_stub_handler(method: str) -> Handler:
    async def stub(_binding: WorkerBinding, _params: dict, _req_id: Any) -> dict:
        # Should never be reached — dispatcher intercepts stubs before handler call
        return {}
    return stub
