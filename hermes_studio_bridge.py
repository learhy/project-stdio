#!/usr/bin/env python3
"""Hermes ↔ Studio Bridge — Production Pipeline Runner.

Called by Hermes (via terminal) to execute tasks through the Studio
LangGraph pipeline against real Boundary worktrees.

Usage:
    # Autonomous (auto-approve):
    python hermes_studio_bridge.py "Add doc comment to errors package"

    # With manual approval (interrupt → waits for stdin):
    python hermes_studio_bridge.py "Add rate limiter middleware" --no-auto-ship

    # Dry-run (no real workers, placeholder state only):
    python hermes_studio_bridge.py "Test intent" --dry-run

Exit codes:
    0 — Success (graph completed, tests passed)
    1 — Error (graph failed or rejected)
    2 — Interrupted (waiting for human approval, not applicable for auto_ship)

Protocol:
    The script outputs JSON lines prefixed with ``STUDIO_RESULT:`` for
    structured parsing by Hermes. Final summary is written to stdout.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Add project root to path so imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio_isolation.meta_orchestrator import MetaOrchestrator, ExecutionResult
from studio_isolation.langgraph_adapter import StudioGraphRunner
from studio_isolation.runner import WorkerSpawnResult

# ── Configuration ──────────────────────────────────────────────────────────────

BOUNDARY_REPO = Path("/home/dan.rohan/software/boundary")
STUDIO_DIR = Path("/home/dan.rohan/software/project-stdio")
CHECKPOINT_DIR = Path("/tmp/studio_checkpoints")


# ── Real Boundary Runner ───────────────────────────────────────────────────────

class BoundaryGoRunner:
    """Production runner: creates real Boundary worktrees and runs Go tools.

    Uses the local Boundary clone at BOUNDARY_REPO. Developer and QA nodes
    get real worktrees with ``git worktree add``. Review/bundler nodes get
    noop results (they use placeholder state from the graph).

    Worktrees are created under a temp directory and cleaned up after
    the graph completes. Each invocation uses a fresh temp dir to avoid
    collisions.

    Lifecycle:
        runner = BoundaryGoRunner()
        # ... use with StudioGraphRunner/MetaOrchestrator ...
        runner.cleanup()  # Call after graph completes
    """

    def __init__(self, repo_path: Path = BOUNDARY_REPO) -> None:
        self._base = repo_path
        self._worktree_root: Path | None = None
        self._worktrees: list[Path] = []

        if not self._base.exists():
            raise FileNotFoundError(f"Boundary repo not found at {self._base}")

    def _ensure_worktree_root(self) -> Path:
        if self._worktree_root is None:
            self._worktree_root = Path(tempfile.mkdtemp(prefix="studio-prod-"))
        return self._worktree_root

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: Any,
        worktree_path: str,
        task_spec: dict[str, Any],
        worker_type: str,
        **kwargs: Any,
    ) -> WorkerSpawnResult:
        """Spawn a worker. Only developer node gets a real worktree."""

        # Non-developer nodes: noop
        if node_id not in ("developer", "qa_verification"):
            return WorkerSpawnResult(
                worker_id=worker_id,
                token=f"noop-{node_id}",
                node_id=node_id,
            )

        root = self._ensure_worktree_root()
        actual_wt = root / f"{bundle_id}-{node_id}"

        # ── Create worktree from Boundary clone ───────────────────────────
        try:
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(actual_wt), "HEAD"],
                cwd=str(self._base),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return WorkerSpawnResult(
                    worker_id=worker_id,
                    token="",
                    node_id=node_id,
                    error=f"git worktree add failed: {result.stderr[:500]}",
                )
            self._worktrees.append(actual_wt)
        except subprocess.TimeoutExpired:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token="",
                node_id=node_id,
                error="git worktree add timed out",
            )
        except Exception as e:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token="",
                node_id=node_id,
                error=f"git worktree add error: {e}",
            )

        # ── Run the task ─────────────────────────────────────────────────
        objective = task_spec.get("objective", task_spec.get("bundle_input", ""))
        token = f"prod-{bundle_id}"

        if node_id == "developer":
            return self._run_developer(worker_id, node_id, actual_wt, objective, token)
        elif node_id == "qa_verification":
            return self._run_qa(worker_id, node_id, actual_wt, token)

        return WorkerSpawnResult(
            worker_id=worker_id,
            token=token,
            node_id=node_id,
        )

    def _run_developer(
        self,
        worker_id: str,
        node_id: str,
        worktree: Path,
        objective: str,
        token: str,
    ) -> WorkerSpawnResult:
        """Execute a developer task in the worktree.

        For now: runs go vet on the changed package. Future:
        will use OpenCode/Claude Code to actually implement code changes.
        """
        # Figure out which package to target from the objective
        target_pkg = self._infer_target_package(objective)

        # Run go vet
        try:
            vet = subprocess.run(
                ["go", "vet", target_pkg],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token="",
                node_id=node_id,
                error=f"go vet {target_pkg} timed out after 300s",
            )
        except FileNotFoundError:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token=token,
                node_id=node_id,
                error="go not found — is Go installed?",
            )

        if vet.returncode != 0:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token="",
                node_id=node_id,
                error=f"go vet {target_pkg} failed:\n{vet.stderr[:500]}",
            )

        # Run go list to verify the package exists
        try:
            go_list = subprocess.run(
                ["go", "list", target_pkg],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=60,
            )
            pkg_path = go_list.stdout.strip()
        except Exception:
            pkg_path = target_pkg

        # Emit structured output
        print(
            f"STUDIO_RESULT: {json.dumps({'changed_files': [str(target_pkg)], 'target_package': pkg_path, 'test_results': {'go_vet': 'passed', 'exit_code': 0}})}"
        )

        return WorkerSpawnResult(
            worker_id=worker_id,
            token=token,
            node_id=node_id,
        )

    def _run_qa(
        self,
        worker_id: str,
        node_id: str,
        worktree: Path,
        token: str,
    ) -> WorkerSpawnResult:
        """Run QA checks in the worktree.

        Currently runs go vet on internal/errors. In future: full test suite.
        """
        try:
            vet = subprocess.run(
                ["go", "vet", "./internal/errors/..."],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as e:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token=token,
                node_id=node_id,
                error=f"QA vet failed: {e}",
            )

        if vet.returncode != 0:
            print(f"QA vet found issues:\n{vet.stderr[:500]}", file=sys.stderr)
        else:
            print("PASS: go vet internal/errors")
            print("TESTS: 1")

        print(
            f"STUDIO_RESULT: {json.dumps({'qa_passed': vet.returncode == 0, 'qa_report': {'tests_run': 1, 'tests_passed': 1 if vet.returncode == 0 else 0, 'summary': 'go vet internal/errors'}})}"
        )

        return WorkerSpawnResult(
            worker_id=worker_id,
            token=token,
            node_id=node_id,
        )

    def _infer_target_package(self, objective: str) -> str:
        """Infer which Go package to target from the objective text."""
        objective_lower = objective.lower()

        # Map known Boundary packages
        if "errors" in objective_lower:
            return "./internal/errors/..."
        if "gprc" in objective_lower or "grpc" in objective_lower:
            return "./internal/..."
        if "auth" in objective_lower:
            return "./internal/auth/..."
        if "controller" in objective_lower:
            return "./internal/..."
        if "readme" in objective_lower or "doc" in objective_lower:
            return "./internal/errors/..."

        # Default: target the internal/errors package as a canary
        return "./internal/errors/..."

    def cleanup(self) -> None:
        """Remove all worktrees created by this runner."""
        for wt in reversed(self._worktrees):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt)],
                    cwd=str(self._base),
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
            shutil.rmtree(str(wt), ignore_errors=True)

        if self._worktree_root and self._worktree_root.exists():
            shutil.rmtree(str(self._worktree_root), ignore_errors=True)

        # Prune stale worktree entries in both repos
        for repo in [self._base, STUDIO_DIR / "boundary-repo"]:
            if repo.exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=str(repo),
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass


# ── CLI ────────────────────────────────────────────────────────────────────────

def emit_result(key: str, value: Any) -> None:
    """Emit a structured result line for Hermes to parse."""
    print(f"STUDIO_RESULT: {json.dumps({key: value})}")


async def run_pipeline(intent: str, auto_ship: bool = True, dry_run: bool = False) -> int:
    """Run the full Studio pipeline for an intent.

    Returns exit code: 0 success, 1 error, 2 interrupted.
    """
    import uuid

    bundle_id = f"bundle-{uuid.uuid4().hex[:12]}"
    start_time = time.monotonic()

    emit_result("bundle_id", bundle_id)
    emit_result("intent", intent)
    emit_result("auto_ship", auto_ship)
    emit_result("dry_run", dry_run)

    print(f"\n{'='*60}")
    print(f"Bundle: {bundle_id}")
    print(f"Intent: {intent}")
    print(f"Auto-ship: {auto_ship}")
    print(f"Dry-run: {dry_run}")
    print(f"{'='*60}\n")

    if dry_run:
        # Dry run: no real workers, placeholder state only
        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            state = await runner.run(
                bundle_input=intent,
                bundle_id=bundle_id,
                auto_ship=auto_ship,
                target_repo="learhy/boundary",
            )
            emit_result("dry_run_state", {
                "approved": state.get("approved"),
                "qa_passed": state.get("qa_passed"),
                "bundle_id": state.get("bundle_id"),
            })
            print("\n✅ Dry-run completed successfully (placeholder state)")
            return 0
        except Exception as e:
            emit_result("error", str(e))
            print(f"\n❌ Dry-run failed: {e}")
            return 1
        finally:
            await runner.close()

    # Production run with real Boundary runner
    boundary_runner = BoundaryGoRunner()

    # Create a mock DB handle (matches the fire test pattern)
    from unittest.mock import MagicMock, AsyncMock
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.conn = MagicMock()
    mock_db.conn.commit = AsyncMock()

    orch = await MetaOrchestrator.create(
        db_path=str(CHECKPOINT_DIR / f"{bundle_id}.db"),
        studio_runner=boundary_runner,
        studio_db=mock_db,
    )

    try:
        result: ExecutionResult = await orch.execute(
            intent=intent,
            bundle_id=bundle_id,
            auto_ship=auto_ship,
            target_repo="learhy/boundary",
        )

        elapsed = time.monotonic() - start_time
        emit_result("elapsed_seconds", round(elapsed, 1))
        emit_result("success", result.success)
        emit_result("pr_url", result.pr_url)
        emit_result("commit_sha", result.commit_sha)

        if result.was_interrupted:
            emit_result("was_interrupted", True)
            emit_result("human_decision", result.human_decision)
            print(f"\n⏸️  Interrupted — awaiting human decision: {result.human_decision}")
            print(f"To resume: python hermes_studio_bridge.py --resume {bundle_id} --decision approve")
            return 2

        if result.success:
            emit_result("final_state", {
                "qa_passed": result.state.get("qa_passed"),
                "approved": result.state.get("approved"),
                "changed_files": result.state.get("changed_files", []),
            })
            print(f"\n✅ Pipeline completed successfully ({elapsed:.1f}s)")
            if result.pr_url:
                print(f"   PR: {result.pr_url}")
            return 0
        else:
            emit_result("error", result.error)
            emit_result("final_state", {
                "qa_passed": result.state.get("qa_passed"),
                "approved": result.state.get("approved"),
            })
            print(f"\n❌ Pipeline failed ({elapsed:.1f}s)")
            if result.error:
                print(f"   Error: {result.error}")
            return 1

    except Exception as e:
        elapsed = time.monotonic() - start_time
        emit_result("error", str(e))
        emit_result("elapsed_seconds", round(elapsed, 1))
        print(f"\n💥 Unexpected error ({elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        await orch.close()
        boundary_runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hermes ↔ Studio Bridge — Run tasks through the Studio pipeline",
    )
    parser.add_argument(
        "intent",
        nargs="?",
        help="The task intent (e.g., 'Add doc comment to errors package')",
    )
    parser.add_argument(
        "--auto-ship",
        action="store_true",
        default=True,
        help="Auto-approve the bundle (default: True)",
    )
    parser.add_argument(
        "--no-auto-ship",
        action="store_true",
        help="Require human approval (interrupt → wait for stdin or --resume)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with placeholder state only, no real workers",
    )
    parser.add_argument(
        "--resume",
        metavar="BUNDLE_ID",
        help="Resume an interrupted bundle",
    )
    parser.add_argument(
        "--decision",
        metavar="DECISION",
        help="Approval decision for --resume: approve, reject: <reason>, or modify: <instructions>",
    )

    args = parser.parse_args()

    if args.no_auto_ship:
        auto_ship = False
    else:
        auto_ship = True

    # TODO: --resume is a stub for future interactive mode
    if args.resume:
        print("Error: --resume not yet implemented", file=sys.stderr)
        sys.exit(1)

    if not args.intent:
        parser.print_help()
        sys.exit(1)

    exit_code = asyncio.run(run_pipeline(args.intent, auto_ship, args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
