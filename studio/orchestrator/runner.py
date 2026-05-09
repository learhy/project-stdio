"""Worker runner: spawns isolated worker subprocesses with bubblewrap.

Phase 1: LocalBwrapWorkerRunner with permissive network isolation escape hatch.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import WorkerState, CapabilityManifest

if TYPE_CHECKING:
    from .db import Database


def _generate_token() -> str:
    return secrets.token_hex(32)


class WorkerSpawnResult:
    def __init__(
        self,
        worker_id: str,
        token: str,
        node_id: str,
        process: asyncio.subprocess.Process | None = None,
        error: str = "",
    ) -> None:
        self.worker_id = worker_id
        self.token = token
        self.node_id = node_id
        self.process = process
        self.error = error


class LocalBwrapWorkerRunner:
    """Spawns worker subprocesses under bubblewrap isolation."""

    def __init__(
        self,
        db: "Database",
        socket_path: str,
        worker_command: list[str] | None = None,
        network_isolation: str = "permissive",
    ) -> None:
        self.db = db
        self.socket_path = socket_path
        self.worker_command = worker_command or ["studio-worker"]
        self.network_isolation = network_isolation

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
    ) -> WorkerSpawnResult:
        """Spawn a worker subprocess in a bubblewrap container.

        Creates a git worktree at worktree_path on a sub-branch
        bundle/<bundle_id>/<node_id> off base_branch. Returns
        WorkerSpawnResult with the worker ID, token, and process handle.
        """
        token = _generate_token()

        # Insert worker row
        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                bundle_id,
                node_id,
                token,
                json.dumps(manifest.model_dump()),
                WorkerState.PENDING,
                self.now(),
            ),
        )
        await self.db.conn.commit()

        # Create git worktree
        worker_branch = f"bundle/{bundle_id}/{node_id}"
        if not os.environ.get("STUDIO_TEST_MODE") == "1":
            try:
                await self._create_worktree(worktree_path, worker_branch, base_branch)
            except Exception as exc:
                return WorkerSpawnResult(
                    worker_id=worker_id,
                    token=token,
                    node_id=node_id,
                    process=None,
                    error=f"Worktree creation failed: {exc}",
                )

        # Build bwrap args
        bwrap_args = self._build_bwrap_args(manifest, worktree_path, token)

        # Spawn worker
        worker_env = {
            **os.environ,
            "STUDIO_WORKER_TOKEN": token,
            "STUDIO_SOCKET_PATH": self.socket_path,
            "STUDIO_WORKER_ID": worker_id,
            "STUDIO_BUNDLE_ID": bundle_id,
            "STUDIO_NODE_ID": node_id,
            "STUDIO_WORKTREE_PATH": worktree_path,
            "STUDIO_BASE_BRANCH": base_branch,
        }

        if task_spec:
            worker_env["STUDIO_TASK_SPEC"] = json.dumps(task_spec)

        process = await asyncio.create_subprocess_exec(
            *bwrap_args,
            *self.worker_command,
            env=worker_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        return WorkerSpawnResult(
            worker_id=worker_id,
            token=token,
            node_id=node_id,
            process=process,
        )

    async def _create_worktree(self, path: str, branch: str, base_branch: str) -> None:
        """Create a git worktree at path on branch, based off base_branch."""
        import os as _os
        _os.makedirs(_os.path.dirname(path) if _os.path.dirname(path) else path, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", path, "-b", branch,
            f"origin/{base_branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"git worktree add failed: {err}")

    def _build_bwrap_args(
        self,
        manifest: CapabilityManifest,
        worktree_path: str,
        token: str,
    ) -> list[str]:
        """Translate a capability manifest into bubblewrap arguments.

        Phase 1 filesystem model:
        - Working directory read-write bound to the worktree path
        - Explicit read-only mounts from filesystem.reads
        - Explicit read-write mounts from filesystem.writes (create: true)
        - Network: unshare-net if network_isolation is 'enforcing'
        """
        args = ["bwrap"]

        # Basic container setup
        args.extend(["--die-with-parent"])
        args.extend(["--tmpfs", "/tmp"])

        # Working directory (read-write)
        args.extend(["--bind", worktree_path, "/work"])
        args.extend(["--chdir", "/work"])

        # Explicit filesystem grants
        fs = manifest.grants.filesystem

        # Read-only mounts
        for read_grant in fs.reads:
            p = read_grant.path
            if os.path.exists(p):
                flag = "--ro-bind" if read_grant.recursive else "--ro-bind"
                args.extend([flag, p, p])

        # Read-write mounts
        for write_grant in fs.writes:
            p = write_grant.path
            if os.path.exists(p) and write_grant.create:
                flag = "--bind" if write_grant.recursive else "--bind"
                args.extend([flag, p, p])

        # Bind the orchestrator socket
        socket_dir = os.path.dirname(self.socket_path)
        if os.path.exists(socket_dir):
            args.extend(["--ro-bind", socket_dir, socket_dir])

        # Network isolation
        if self.network_isolation == "enforcing":
            args.append("--unshare-net")
        # Phase 1 permissive: no --unshare-net, worker gets host network

        # Proc
        args.extend(["--proc", "/proc"])

        # Dev
        args.extend(["--dev", "/dev"])

        return args

    async def kill_worker(self, process: asyncio.subprocess.Process) -> None:
        """Send SIGTERM, wait up to 30s, then SIGKILL."""
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass  # Already exited


class NoopWorkerRunner:
    """Runner that spawns no actual process — used for testing."""

    def __init__(self, db: "Database") -> None:
        self.db = db

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
    ) -> WorkerSpawnResult:
        token = _generate_token()
        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (worker_id, bundle_id, node_id, token, json.dumps(manifest.model_dump()),
             WorkerState.PENDING, int(time.time())),
        )
        await self.db.conn.commit()
        # Return a result with no real process
        return WorkerSpawnResult(worker_id, token, node_id, None)  # type: ignore[arg-type]
