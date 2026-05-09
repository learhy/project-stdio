"""Phase 1 developer worker: connects to orchestrator, executes task, reports results.

Invokes an external coding agent (default: studio-code) inside the bubblewrap
container. Communicates with the orchestrator over JSON-RPC via Unix socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from typing import Any

# ── Configuration from environment ────────────────────────────────────────────

_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")
_SOCKET_PATH = os.environ.get("STUDIO_SOCKET_PATH", "/run/studio/orchestrator.sock")
_WORKER_ID = os.environ.get("STUDIO_WORKER_ID", "unknown")
_BUNDLE_ID = os.environ.get("STUDIO_BUNDLE_ID", "unknown")
_NODE_ID = os.environ.get("STUDIO_NODE_ID", "unknown")
_TASK_SPEC_RAW = os.environ.get("STUDIO_TASK_SPEC", "{}")
_HEARTBEAT_INTERVAL = float(os.environ.get("STUDIO_HEARTBEAT_INTERVAL", "30"))
_AGENT_COMMAND = os.environ.get("STUDIO_AGENT_COMMAND", "echo 'worker stub: no agent configured'")


def _now() -> int:
    return int(time.time())


def _load_task_spec() -> dict[str, Any]:
    try:
        return json.loads(_TASK_SPEC_RAW)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

class RpcClient:
    """Minimal JSON-RPC 2.0 client over a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._req_id = 0

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._req_id,
        }
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

        line = await self.reader.readline()
        if not line:
            return {"error": {"code": -1, "message": "Connection closed"}}

        return json.loads(line.decode("utf-8"))

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()


# ── Worker ────────────────────────────────────────────────────────────────────

class Worker:
    """Phase 1 developer worker. Connects, heartbeats, executes, reports."""

    def __init__(self) -> None:
        self.rpc = RpcClient(_SOCKET_PATH)
        self.task_spec = _load_task_spec()
        self._heartbeat_task: asyncio.Task | None = None
        self._agent_process: asyncio.subprocess.Process | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def run(self) -> int:
        """Main entry point. Returns exit code (0 = success, 1 = failure)."""
        if not _TOKEN:
            self._log("ERROR: STUDIO_WORKER_TOKEN not set — cannot authenticate")
            return 1

        try:
            await self.rpc.connect()
        except Exception as exc:
            self._log(f"ERROR: cannot connect to orchestrator at {_SOCKET_PATH}: {exc}")
            return 1

        # Authenticate
        auth_resp = await self.rpc.call("auth", {"token": _TOKEN})
        if "error" in auth_resp:
            self._log(f"ERROR: auth failed: {auth_resp['error'].get('message', auth_resp['error'])}")
            await self.rpc.close()
            return 1

        bound = auth_resp.get("result", {}).get("bound", False)
        if not bound:
            self._log("ERROR: auth rejected — token not bound to any worker")
            await self.rpc.close()
            return 1

        self._log(f"Authenticated as worker {_WORKER_ID} (node {_NODE_ID}, bundle {_BUNDLE_ID})")
        self._running = True

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            objective = self.task_spec.get("objective", self.task_spec.get("spec", "execute task"))
            outcome = await self._execute_task(objective)
        except Exception as exc:
            outcome = {"outcome": "failure", "errors": [str(exc)], "summary": f"Worker crashed: {exc}"}
        finally:
            self._running = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass

        # Send final report
        final_params = {
            "outcome": outcome.get("outcome", "failure"),
            "files_changed": outcome.get("files_changed", []),
            "tests_run": outcome.get("tests_run", 0),
            "tests_passed": outcome.get("tests_passed", 0),
            "tests_failed": outcome.get("tests_failed", 0),
            "errors": outcome.get("errors", []),
            "summary": outcome.get("summary", ""),
        }

        await self.rpc.call("worker.final_report", final_params)
        self._log(f"Final report sent: {outcome.get('outcome', 'failure')}")

        await self.rpc.close()
        return 0 if outcome.get("outcome") == "success" else 1

    # ── Heartbeat ──────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats every _HEARTBEAT_INTERVAL seconds."""
        phase = "starting"
        while self._running:
            try:
                resp = await self.rpc.call("worker.heartbeat", {
                    "phase": phase,
                    "progress": "",
                    "current_step": None,
                    "estimated_completion_seconds": None,
                })
                if "result" in resp:
                    phase = "writing-code"
            except Exception:
                pass
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── Task execution ─────────────────────────────────────────────────────

    async def _execute_task(self, objective: str) -> dict:
        """Run the agent command and return outcome dict."""
        self._log(f"Task objective: {objective}")
        await self.rpc.notify("worker.progress_report", {
            "stage": "starting",
            "percent": 0,
            "message": f"Starting task: {objective[:100]}",
        })

        # Build command — split _AGENT_COMMAND by spaces, appending the objective
        agent_cmd = _AGENT_COMMAND.split()
        full_cmd = [*agent_cmd, objective] if "{}" not in _AGENT_COMMAND else []

        # Handle {} substitution
        if "{}" in _AGENT_COMMAND:
            full_cmd = [p.replace("{}", objective) for p in agent_cmd]

        self._log(f"Running: {' '.join(full_cmd)}")

        try:
            self._agent_process = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Stream stdout and stderr as log messages
            await self.rpc.notify("worker.progress_report", {
                "stage": "running",
                "percent": 50,
                "message": "Agent is working...",
            })

            stdout, stderr = await asyncio.wait_for(
                self._agent_process.communicate(), timeout=3600
            )

            returncode = self._agent_process.returncode

            if stdout:
                for line in stdout.decode("utf-8", errors="replace").splitlines()[:20]:
                    await self.rpc.notify("worker.log", {
                        "level": "info",
                        "message": line[:500],
                    })

            if stderr:
                for line in stderr.decode("utf-8", errors="replace").splitlines()[:10]:
                    await self.rpc.notify("worker.log", {
                        "level": "warn" if returncode == 0 else "error",
                        "message": line[:500],
                    })

            if returncode == 0:
                return {
                    "outcome": "success",
                    "summary": f"Agent completed successfully for: {objective[:200]}",
                    "files_changed": [],
                    "errors": [],
                }
            else:
                return {
                    "outcome": "failure",
                    "summary": f"Agent exited with code {returncode}",
                    "errors": [f"exit_code={returncode}"],
                }

        except asyncio.TimeoutError:
            if self._agent_process:
                self._agent_process.kill()
                await self._agent_process.wait()
            return {
                "outcome": "timeout",
                "summary": "Agent exceeded time limit",
                "errors": ["timeout after 3600s"],
            }
        except Exception as exc:
            return {
                "outcome": "failure",
                "summary": f"Agent execution error: {exc}",
                "errors": [str(exc)],
            }

    # ── Internal logging ───────────────────────────────────────────────────

    @staticmethod
    def _log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    worker = Worker()
    exit_code = asyncio.run(worker.run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
