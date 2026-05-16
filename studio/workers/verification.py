"""Verification strategy executor.

Runs verification strategies produced by the bundler against worker output.
Returns structured VerificationResult objects for the self-healing loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)


async def _now() -> int:
    return int(time.time())


class VerificationRunner:
    """Executes a VerificationStrategy and returns a VerificationResult."""

    def __init__(self, worktree_path: str, timeout_seconds: int = 60) -> None:
        self._worktree = worktree_path
        self._timeout = timeout_seconds

    async def run(self, strategy: dict | None, skip_types: list[str] | None = None) -> "VerificationResult":
        from studio.orchestrator.artifacts import (
            VerificationResult,
            VerificationFailure,
            VerificationStrategy,
            ArtifactType,
        )

        if skip_types is None:
            skip_types = []

        # No strategy supplied — fall back to pytest if tests exist
        if not strategy:
            return await self._fallback_pytest()

        vs = VerificationStrategy.from_dict(strategy)

        if vs.type.value in skip_types:
            return VerificationResult(passed=True, output="Verification skipped for artifact type")

        if vs.type == ArtifactType.EXECUTABLE_APP:
            return await self._verify_executable_app(vs)
        elif vs.type == ArtifactType.LIBRARY:
            return await self._verify_library(vs)
        elif vs.type == ArtifactType.INFRASTRUCTURE:
            return await self._verify_infrastructure(vs)
        elif vs.type == ArtifactType.DOCUMENTATION:
            return await self._verify_documentation(vs)
        elif vs.type == ArtifactType.DATA_SCHEMA:
            return await self._verify_data_schema(vs)
        elif vs.type == ArtifactType.TEST_SUITE:
            return await self._verify_library(vs)  # same pattern: run test command
        elif vs.type == ArtifactType.MIXED:
            return await self._verify_mixed(vs, skip_types)
        else:
            return VerificationResult(passed=True, output=f"No verification strategy for type: {vs.type}")

    # ── Executable app ──────────────────────────────────────────────────────

    async def _verify_executable_app(self, vs: "VerificationStrategy") -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        failures: list[VerificationFailure] = []
        output_lines: list[str] = []

        startup_cmd = vs.startup_command
        if not startup_cmd:
            return VerificationResult(passed=False, output="No startup_command in verification strategy",
                                      failures=[VerificationFailure(test_name="startup", summary="No startup_command")])

        # Start the app
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", startup_cmd,
                cwd=self._worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return VerificationResult(passed=False, output=f"Failed to start app: {exc}",
                                      failures=[VerificationFailure(test_name="startup", summary=str(exc))])

        output_lines.append(f"Started: {startup_cmd}")

        # Wait for health check
        if vs.health_check:
            healthy = await self._wait_for_health(vs.health_check)
            if not healthy:
                await self._kill_proc(proc)
                return VerificationResult(passed=False, output="Health check failed",
                                          failures=[VerificationFailure(test_name="health_check",
                                                                        summary=vs.health_check,
                                                                        error_output="Health check did not pass")])
            output_lines.append(f"Health check passed: {vs.health_check}")

        # Run smoke tests
        for st in vs.smoke_tests:
            try:
                passed, detail = await self._run_smoke_test(st)
                output_lines.append(f"Smoke test {st.method} {st.path}: {'PASS' if passed else 'FAIL'}")
                if not passed:
                    failures.append(VerificationFailure(
                        test_name=f"{st.method} {st.path}",
                        expected=f"status {st.expected_status}",
                        actual=detail.get("actual", "unknown"),
                        error_output=detail.get("error", ""),
                        summary=f"Smoke test failed: {st.method} {st.path}",
                    ))
            except Exception as exc:
                failures.append(VerificationFailure(
                    test_name=f"{st.method} {st.path}",
                    summary=str(exc),
                ))

        # Teardown
        if vs.teardown_command:
            try:
                subprocess.run(["bash", "-c", vs.teardown_command], cwd=self._worktree, capture_output=True, timeout=10)
            except Exception:
                pass

        await self._kill_proc(proc)
        output = "\n".join(output_lines)
        return VerificationResult(passed=len(failures) == 0, failures=failures, output=output)

    async def _wait_for_health(self, health_check: str, max_wait: int = 15) -> bool:
        """Poll a health check URL. Format: 'GET http://localhost:5000/'"""
        import urllib.request

        parts = health_check.split(" ", 1)
        url = parts[1] if len(parts) > 1 else parts[0]

        for _ in range(max_wait):
            try:
                req = urllib.request.Request(url, method="GET")
                urllib.request.urlopen(req, timeout=2)
                return True
            except Exception:
                await asyncio.sleep(1)
        return False

    async def _run_smoke_test(self, st: "SmokeTest") -> tuple[bool, dict]:
        import urllib.request
        from studio.orchestrator.artifacts import SmokeTest

        url = f"http://localhost:5000{st.path}"
        data = None
        if st.body:
            data = json.dumps(st.body).encode()
        headers = {"Content-Type": "application/json"} if data else {}

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=st.method)
            resp = urllib.request.urlopen(req, timeout=5)
            actual_status = resp.getcode()
            if actual_status == st.expected_status:
                return True, {}
            return False, {"actual": f"status {actual_status}", "error": resp.read().decode(errors="replace")[:500]}
        except Exception as exc:
            return False, {"actual": "connection failed", "error": str(exc)}

    async def _kill_proc(self, proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    # ── Library ─────────────────────────────────────────────────────────────

    async def _verify_library(self, vs: "VerificationStrategy") -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        test_cmd = vs.test_command or "pytest"
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", test_cmd,
                cwd=self._worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return VerificationResult(passed=True, output=output[:2000])

            return VerificationResult(passed=False, output=output[:2000] + "\n" + err[:1000],
                                      failures=[VerificationFailure(
                                          test_name=test_cmd,
                                          expected="exit code 0",
                                          actual=f"exit code {proc.returncode}",
                                          error_output=err[:1000],
                                          summary="Test command failed",
                                      )])
        except asyncio.TimeoutError:
            return VerificationResult(passed=False, output="Verification timed out",
                                      failures=[VerificationFailure(test_name=test_cmd, summary="Timed out")])
        except Exception as exc:
            return VerificationResult(passed=False, output=str(exc),
                                      failures=[VerificationFailure(test_name=test_cmd, summary=str(exc))])

    # ── Infrastructure ──────────────────────────────────────────────────────

    async def _verify_infrastructure(self, vs: "VerificationStrategy") -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        validate_cmd = vs.validate_command
        if not validate_cmd:
            return VerificationResult(passed=True, output="No validate_command — skipping infrastructure verification")

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", validate_cmd,
                cwd=self._worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return VerificationResult(passed=True, output=output[:2000])

            return VerificationResult(passed=False, output=output[:2000] + "\n" + err[:1000],
                                      failures=[VerificationFailure(
                                          test_name=validate_cmd,
                                          expected="exit code 0",
                                          actual=f"exit code {proc.returncode}",
                                          error_output=err[:1000],
                                          summary="Validation command failed",
                                      )])
        except asyncio.TimeoutError:
            return VerificationResult(passed=False, output="Verification timed out",
                                      failures=[VerificationFailure(test_name=validate_cmd, summary="Timed out")])
        except Exception as exc:
            return VerificationResult(passed=False, output=str(exc),
                                      failures=[VerificationFailure(test_name=validate_cmd, summary=str(exc))])

    # ── Documentation ───────────────────────────────────────────────────────

    async def _verify_documentation(self, vs: "VerificationStrategy") -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult
        return VerificationResult(passed=True, output="Documentation verification deferred to LLM review pass")

    # ── Data schema ─────────────────────────────────────────────────────────

    async def _verify_data_schema(self, vs: "VerificationStrategy") -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure

        validate_cmd = vs.validate_command
        if not validate_cmd:
            return VerificationResult(passed=True, output="No validate_command — skipping schema verification")

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", validate_cmd,
                cwd=self._worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return VerificationResult(passed=True, output=output[:2000])

            return VerificationResult(passed=False, output=output[:2000] + "\n" + err[:1000],
                                      failures=[VerificationFailure(
                                          test_name=validate_cmd,
                                          expected="exit code 0",
                                          actual=f"exit code {proc.returncode}",
                                          error_output=err[:1000],
                                          summary="Schema validation failed",
                                      )])
        except asyncio.TimeoutError:
            return VerificationResult(passed=False, output="Verification timed out",
                                      failures=[VerificationFailure(test_name=validate_cmd, summary="Timed out")])
        except Exception as exc:
            return VerificationResult(passed=False, output=str(exc),
                                      failures=[VerificationFailure(test_name=validate_cmd, summary=str(exc))])

    # ── Mixed ───────────────────────────────────────────────────────────────

    async def _verify_mixed(self, vs: "VerificationStrategy", skip_types: list[str]) -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult, VerificationFailure, ArtifactType

        sub_strategies = vs.model_dump().get("sub_strategies", [])
        if not sub_strategies:
            return VerificationResult(passed=True, output="Mixed type with no sub_strategies — skipping")

        all_failures: list[VerificationFailure] = []
        all_output: list[str] = []

        for sub in sub_strategies:
            result = await self.run(sub, skip_types)
            all_output.append(result.output)
            all_failures.extend(result.failures)

        return VerificationResult(
            passed=len(all_failures) == 0,
            failures=all_failures,
            output="\n---\n".join(all_output),
        )

    # ── Fallback ────────────────────────────────────────────────────────────

    async def _fallback_pytest(self) -> "VerificationResult":
        from studio.orchestrator.artifacts import VerificationResult

        # Check if tests directory or test files exist
        import os
        wt = self._worktree
        has_tests = False
        for root, dirs, files in os.walk(wt):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("test_") and f.endswith(".py"):
                    has_tests = True
                    break
            if has_tests:
                break

        if not has_tests:
            return VerificationResult(passed=True, output="No tests found — skipping verification")

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", "pytest",
                cwd=self._worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            output = stdout.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return VerificationResult(passed=True, output=output[:2000])
            return VerificationResult(passed=False, output=output[:2000])
        except Exception as exc:
            return VerificationResult(passed=False, output=str(exc))
