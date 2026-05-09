"""Orchestrator entry point: wires all components and starts the event loop.

Single Unix domain socket serves both worker connections (persistent,
token-authenticated) and CLI/admin requests (one-shot JSON-RPC).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .db import Database, create_database
from .state_machine import BundleStateMachine
from .rpc import (
    RpcDispatcher,
    RpcHandlers,
    ConnectionManager,
    WorkerBinding,
    create_rpc_system,
    _make_error,
    _make_result,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    CAPABILITY_DENIED,
    INTERNAL_ERROR,
)
from .runner import LocalBwrapWorkerRunner
from .executor import LinearDagExecutor
from .scheduler import Scheduler
from .reconciler import Reconciler
from .models import Settings, OrchestratorSettings

logger = logging.getLogger(__name__)


class Orchestrator:
    """Top-level application that owns every subsystem."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.db: Database | None = None
        self.sm: BundleStateMachine | None = None
        self.dispatcher: RpcDispatcher | None = None
        self.handlers: RpcHandlers | None = None
        self.conn_mgr: ConnectionManager | None = None
        self.runner: LocalBwrapWorkerRunner | None = None
        self.executor: LinearDagExecutor | None = None
        self.scheduler: Scheduler | None = None
        self.reconciler: Reconciler | None = None
        self._server: asyncio.AbstractServer | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all subsystems, recover state, and begin serving."""
        cfg = self.settings.orchestrator

        # 1. Database
        self.db = await create_database(cfg.db_path)

        # 2. State machine (kernel mode — Phase 1 approves/rejects directly)
        self.sm = BundleStateMachine(self.db, kernel_mode=True)

        # 3. RPC system
        self.dispatcher, self.handlers, self.conn_mgr = create_rpc_system(
            self.db, cfg.socket_path, self.sm
        )

        # 4. Worker runner
        self.runner = LocalBwrapWorkerRunner(
            self.db,
            cfg.socket_path,
            network_isolation=self.settings.kernel.network_isolation,
        )

        # 5. Executor
        self.executor = LinearDagExecutor(
            self.db,
            self.sm,
            self.runner,
            self.handlers,
            self.conn_mgr,
            global_concurrency=self.settings.worker.global_concurrency,
            heartbeat_timeout_multiplier=self.settings.worker.heartbeat_timeout_multiplier,
        )

        # 6. Scheduler
        self.scheduler = Scheduler(
            self.db,
            self.executor,
            dispatch_interval=1.0,
            heartbeat_check_interval=float(
                self.settings.worker.heartbeat_max_interval_minutes * 60
            ),
        )

        # 7. Reconciler
        self.reconciler = Reconciler(self.db, self.sm, self.executor)

        # 8. Crash recovery (idempotent)
        counts = await self.reconciler.reconcile()
        logger.info("Reconciliation complete: %s", counts)

        # 9. Start periodic loops
        await self.scheduler.start()
        logger.info("Scheduler started")

        # 10. Bind socket (single socket for workers + CLI)
        socket_path = cfg.socket_path
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=socket_path
        )
        os.chmod(socket_path, 0o660)
        self._running = True
        logger.info("Orchestrator listening on %s", socket_path)

    async def stop(self) -> None:
        """Graceful shutdown: stop accepting, drain loops, close DB."""
        self._running = False
        logger.info("Shutting down...")

        if self.scheduler:
            await self.scheduler.stop()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Close lingering worker connections
        if self.conn_mgr:
            for binding in list(self.conn_mgr._by_worker_id.values()):
                try:
                    binding.writer.close()
                except Exception:
                    pass

        if self.db:
            await self.db.close()

        logger.info("Orchestrator stopped")

    # ── Connection dispatch ────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Route a new connection based on its first message.

        - "auth"  → persistent worker session
        - "studio.*" → one-shot CLI request
        """
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line:
                return

            try:
                body = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                writer.write(
                    (json.dumps(_make_error(PARSE_ERROR, "Parse error")) + "\n").encode()
                )
                await writer.drain()
                return

            method = body.get("method", "")

            if method == "auth":
                await self._serve_worker(reader, writer, body)
            elif method.startswith("studio."):
                await self._serve_cli(writer, body)
            else:
                writer.write(
                    (
                        json.dumps(
                            _make_error(
                                INVALID_REQUEST,
                                "First message must be auth or studio.* method",
                                req_id=body.get("id"),
                            )
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ── Worker session ─────────────────────────────────────────────────────

    async def _serve_worker(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        auth_body: dict,
    ) -> None:
        """Authenticate a worker, then pump RPC messages until disconnect."""
        token = auth_body.get("token", "")
        req_id = auth_body.get("id")

        if not token:
            writer.write(
                (
                    json.dumps(
                        _make_error(
                            INVALID_REQUEST,
                            "First message must be auth with token",
                            req_id=req_id,
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            return

        row = await self.db.fetch_one(
            "SELECT id, bundle_id, node_id, token, manifest_json FROM workers WHERE token = ?",
            (token,),
        )
        if row is None:
            writer.write(
                (
                    json.dumps(
                        _make_error(
                            CAPABILITY_DENIED,
                            "Invalid or expired worker token",
                            req_id=req_id,
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            return

        worker_id = row["id"]
        bundle_id = row["bundle_id"]
        node_id = row["node_id"]

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

        self.conn_mgr._bindings[f"{bundle_id}:{node_id}"] = binding
        self.conn_mgr._by_worker_id[worker_id] = binding

        writer.write(
            (
                json.dumps(
                    _make_result({"bound": True, "worker_id": worker_id}, req_id)
                )
                + "\n"
            ).encode()
        )
        await writer.drain()

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self.dispatcher.dispatch(binding, line)
                if response is not None:
                    writer.write(response)
                    await writer.drain()
        except Exception:
            pass
        finally:
            self.conn_mgr._bindings.pop(f"{bundle_id}:{node_id}", None)
            self.conn_mgr._by_worker_id.pop(worker_id, None)

    # ── CLI request ────────────────────────────────────────────────────────

    async def _serve_cli(self, writer: asyncio.StreamWriter, body: dict) -> None:
        """Handle a one-shot studio.* JSON-RPC request."""
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        handler = _CLI_HANDLERS.get(method)
        if handler is None:
            resp = _make_error(METHOD_NOT_FOUND, f"Method not found: {method}", req_id=req_id)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            return

        try:
            result = await handler(self, params)
            resp = _make_result(result if result is not None else {}, req_id)
        except Exception as exc:
            resp = _make_error(INTERNAL_ERROR, str(exc), req_id=req_id)

        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()


# ── CLI handler implementations ────────────────────────────────────────────────

async def _cli_submit(app: Orchestrator, params: dict) -> dict:
    submission = params.get("submission", {})
    bundle_input = submission.get("bundle_input", {})
    task_dag = submission.get("task_dag", {})
    repo = bundle_input.get("target_repo", "control-plane")

    from ulid import ULID
    bundle_id = str(ULID())

    dag_nodes = []
    for n in task_dag.get("nodes", []):
        dag_nodes.append({
            "node_id": n.get("id", "task-1"),
            "kind": n.get("kind", "worker"),
            "spec": n.get("spec", {}),
        })

    dag_edges = []
    for e in task_dag.get("edges", []):
        dag_edges.append({
            "from_node_id": e.get("from", ""),
            "to_node_id": e.get("to", ""),
            "condition": e.get("condition", {"kind": "on_success"}),
        })

    await app.sm.transition_1_submit(bundle_id, repo, submission, dag_nodes, dag_edges)
    return {"bundle_id": bundle_id}


async def _cli_approve(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    await app.sm.transition_1a_approve(bundle_id, "cli")

    # Transition 6: start execution
    await app.sm.transition_6_start_execution(bundle_id)
    await app.executor.start_bundle(bundle_id)

    return {"approved": True}


async def _cli_reject(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    reason = params.get("reason", "rejected via CLI")
    await app.sm.transition_1b_reject(bundle_id, "cli", reason)
    return {"rejected": True}


async def _cli_list(app: Orchestrator, params: dict) -> dict:
    state = params.get("state")
    if state:
        rows = await app.db.fetch_all(
            "SELECT id, state, created_at, proposal_json FROM bundles WHERE state = ?",
            (state,),
        )
    else:
        rows = await app.db.fetch_all(
            "SELECT id, state, created_at, proposal_json FROM bundles WHERE state NOT IN (?, ?, ?, ?, ?)",
            ("complete", "failed", "rejected", "parked", "aborted"),
        )

    bundles = []
    for r in rows:
        secs = app.sm.now() - (r["created_at"] or 0)
        age = _format_age(secs)
        proposal = json.loads(r["proposal_json"] or "{}")
        bundles.append({
            "id": r["id"],
            "state": r["state"],
            "age": age,
            "idea": proposal.get("bundle_input", {}).get("idea", ""),
        })
    return {"bundles": bundles}


async def _cli_show(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    row = await app.db.fetch_one(
        "SELECT id, state, proposal_json FROM bundles WHERE id = ?", (bundle_id,)
    )
    if row is None:
        return {"error": f"Bundle {bundle_id} not found"}

    proposal = json.loads(row["proposal_json"] or "{}")
    nodes = await app.db.fetch_all(
        "SELECT id, node_id, kind, state FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
    )

    return {
        "bundle_id": row["id"],
        "state": row["state"],
        "idea": proposal.get("bundle_input", {}).get("idea", ""),
        "nodes": [dict(n) for n in nodes],
    }


async def _cli_show_worker(app: Orchestrator, params: dict) -> dict:
    worker_id = params.get("worker_id", "")
    row = await app.db.fetch_one(
        "SELECT id, bundle_id, node_id, state, current_phase, last_heartbeat FROM workers WHERE id = ?",
        (worker_id,),
    )
    if row is None:
        return {"error": f"Worker {worker_id} not found"}

    heartbeat_ago = ""
    if row["last_heartbeat"]:
        secs = app.sm.now() - row["last_heartbeat"]
        heartbeat_ago = _format_age(secs)

    logs = await app.db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE subject_id = ? AND event_type LIKE 'worker.log.%' ORDER BY id DESC LIMIT 20",
        (worker_id,),
    )

    recent_logs = []
    for l in logs:
        try:
            payload = json.loads(l["payload_json"] or "{}")
            recent_logs.append({"level": "info", "message": payload.get("message", "")})
        except Exception:
            pass

    return {
        "worker_id": row["id"],
        "bundle_id": row["bundle_id"],
        "state": row["state"],
        "phase": row["current_phase"] or "unknown",
        "last_heartbeat_ago": heartbeat_ago,
        "recent_logs": recent_logs,
    }


async def _cli_kill(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    workers = await app.db.fetch_all(
        "SELECT id FROM workers WHERE bundle_id = ? AND state = ?",
        (bundle_id, "running"),
    )
    for w in workers:
        proc = app.executor._running_workers.pop(w["id"], None)
        if proc and proc.returncode is None:
            await app.runner.kill_worker(proc)

    await app.sm.transition_25_fail_execution(bundle_id, "killed via CLI")
    return {"workers_killed": len(workers)}


async def _cli_status(app: Orchestrator, params: dict) -> dict:
    bundles = await app.db.fetch_all(
        "SELECT id, state, proposal_json FROM bundles WHERE state NOT IN (?, ?, ?, ?, ?)",
        ("complete", "failed", "rejected", "parked", "aborted"),
    )
    return {
        "uptime": 0,  # Phase 1: not tracking precise uptime
        "bundles": [
            {
                "id": b["id"],
                "state": b["state"],
                "idea": json.loads(b["proposal_json"] or "{}").get("bundle_input", {}).get("idea", ""),
            }
            for b in bundles
        ],
    }


_CLI_HANDLERS = {
    "studio.submit": _cli_submit,
    "studio.approve": _cli_approve,
    "studio.reject": _cli_reject,
    "studio.list": _cli_list,
    "studio.show": _cli_show,
    "studio.show_worker": _cli_show_worker,
    "studio.kill": _cli_kill,
    "studio.status": _cli_status,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = Settings()
    app = Orchestrator(settings)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        await app.start()

        stop_event = asyncio.Event()

        def _on_signal(signum, frame):
            logger.info("Received signal %s", signum)
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, _on_signal, signal.SIGTERM, None)
        loop.add_signal_handler(signal.SIGINT, _on_signal, signal.SIGINT, None)

        await stop_event.wait()
        await app.stop()

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
