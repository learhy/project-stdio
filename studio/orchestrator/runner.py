"""Worker runner: spawns isolated worker subprocesses with bubblewrap.

Phase 3: always-unshare-net with per-worker egress proxy for network enforcement.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import WorkerState, CapabilityManifest, EgressProxySettings

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
        proxy_process: asyncio.subprocess.Process | None = None,
        error: str = "",
    ) -> None:
        self.worker_id = worker_id
        self.token = token
        self.node_id = node_id
        self.process = process
        self.proxy_process = proxy_process
        self.error = error


class LocalBwrapWorkerRunner:
    """Spawns worker subprocesses under bubblewrap isolation with egress proxy."""

    def __init__(
        self,
        db: "Database",
        socket_path: str,
        egress_proxy: EgressProxySettings | None = None,
        worker_command: list[str] | None = None,
        token_expiry_minutes: int = 15,
    ) -> None:
        self.db = db
        self.socket_path = socket_path
        self.egress_proxy = egress_proxy or EgressProxySettings()
        self.worker_command = worker_command or ["studio-worker"]
        self.token_expiry_minutes = token_expiry_minutes
        # Track proxy processes for cleanup
        self._proxy_processes: dict[str, asyncio.subprocess.Process] = {}

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
        """Spawn a worker subprocess in a bubblewrap container with egress proxy.

        Creates a git worktree at worktree_path on a sub-branch
        bundle/<bundle_id>/<node_id> off base_branch. Spawns a per-worker
        egress proxy on a Unix socket. Returns WorkerSpawnResult with
        the worker ID, token, process, and proxy process handles.
        """
        token = _generate_token()
        token_expires_at = self.now() + (self.token_expiry_minutes * 60)
        proxy_socket = f"{self.egress_proxy.socket_dir}/proxy-{worker_id}.sock"

        # Insert worker row
        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                bundle_id,
                node_id,
                token,
                token_expires_at,
                json.dumps(manifest.model_dump()),
                WorkerState.PENDING,
                self.now(),
            ),
        )
        await self.db.conn.commit()

        # Audit: worker spawn
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_spawned", "worker", worker_id,
             json.dumps({"bundle_id": bundle_id, "node_id": node_id,
                         "token_expires_at": token_expires_at}),
             self.now()),
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

        # Spawn egress proxy subprocess
        proxy_process: asyncio.subprocess.Process | None = None
        proxy_error: str = ""
        if self.egress_proxy.enabled and os.environ.get("STUDIO_TEST_MODE") != "1":
            try:
                proxy_process = await self._spawn_proxy(
                    worker_id=worker_id,
                    manifest=manifest,
                    proxy_socket=proxy_socket,
                )
                self._proxy_processes[worker_id] = proxy_process
            except Exception as exc:
                proxy_error = f"Proxy spawn failed: {exc}"
                return WorkerSpawnResult(
                    worker_id=worker_id,
                    token=token,
                    node_id=node_id,
                    process=None,
                    error=proxy_error,
                )

        # Build bwrap args (always --unshare-net)
        bwrap_args = self._build_bwrap_args(manifest, worktree_path, token, proxy_socket)

        # http_proxy over Unix socket: httpx and some tools support this
        proxy_url = f"http+unix://{proxy_socket.replace('/', '%2F')}"
        worker_env = {
            **os.environ,
            "STUDIO_WORKER_TOKEN": token,
            "STUDIO_SOCKET_PATH": self.socket_path,
            "STUDIO_WORKER_ID": worker_id,
            "STUDIO_BUNDLE_ID": bundle_id,
            "STUDIO_NODE_ID": node_id,
            "STUDIO_WORKTREE_PATH": worktree_path,
            "STUDIO_BASE_BRANCH": base_branch,
            "STUDIO_PROXY_SOCKET": proxy_socket,
            # Standard env vars for tools that support them
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            # Tell tools not to bypass the proxy for localhost
            "no_proxy": "",
            "NO_PROXY": "",
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
            proxy_process=proxy_process,
        )

    async def _spawn_proxy(
        self,
        worker_id: str,
        manifest: CapabilityManifest,
        proxy_socket: str,
    ) -> asyncio.subprocess.Process:
        """Spawn the per-worker egress proxy subprocess."""
        proxy_env = {
            **os.environ,
            "STUDIO_PROXY_SOCKET": proxy_socket,
            "STUDIO_MANIFEST_JSON": json.dumps(manifest.model_dump()),
        }
        return await asyncio.create_subprocess_exec(
            "studio-proxy",
            env=proxy_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

        # Set bot author identity so commits are attributed correctly
        for key, value in [("user.name", "studio-agents[bot]"), ("user.email", "studio-agents@learhy.net")]:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", path, "config", key, value,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

    def _build_bwrap_args(
        self,
        manifest: CapabilityManifest,
        worktree_path: str,
        token: str,
        proxy_socket: str = "",
    ) -> list[str]:
        """Translate a capability manifest into bubblewrap arguments.

        Phase 3 network model:
        - Always --unshare-net (no host network access)
        - Egress proxy reachable via bind-mounted Unix socket
        - Working directory read-write bound to the worktree path
        - Explicit read-only mounts from filesystem.reads
        - Explicit read-write mounts from filesystem.writes (create: true)
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
                args.extend(["--ro-bind", p, p])

        # Read-write mounts
        for write_grant in fs.writes:
            p = write_grant.path
            if os.path.exists(p) and write_grant.create:
                args.extend(["--bind", p, p])

        # Bind the orchestrator socket
        socket_dir = os.path.dirname(self.socket_path)
        if os.path.exists(socket_dir):
            args.extend(["--ro-bind", socket_dir, socket_dir])

        # Bind the proxy socket
        if proxy_socket:
            # The proxy socket file itself needs to be accessible
            # Bind its parent directory so it's reachable inside the namespace
            proxy_dir = os.path.dirname(proxy_socket)
            if proxy_dir and os.path.exists(proxy_dir):
                args.extend(["--ro-bind", proxy_dir, proxy_dir])

        # Network: always isolate — no host network
        args.append("--unshare-net")

        # Proc
        args.extend(["--proc", "/proc"])

        # Dev
        args.extend(["--dev", "/dev"])

        return args

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process,
        worker_id: str = "",
    ) -> None:
        """Send SIGTERM to worker and proxy, wait up to 30s, then SIGKILL."""
        # Kill proxy first so no new connections arrive
        proxy = self._proxy_processes.pop(worker_id, None) if worker_id else None
        if proxy:
            try:
                proxy.terminate()
            except ProcessLookupError:
                pass

        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass

        # Wait for proxy to exit after worker is gone
        if proxy:
            try:
                await asyncio.wait_for(proxy.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proxy.kill()
                await proxy.wait()
            finally:
                # Clean up proxy socket
                try:
                    os.unlink(f"{self.egress_proxy.socket_dir}/proxy-{worker_id}.sock")
                except OSError:
                    pass


class NoopWorkerRunner:
    """Runner that spawns no actual process — used for testing."""

    def __init__(self, db: "Database", token_expiry_minutes: int = 15) -> None:
        self.db = db
        self.token_expiry_minutes = token_expiry_minutes

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
        token_expires_at = int(time.time()) + (self.token_expiry_minutes * 60)
        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_id, bundle_id, node_id, token, token_expires_at,
             json.dumps(manifest.model_dump()),
             WorkerState.PENDING, int(time.time())),
        )
        await self.db.conn.commit()

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_spawned", "worker", worker_id,
             json.dumps({"bundle_id": bundle_id, "node_id": node_id,
                         "token_expires_at": token_expires_at}),
             int(time.time())),
        )
        await self.db.conn.commit()
        # Return a result with no real process
        return WorkerSpawnResult(worker_id, token, node_id, None)  # type: ignore[arg-type]
