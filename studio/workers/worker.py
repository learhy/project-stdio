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

from .client import RpcClient, get_orchestrator_addr_display

# ── Configuration from environment ────────────────────────────────────────────

_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")
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

# ── Worker ────────────────────────────────────────────────────────────────────

class Worker:
    """Phase 1 developer worker. Connects, heartbeats, executes, reports."""

    def __init__(self) -> None:
        self.rpc = RpcClient()
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
            addr_display = get_orchestrator_addr_display()
            self._log(f"ERROR: cannot connect to orchestrator at {addr_display}: {exc}")
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
                await self.rpc.notify("worker.heartbeat", {
                    "phase": phase,
                    "progress": "",
                    "current_step": None,
                    "estimated_completion_seconds": None,
                })
                phase = "writing-code"
            except Exception:
                pass

            # Check for incoming messages from orchestrator
            try:
                msg = await self.rpc.receive(timeout=0.1)
                if msg is None:
                    pass
                elif msg.get("method") == "worker.inject_context":
                    await self._handle_inject_context(msg)
                elif msg.get("method") == "worker.describe_progress":
                    result = await self._handle_describe_progress(msg.get("params", {}))
                    if msg.get("id") is not None:
                        await self.rpc.respond(msg["id"], result)
                elif msg.get("method") == "worker.show_artifact":
                    result = await self._handle_show_artifact(msg.get("params", {}))
                    if msg.get("id") is not None:
                        await self.rpc.respond(msg["id"], result)
            except Exception:
                pass

            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── Describe progress handler (Bundle 5.2) ────────────────────────────

    async def _handle_describe_progress(self, params: dict) -> dict:
        """Return structured current state snapshot for the orchestrator."""
        agent_running = self._agent_process is not None and self._agent_process.returncode is None
        return {
            "current_activity": "executing task" if agent_running else "idle",
            "completed_steps": getattr(self, '_completed_steps', []),
            "planned_steps": getattr(self, '_planned_steps', []),
            "blockers": getattr(self, '_blockers', []),
            "confidence": "medium",
            "recent_tool_calls": getattr(self, '_recent_tool_calls', [])[-5:],
            "agent_running": agent_running,
            "objective": self.task_spec.get("objective", "")[:200],
        }

    # ── Show artifact handler (Bundle 5.2) ────────────────────────────────

    async def _handle_show_artifact(self, params: dict) -> dict:
        """Return the current contents of a file within the worktree."""
        path = params.get("path", "")
        if not path:
            return {"error": "path is required"}

        import os
        cwd = os.getcwd()
        full_path = os.path.join(cwd, path)

        # Security: refuse absolute paths and path traversal
        if os.path.isabs(path) or ".." in path.split(os.sep):
            return {"path": path, "error": "path traversal denied"}

        try:
            st = os.stat(full_path)
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {
                "path": path,
                "content": content[:100_000],
                "size_bytes": st.st_size,
                "last_modified": int(st.st_mtime),
            }
        except FileNotFoundError:
            return {"path": path, "error": "file not found"}
        except Exception as exc:
            return {"path": path, "error": str(exc)}

    # ── Inject context handler (Bundle 5.1) ───────────────────────────────

    async def _handle_inject_context(self, msg: dict) -> None:
        """Handle an inject_context message from the orchestrator."""
        params = msg.get("params", {})
        injection_id = params.get("injection_id", "")
        context_type = params.get("type", "feedback")
        content = params.get("content", "")
        action = params.get("action")
        action_path = params.get("action_path")

        self._log(f"Received inject_context: {context_type} (injection_id={injection_id})")

        worker_response = "acknowledged"

        # Handle embedded queries
        if action == "describe_progress":
            worker_response = json.dumps({
                "current_activity": "executing task",
                "completed_steps": [],
                "planned_steps": [],
                "blockers": [],
                "confidence": "medium",
                "recent_tool_calls": [],
            })
        elif action == "show_artifact" and action_path:
            worker_response = json.dumps({
                "path": action_path,
                "content": "",
                "size_bytes": 0,
                "error": "not yet implemented",
            })

        # Acknowledge via worker.respond_to_query
        try:
            await self.rpc.call("worker.respond_to_query", {
                "injection_id": injection_id,
                "query_type": action or "describe_progress",
                "response": {"worker_response": worker_response},
            })
        except Exception:
            pass

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
