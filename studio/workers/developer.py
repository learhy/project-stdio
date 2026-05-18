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
import shlex
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
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1")

_STUCK_WINDOW = 20
_STUCK_THRESHOLD = 3
_HUMAN_INPUT_POLL_INTERVAL = 30
_OPencode_BIN = "opencode"

GIT_AUTHOR_NAME = "studio-agents[bot]"
GIT_AUTHOR_EMAIL = "studio-agents@learhy.net"


def _now() -> int:
    return int(time.time())


def _is_kill_or_abort(response: str) -> bool:
    """Check if a PM response indicates the worker should abort."""
    return (not response.strip() or
            response.strip().lower() in ("/kill", "/abort") or
            response.strip().lower().startswith("/kill ") or
            response.strip().lower().startswith("/abort "))


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
            "attempts": outcome.get("attempts", 1),
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
        model = self.task_spec.get("model", "ollama-cloud/deepseek-v4-pro")
        gates: list[str] = self.task_spec.get("gates", [])
        artifact_type = self.task_spec.get("artifact_type", "mixed")
        verification_strategy = self.task_spec.get("verification_strategy")
        max_attempts = self.task_spec.get("max_fix_attempts", 5)
        skip_verification_types = self.task_spec.get("skip_verification_for_types", ["documentation"])

        self._log(f"Worktree: {_WORKTREE_PATH}, base branch: {_BASE_BRANCH}")
        self._setup_git_identity()
        self._init_opencode_project()

        initial_prompt = self._build_initial_prompt(objective)
        self._log(f"Task objective: {initial_prompt[:200]}")
        await self.rpc.notify("worker.progress_report", {
            "stage": "starting",
            "percent": 0,
            "message": f"Starting task: {objective[:100]}",
        })

        last_result = None
        opencode_errors: list[str] = []

        for attempt in range(1, max_attempts + 1):
            # Step 1: Write (or fix) the code
            if attempt == 1:
                prompt = initial_prompt
            elif last_result is not None:
                prompt = self._build_fix_prompt(objective, last_result.failures, attempt)
            else:
                prompt = self._build_opencode_retry_prompt(objective, opencode_errors, attempt)

            self._log(f"Attempt {attempt}/{max_attempts}: running opencode")
            opencode_result = await self._run_opencode(prompt, model)
            opencode_errors = opencode_result.get("errors", [])

            if opencode_result.get("stuck"):
                await self._commit_worktree(objective, failed=True)
                return opencode_result
            if not opencode_result.get("success"):
                self._log(f"OpenCode failed on attempt {attempt}: {opencode_result.get('summary', '')}")
                await self._report_checkpoint(
                    phase_completed=f"Attempt {attempt}: opencode failed",
                    phase_starting=f"Attempt {attempt + 1}: retrying" if attempt < max_attempts else "All attempts exhausted",
                    concerns=[opencode_result.get("summary", "opencode failure")],
                )
                if attempt < max_attempts:
                    continue
                pm_response = await self._escalate_to_pm(objective, [], max_attempts)
                if pm_response and not _is_kill_or_abort(pm_response):
                    self._log(f"PM overrode opencode failure: {pm_response[:200]}")
                    await self._commit_worktree(objective, failed=False)
                    return {"outcome": "success",
                            "summary": f"Task accepted by PM after {max_attempts} failed opencode attempts",
                            "attempts": max_attempts,
                            "pm_override": True}
                await self._commit_worktree(objective, failed=True)
                return {"outcome": "failure", "summary": f"OpenCode failed after {max_attempts} attempts",
                        "errors": opencode_result.get("errors", [])}

            # Step 2: Verify
            self._current_phase = "running-tests"
            verify_result = await self._run_verification(verification_strategy, skip_verification_types)
            verify_result.attempt = attempt
            last_result = verify_result

            if verify_result.passed:
                # Step 3: Commit and run gates
                self._log(f"Verification passed on attempt {attempt}")
                committed = await self._commit_worktree(objective, failed=False)
                if not committed:
                    return {
                        "outcome": "failure",
                        "node_state": "failed",
                        "summary": "No output produced — worker completed but generated zero code changes",
                        "errors": ["no_output_produced"],
                    }
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
                return {
                    "outcome": "success",
                    "summary": f"Task completed on attempt {attempt}: {objective[:200]}",
                    "attempts": attempt,
                    "tests_run": len(gates),
                    "tests_passed": len(gates),
                    "files_changed": len(await self._get_files_changed()),
                }

            # Step 4: Build fix prompt from failure output (used in next iteration)
            self._log(f"Verification failed on attempt {attempt}: {len(verify_result.failures)} failures")
            await self._report_checkpoint(
                phase_completed=f"Attempt {attempt} failed verification",
                phase_starting=f"Attempt {attempt + 1}: fixing {len(verify_result.failures)} failures",
                concerns=[f.summary for f in verify_result.failures],
            )

        # All attempts exhausted
        pm_response = await self._escalate_to_pm(objective, last_result.failures if last_result else [], max_attempts)
        if pm_response and not _is_kill_or_abort(pm_response):
            self._log(f"PM overrode verification failure: {pm_response[:200]}")
            await self._commit_worktree(objective, failed=False)
            return {
                "outcome": "success",
                "summary": f"Task accepted by PM after {max_attempts} verification attempts",
                "attempts": max_attempts,
                "pm_override": True,
            }
        await self._commit_worktree(objective, failed=True)
        return {
            "outcome": "failure",
            "summary": f"Verification failed after {max_attempts} attempts",
            "attempts": max_attempts,
            "errors": [f.summary for f in (last_result.failures if last_result else [])],
        }

    # ── Initial prompt construction ─────────────────────────────────────────

    def _build_initial_prompt(self, objective: str) -> str:
        prompt_parts = [objective]
        description = self.task_spec.get("description", "")
        if description:
            prompt_parts.append(f"\nDetails: {description}")
        deps = self.task_spec.get("dependencies", [])
        if deps:
            prompt_parts.append(f"\nDependencies: {', '.join(deps)}")
        language = self.task_spec.get("language", "")
        if language:
            prompt_parts.append(f"\nUse {language}.")
        bundle_idea = self.task_spec.get("bundle_idea", "")
        if bundle_idea:
            prompt_parts.append(f"\n---\nAdditional context from the original request:\n{bundle_idea}")
        bundle_requirements = self.task_spec.get("bundle_requirements", "")
        if bundle_requirements:
            prompt_parts.append(f"\nRequirements summary: {bundle_requirements}")
        return "\n".join(prompt_parts)

    # ── OpenCode execution ──────────────────────────────────────────────────

    async def _run_opencode(self, prompt: str, model: str) -> dict:
        """Run opencode and return result dict. Does not commit."""
        saved_cwd = os.getcwd()
        chdir_done = False
        if os.path.isdir(_WORKTREE_PATH):
            os.chdir(_WORKTREE_PATH)
            chdir_done = True

        inner_cmd = f"{_OPencode_BIN} run --model {model} --print-logs {shlex.quote(prompt)}"
        cmd = ["bash", "-c", inner_cmd]

        try:
            self._agent_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "OLLAMA_CLOUD_BASE_URL": _OLLAMA_BASE_URL},
            )

            await self.rpc.notify("worker.progress_report", {
                "stage": "running",
                "percent": 50,
                "message": "OpenCode is working...",
            })

            stdout_lines, stderr_data, stuck = await self._stream_and_detect_stuck()
            returncode = await self._agent_process.wait()

            if stuck:
                self._log("OpenCode appears stuck — requesting human input")
                human_response = await self._request_human_input(
                    f"OpenCode appears stuck on task: {prompt[:200]}",
                    "\n".join(stdout_lines[-20:]),
                )
                if human_response:
                    return {"success": False, "stuck": True,
                            "summary": f"Stuck — human responded: {human_response[:200]}",
                            "errors": ["stuck-after-human-input"]}
                return {"success": False, "stuck": True,
                        "summary": "Stuck — no human response within poll window",
                        "errors": ["stuck-no-human-response"]}

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

            print(f"[developer] opencode exit={returncode} stdout_lines={len(stdout_lines)} stderr_bytes={len(stderr_data)}", file=sys.stderr, flush=True)

            if returncode == 0:
                return {"success": True, "stuck": False, "stdout_lines": stdout_lines}
            else:
                return {"success": False, "stuck": False,
                        "summary": f"OpenCode exited with code {returncode}",
                        "errors": [f"exit_code={returncode}"]}

        except asyncio.TimeoutError:
            if self._agent_process:
                self._agent_process.kill()
                await self._agent_process.wait()
            return {"success": False, "stuck": False,
                    "summary": "OpenCode exceeded time limit",
                    "errors": ["timeout"]}
        except Exception as exc:
            return {"success": False, "stuck": False,
                    "summary": f"Execution error: {exc}",
                    "errors": [str(exc)]}
        finally:
            if chdir_done:
                os.chdir(saved_cwd)

    # ── Verification ────────────────────────────────────────────────────────

    async def _run_verification(self, strategy: dict | None, skip_types: list[str]) -> "VerificationResult":
        from .verification import VerificationRunner
        from studio.orchestrator.artifacts import VerificationResult

        timeout = self.task_spec.get("verification_timeout_seconds", 60)
        runner = VerificationRunner(_WORKTREE_PATH, timeout_seconds=timeout)
        try:
            return await runner.run(strategy, skip_types)
        except Exception as exc:
            from studio.orchestrator.artifacts import VerificationFailure
            return VerificationResult(
                passed=False,
                output=str(exc),
                failures=[VerificationFailure(test_name="verification_runner", summary=str(exc))],
            )

    # ── Fix prompt construction ─────────────────────────────────────────────

    def _build_fix_prompt(self, original_objective: str, failures: list, attempt: int) -> str:
        failure_text = "\n".join([
            f"FAILURE {i+1}: {f.test_name}\n"
            f"  Expected: {f.expected}\n"
            f"  Got: {f.actual}\n"
            f"  Error: {f.error_output}"
            for i, f in enumerate(failures)
        ])

        return f"""The code you wrote failed verification on attempt {attempt}.

Original objective: {original_objective}

Verification failures:
{failure_text}

Fix these specific failures. Do not change code that is working correctly.
After fixing, the verification will run again automatically."""

    def _build_opencode_retry_prompt(self, original_objective: str, errors: list[str], attempt: int) -> str:
        error_text = "\n".join(f"- {e}" for e in errors) if errors else "Unknown error"
        return f"""The previous attempt to execute this task failed with errors:

{error_text}

Original objective: {original_objective}

Fix the error and complete the task. Do not repeat the same approach that caused the failure."""

    # ── Escalation ──────────────────────────────────────────────────────────

    async def _report_checkpoint(self, phase_completed: str, phase_starting: str, concerns: list[str]) -> None:
        try:
            await self.rpc.call("worker.report_checkpoint", {
                "checkpoint_id": f"{_WORKER_ID}-{_now()}",
                "phase_completed": phase_completed,
                "phase_starting": phase_starting,
                "summary": phase_completed,
                "concerns": concerns,
                "estimated_remaining": {},
            })
        except Exception:
            pass

    async def _escalate_to_pm(self, objective: str, failures: list, attempts: int) -> str | None:
        """Escalate to PM and return the human response (or None if no response).

        Returns the PM's response string, or None if the PM did not respond
        or the escalation failed.
        """
        from studio.orchestrator.artifacts import VerificationFailure

        failure_lines = []
        for f in failures:
            if isinstance(f, VerificationFailure):
                failure_lines.append(f"- {f.test_name}: {f.summary}")
            else:
                failure_lines.append(f"- {f}")

        context = f"""Task: {objective[:500]}

All {attempts} verification attempts exhausted.

Failures:
{chr(10).join(failure_lines) if failure_lines else 'OpenCode execution failure on every attempt'}"""

        self._log(f"Escalating to PM after {attempts} attempts")
        try:
            await self.rpc.notify("worker.progress_report", {
                "stage": "escalating",
                "percent": 100,
                "message": f"Escalating after {attempts} failed attempts",
            })
            response = await self._request_human_input(
                f"All {attempts} verification attempts exhausted for task",
                context,
            )
            if response:
                self._log(f"PM responded to escalation: {response[:200]}")
                return response
            self._log("No PM response received within poll window")
            return None
        except Exception:
            return None

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

    def _init_opencode_project(self) -> None:
        """Create a .git/opencode file in the worktree if it doesn't exist.

        Without this, the first ``opencode run`` invocation may fail to identify
        the worktree as a distinct project and fall back to an auto-discovered
        server bound to the main project directory.
        """
        if not _WORKTREE_PATH:
            return
        opencode_file = os.path.join(_WORKTREE_PATH, ".git", "opencode")
        if os.path.exists(opencode_file):
            return
        try:
            # Get the git repo's initial commit SHA to use as project key
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=_WORKTREE_PATH, capture_output=True, text=True,
            )
            commit_sha = proc.stdout.strip()
            if commit_sha:
                with open(opencode_file, "w") as f:
                    f.write(commit_sha + "\n")
                self._log(f"Initialized opencode project: {commit_sha[:16]}...")
        except Exception:
            pass

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

    async def _commit_worktree(self, objective: str, failed: bool = False) -> bool:
        """Commit changes in the worktree. Returns True if changes were committed."""
        if not _WORKTREE_PATH:
            return False
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
                return False  # Nothing to commit

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
            return True
        except Exception as exc:
            self._log(f"Commit error: {exc}")
            return False

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
