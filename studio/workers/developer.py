"""Developer worker: real implementation invoking OpenCode CLI inside a git worktree.

Replaces the Phase 1 stub. Reads task spec (objective, gates, model config),
manages a git worktree, runs opencode in headless mode, detects stuck iterations,
commits results, and reports via worker.final_report.

Communicates with the orchestrator over JSON-RPC via Unix socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any


# ── Configuration from environment ────────────────────────────────────────────

_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")
_WORKER_ID = os.environ.get("STUDIO_WORKER_ID", "unknown")
_BUNDLE_ID = os.environ.get("STUDIO_BUNDLE_ID", "unknown")
_NODE_ID = os.environ.get("STUDIO_NODE_ID", "unknown")
_TASK_SPEC_RAW = os.environ.get("STUDIO_TASK_SPEC", "{}")
_HEARTBEAT_INTERVAL = float(os.environ.get("STUDIO_HEARTBEAT_INTERVAL", "30"))
_WORKTREE_PATH = os.environ.get("STUDIO_WORKTREE_PATH", "")
_BASE_BRANCH = os.environ.get("STUDIO_BASE_BRANCH", "main")
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api")

_STUCK_WINDOW = 20
_STUCK_THRESHOLD = 3
_HUMAN_INPUT_POLL_INTERVAL = 30
_OPencode_BIN = "opencode"

GIT_AUTHOR_NAME = "studio-agents[bot]"
GIT_AUTHOR_EMAIL = "studio-agents@learhy.net"


def _now() -> int:
    return int(time.time())


def _load_task_spec() -> dict[str, Any]:
    try:
        return json.loads(_TASK_SPEC_RAW)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

from .client import RpcClient, get_orchestrator_addr_display


# ── Stuck detection ───────────────────────────────────────────────────────────

def _hash_lines(lines: list[str]) -> str:
    return hashlib.sha256("".join(lines).encode()).hexdigest()


# ── Developer Worker ──────────────────────────────────────────────────────────

class DeveloperWorker:
    """Real developer worker. Connects, heartbeats, invokes OpenCode, reports."""

    def __init__(self) -> None:
        self.rpc = RpcClient()
        self.task_spec = _load_task_spec()
        self._heartbeat_task: asyncio.Task | None = None
        self._agent_process: asyncio.subprocess.Process | None = None
        self._running = False
        self._current_phase = "starting"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def run(self) -> int:
        if not _TOKEN:
            self._log("ERROR: STUDIO_WORKER_TOKEN not set")
            return 1

        if not _WORKTREE_PATH:
            self._log("ERROR: STUDIO_WORKTREE_PATH not set")
            return 1

        if not shutil.which(_OPencode_BIN):
            self._log(f"ERROR: opencode CLI not found — expected binary: {_OPencode_BIN}")
            return 1

        try:
            await self.rpc.connect()
        except Exception as exc:
            addr_display = get_orchestrator_addr_display()
            self._log(f"ERROR: cannot connect to orchestrator at {addr_display}: {exc}")
            return 1

        auth_resp = await self.rpc.call("auth", {"token": _TOKEN})
        if "error" in auth_resp:
            self._log(f"ERROR: auth failed: {auth_resp['error'].get('message', auth_resp['error'])}")
            await self.rpc.close()
            return 1

        if not auth_resp.get("result", {}).get("bound", False):
            self._log("ERROR: auth rejected")
            await self.rpc.close()
            return 1

        self._log(f"Authenticated as worker {_WORKER_ID} (node {_NODE_ID}, bundle {_BUNDLE_ID})")
        self._running = True

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            outcome = await self._execute_task()
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

        # Collect files_changed via git diff
        files_changed = await self._get_files_changed()

        final_params = {
            "outcome": outcome.get("outcome", "failure"),
            "files_changed": files_changed,
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
        while self._running:
            try:
                await self.rpc.call("worker.heartbeat", {
                    "phase": self._current_phase,
                    "progress": "",
                    "current_step": None,
                    "estimated_completion_seconds": None,
                })
            except Exception:
                pass
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── Task execution ─────────────────────────────────────────────────────

    async def _execute_task(self) -> dict:
        objective = self.task_spec.get("objective", self.task_spec.get("idea", "execute task"))
        model = self.task_spec.get("model", "kimi-k2.6:cloud")
        gates: list[str] = self.task_spec.get("gates", [])

        # Set up git identity in worktree
        self._log(f"Worktree: {_WORKTREE_PATH}, base branch: {_BASE_BRANCH}")
        self._setup_git_identity()

        self._log(f"Task objective: {objective[:200]}")
        await self.rpc.notify("worker.progress_report", {
            "stage": "starting",
            "percent": 0,
            "message": f"Starting task: {objective[:100]}",
        })

        # Build opencode command
        cmd = [_OPencode_BIN, "run", "--headless", "--model", model, objective]
        self._log(f"Running: {' '.join(cmd[:4])} ...")

        try:
            self._agent_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=_WORKTREE_PATH,
                env={**os.environ, "OLLAMA_CLOUD_BASE_URL": _OLLAMA_BASE_URL},
            )

            await self.rpc.notify("worker.progress_report", {
                "stage": "running",
                "percent": 50,
                "message": "OpenCode is working...",
            })

            # Stream stdout and detect stuckness
            stdout_lines, stderr_data, stuck = await self._stream_and_detect_stuck()

            returncode = await self._agent_process.wait()

            if stuck:
                self._log("OpenCode appears stuck — requesting human input")
                human_response = await self._request_human_input(
                    f"OpenCode appears stuck on task: {objective[:200]}",
                    "\n".join(stdout_lines[-20:]),
                )
                if human_response:
                    self._log(f"Human response received: {human_response[:200]}")
                    # Resume with context — for v1, commit WIP and report
                    await self._commit_worktree(objective, failed=True)
                    return {
                        "outcome": "failure",
                        "summary": f"Stuck — human responded: {human_response[:200]}",
                        "errors": ["stuck-after-human-input"],
                    }
                else:
                    # No human response after timeout — fail
                    await self._commit_worktree(objective, failed=True)
                    return {
                        "outcome": "failure",
                        "summary": "Stuck — no human response within poll window",
                        "errors": ["stuck-no-human-response"],
                    }

            # Log output
            for line in stdout_lines[-30:]:
                await self.rpc.notify("worker.log", {
                    "level": "info",
                    "message": line[:500],
                })

            if stderr_data:
                stderr_text = stderr_data.decode("utf-8", errors="replace")
                for line in stderr_text.splitlines()[-10:]:
                    await self.rpc.notify("worker.log", {
                        "level": "warn" if returncode == 0 else "error",
                        "message": line[:500],
                    })

            if returncode == 0:
                # Run pre-merge gates
                gate_result = await self._run_gates(gates)
                if not gate_result["passed"]:
                    await self._commit_worktree(objective, failed=True)
                    return {
                        "outcome": "failure",
                        "summary": f"Pre-merge gates failed: {gate_result['failed_gate']}",
                        "errors": [gate_result["output"]],
                        "tests_run": len(gates),
                        "tests_failed": 1,
                    }

                await self._commit_worktree(objective, failed=False)
                return {
                    "outcome": "success",
                    "summary": f"Task completed: {objective[:200]}",
                    "tests_run": len(gates),
                    "tests_passed": len(gates),
                }
            else:
                await self._commit_worktree(objective, failed=True)
                return {
                    "outcome": "failure",
                    "summary": f"OpenCode exited with code {returncode}",
                    "errors": [f"exit_code={returncode}"],
                }

        except asyncio.TimeoutError:
            if self._agent_process:
                self._agent_process.kill()
                await self._agent_process.wait()
            await self._commit_worktree(objective, failed=True)
            return {
                "outcome": "timeout",
                "summary": "OpenCode exceeded time limit",
                "errors": ["timeout"],
            }
        except Exception as exc:
            await self._commit_worktree(objective, failed=True)
            return {
                "outcome": "failure",
                "summary": f"Execution error: {exc}",
                "errors": [str(exc)],
            }

    async def _stream_and_detect_stuck(self) -> tuple[list[str], bytes, bool]:
        """Stream stdout from opencode, tracking rolling hash for stuck detection.

        Returns (all_lines, stderr_data, stuck_detected).
        """
        stdout_lines: list[str] = []
        stderr_data = b""
        stuck_count = 0
        last_hash = ""
        stuck = False

        async def _read_stderr() -> bytes:
            try:
                return await self._agent_process.stderr.read()
            except Exception:
                return b""

        stderr_task = asyncio.create_task(_read_stderr())

        try:
            while self._agent_process.returncode is None:
                line = await self._agent_process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                stdout_lines.append(decoded)

                if len(stdout_lines) >= _STUCK_WINDOW:
                    window = stdout_lines[-_STUCK_WINDOW:]
                    h = _hash_lines(window)
                    if h == last_hash:
                        stuck_count += 1
                        if stuck_count >= _STUCK_THRESHOLD:
                            stuck = True
                            # Kill opencode so we can surface the stuck state
                            if self._agent_process.returncode is None:
                                self._agent_process.kill()
                            break
                    else:
                        stuck_count = 0
                    last_hash = h

                # Emit phase based on output content
                self._update_phase_from_output(decoded)

        except Exception:
            pass

        # Collect stderr (with timeout — killed processes may hang on buffer drain)
        try:
            stderr_data = await asyncio.wait_for(stderr_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()
            stderr_data = b""

        return stdout_lines, stderr_data, stuck

    def _update_phase_from_output(self, line: str) -> None:
        """Infer worker phase from opencode output content."""
        lower = line.lower()
        if any(kw in lower for kw in ("thinking", "reasoning", "analyzing")):
            self._current_phase = "thinking"
        elif any(kw in lower for kw in ("tool call", "executing", "running command")):
            self._current_phase = "tool-call"
        elif any(kw in lower for kw in ("writing", "creating file", "modifying", "editing")):
            self._current_phase = "writing-code"
        elif any(kw in lower for kw in ("test", "pytest", "cargo test", "go test", "npm test")):
            self._current_phase = "running-tests"

    # ── Human input ────────────────────────────────────────────────────────

    async def _request_human_input(self, question: str, context: str) -> str | None:
        """Call worker.request_human_input and poll for response."""
        resp = await self.rpc.call("worker.request_human_input", {
            "question": question,
            "context": context,
            "options": None,
        })
        if "error" in resp:
            self._log(f"human_input request failed: {resp['error']}")
            return None

        request_id = resp.get("result", {}).get("request_id", "")
        if not request_id:
            return None

        self._log(f"Human input requested: {request_id}")

        poll_interval = getattr(self, "_human_input_poll_interval", _HUMAN_INPUT_POLL_INTERVAL)
        poll_count = getattr(self, "_human_input_poll_count", 20)

        for _ in range(poll_count):
            await asyncio.sleep(poll_interval)
            poll = await self.rpc.call("worker.poll_human_input", {
                "request_id": request_id,
            })
            result = poll.get("result", {})
            if result.get("pending") is False and result.get("response"):
                return result["response"]

        return None

    # ── Git operations ─────────────────────────────────────────────────────

    def _setup_git_identity(self) -> None:
        """Set git author identity in the worktree."""
        if not _WORKTREE_PATH:
            return
        try:
            subprocess.run(
                ["git", "config", "user.name", GIT_AUTHOR_NAME],
                cwd=_WORKTREE_PATH, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", GIT_AUTHOR_EMAIL],
                cwd=_WORKTREE_PATH, capture_output=True,
            )
        except Exception:
            pass

    async def _commit_worktree(self, objective: str, failed: bool = False) -> None:
        """Commit changes in the worktree."""
        if not _WORKTREE_PATH:
            return
        try:
            # Stage all changes
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "-A",
                cwd=_WORKTREE_PATH,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

            # Check if there's anything to commit
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--cached", "--quiet",
                cwd=_WORKTREE_PATH,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            rc = await proc.wait()
            if rc == 0:
                return  # Nothing to commit

            if failed:
                msg = f"WIP: {objective[:200]} (stuck/failed)"
            else:
                msg = objective[:200]

            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", msg,
                cwd=_WORKTREE_PATH,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            self._log(f"Committed: {msg[:100]}")
        except Exception as exc:
            self._log(f"Commit error: {exc}")

    async def _get_files_changed(self) -> list[str]:
        """Return list of files changed in the worktree vs base branch."""
        if not _WORKTREE_PATH:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only", f"{_BASE_BRANCH}..HEAD",
                cwd=_WORKTREE_PATH,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            files = stdout.decode("utf-8", errors="replace").strip().splitlines()
            return [f for f in files if f]
        except Exception:
            return []

    # ── Gate execution ─────────────────────────────────────────────────────

    async def _run_gates(self, gates: list[str]) -> dict:
        """Run pre-merge gate commands in the worktree.

        Returns {"passed": bool, "failed_gate": str, "output": str}.
        """
        if not gates:
            return {"passed": True, "failed_gate": "", "output": ""}

        for gate in gates:
            self._log(f"Running gate: {gate}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-c", gate,
                    cwd=_WORKTREE_PATH,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=300,
                )
                if proc.returncode != 0:
                    out = stdout.decode("utf-8", errors="replace")[:1000]
                    err = stderr.decode("utf-8", errors="replace")[:1000]
                    return {
                        "passed": False,
                        "failed_gate": gate,
                        "output": f"STDOUT:\n{out}\nSTDERR:\n{err}",
                    }
            except asyncio.TimeoutError:
                return {
                    "passed": False,
                    "failed_gate": gate,
                    "output": "Gate timed out after 300s",
                }

        return {"passed": True, "failed_gate": "", "output": ""}

    # ── Internal logging ───────────────────────────────────────────────────

    @staticmethod
    def _log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    worker = DeveloperWorker()
    exit_code = asyncio.run(worker.run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
