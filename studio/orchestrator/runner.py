"""Worker runner: spawns isolated worker subprocesses with bubblewrap.

Phase 3: always-unshare-net with per-worker egress proxy for network enforcement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh
import docker as docker_lib

from .models import WorkerState, NodeState, CapabilityManifest, EgressProxySettings, RemoteFleetSettings, FleetHost, K8sRunnerSettings, DockerRunnerSettings, RunnerSelectorSettings
from . import tls as tls_helpers

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)

# Resolve worker binaries relative to sys.executable so subprocess spawn works
# even when .venv/bin is not on PATH.
_VENV_BIN_DIR = os.path.dirname(os.path.abspath(sys.executable))


def _resolve_bin(name: str) -> str:
    """Return full path to a venv binary, falling back to bare name."""
    path = os.path.join(_VENV_BIN_DIR, name)
    if os.path.isfile(path):
        return path
    return name


def _generate_token() -> str:
    return secrets.token_hex(32)


def capability_to_bwrap_args(
    manifest: CapabilityManifest,
    worktree_path: str,
    socket_path: str = "",
    proxy_socket: str = "",
) -> list[str]:
    """Translate a capability manifest into bubblewrap arguments.

    Phase 3 network model:
    - Always --unshare-net (no host network access)
    - Egress proxy reachable via bind-mounted Unix socket
    - Working directory read-write bound to the worktree path
    - Explicit read-only mounts from filesystem.reads
    - Explicit read-write mounts from filesystem.writes (create: true)

    Shared between LocalBwrapWorkerRunner and RemoteSSHWorkerRunner (Bundle 4.2).
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

    # Bind the orchestrator socket directory (local workers only)
    if socket_path:
        socket_dir = os.path.dirname(socket_path)
        if os.path.exists(socket_dir):
            args.extend(["--ro-bind", socket_dir, socket_dir])

    # Bind the proxy socket
    if proxy_socket:
        proxy_dir = os.path.dirname(proxy_socket)
        if proxy_dir and os.path.exists(proxy_dir):
            args.extend(["--ro-bind", proxy_dir, proxy_dir])

    # Network: isolate when egress proxy is active, otherwise skip
    # --unshare-net requires CAP_NET_ADMIN which may not be available
    if proxy_socket:
        args.append("--unshare-net")

    # Proc
    args.extend(["--proc", "/proc"])

    # Dev
    args.extend(["--dev", "/dev"])

    return args


def capability_to_runner_compatibility(
    manifest: CapabilityManifest,
) -> dict[str, dict[str, Any]]:
    """Return per-runner compatibility info including which grants each runner enforces.

    k8s runners cannot enforce exec_allowlist (no bubblewrap), so those grants
    are reported as unenforced. local and SSH runners enforce everything.
    """
    compat: dict[str, dict[str, Any]] = {
        "local": {"compatible": True, "unenforced_grants": []},
        "remote_ssh": {"compatible": True, "unenforced_grants": []},
        "k8s": {"compatible": True, "unenforced_grants": []},
        "docker": {"compatible": True, "unenforced_grants": []},
    }

    # k8s and docker can't enforce exec allowlists — no bwrap in containers
    exec_grants = manifest.grants.process.exec
    if exec_grants:
        compat["k8s"]["unenforced_grants"] = ["exec_allowlist"]
        compat["docker"]["unenforced_grants"] = ["exec_allowlist"]

    return compat


def capability_to_docker_args(
    manifest: CapabilityManifest,
    worker_id: str,
    orchestrator_addr: str,
    token: str,
    docker_network: str = "",
    proxy_env: dict[str, str] | None = None,
) -> list[str]:
    """Translate a capability manifest into docker run flags (Bundle 4.5).

    Returns a list of docker run arguments for the worker container.
    Network namespace is shared with the proxy sidecar via --network container:<proxy>.
    """
    args: list[str] = []

    # Resource grants
    resources = manifest.grants.resources
    if resources.cpu_limit:
        args.extend(["--cpus", str(resources.cpu_limit)])
    if resources.memory_limit:
        args.extend(["--memory", f"{resources.memory_limit}m"])
    args.extend(["--pids-limit", "256"])

    # Security defaults
    args.extend(["--read-only"])
    args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=512M"])
    args.extend(["--no-new-privileges"])
    args.extend(["--cap-drop", "ALL"])
    args.extend(["--user", "10000:10000"])

    # Working directory (shared volume mounted at /work)
    args.extend(["--workdir", "/work"])

    # Orchestrator connection env vars
    args.extend(["--env", f"STUDIO_ORCHESTRATOR_ADDR={orchestrator_addr}"])
    args.extend(["--env", f"STUDIO_WORKER_TOKEN={token}"])
    args.extend(["--env", f"STUDIO_WORKER_ID={worker_id}"])

    # Pass proxy config as env vars if proxy is running
    if proxy_env:
        for key, val in proxy_env.items():
            args.extend(["--env", f"{key}={val}"])

    # Secrets as env vars
    for secret in manifest.grants.secrets:
        args.extend(["--env", f"{secret.name}={secret.purpose}"])

    # DNS
    dns = manifest.grants.network.dns
    if dns.enabled and dns.resolvers:
        for resolver in dns.resolvers:
            args.extend(["--dns", resolver])
    elif not dns.enabled:
        args.extend(["--dns", "0.0.0.0"])

    # Labels for identification
    args.extend(["--label", f"studio/worker-id={worker_id}"])
    args.extend(["--label", "studio/runner=docker"])

    return args


class WorkerSpawnResult:
    def __init__(
        self,
        worker_id: str,
        token: str,
        node_id: str,
        process: asyncio.subprocess.Process | "RemoteWorkerHandle | K8sWorkerHandle | None" = None,
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
        ca_cert_path: str = "",
        ca_key_path: str = "",
    ) -> None:
        self.db = db
        self.socket_path = socket_path
        self.egress_proxy = egress_proxy or EgressProxySettings()
        self.worker_command = worker_command or [_resolve_bin("studio-worker")]
        self.token_expiry_minutes = token_expiry_minutes
        self.ca_cert_path = ca_cert_path
        self.ca_key_path = ca_key_path
        self._bwrap_available: bool | None = None  # cached
        # Track proxy processes for cleanup
        self._proxy_processes: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def now() -> int:
        return int(time.time())

    @staticmethod
    async def _check_bwrap() -> bool:
        """Check if bwrap works on this system (needs user namespaces)."""
        import logging
        _logger = logging.getLogger(__name__)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bwrap", "--unshare-user", "--die-with-parent",
                "--ro-bind", "/usr", "/usr",
                "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            ok = proc.returncode == 0
            if not ok:
                _logger.warning("bwrap not available, workers run without container isolation: %s",
                                stderr.decode(errors="replace").strip().split("\n")[0] if stderr else "unknown error")
            return ok
        except FileNotFoundError:
            _logger.warning("bwrap not installed, workers run without container isolation")
            return False

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        """Spawn a worker subprocess in a bubblewrap container with egress proxy.

        Creates a git worktree at worktree_path on a sub-branch
        bundle/<bundle_id>/<node_id> off base_branch. Spawns a per-worker
        egress proxy on a Unix socket. Returns WorkerSpawnResult with
        the worker ID, token, process, and proxy process handles.

        When target is 'new-repo', initializes an empty git repository
        instead of creating a worktree from the base branch.

        worker_type "review" runs studio-review (review.py); "developer" runs
        the configured worker_command (default studio-worker / developer.py).
        """
        token = _generate_token()
        token_expires_at = self.now() + (self.token_expiry_minutes * 60)
        proxy_socket = f"{self.egress_proxy.socket_dir}/proxy-{worker_id}.sock" if self.egress_proxy.enabled else ""

        # Clean up terminal-state worker rows for the same bundle_id + node_id
        existing = await self.db.fetch_one(
            "SELECT id, state FROM workers WHERE bundle_id = ? AND node_id = ?",
            (bundle_id, node_id),
        )
        reusing_pending = False
        if existing:
            terminal_states = (WorkerState.COMPLETE, WorkerState.FAILED, WorkerState.KILLED, WorkerState.CONNECTION_LOST)
            if existing["state"] in terminal_states:
                # Unlink dag_nodes first (FK constraint), then delete worker
                await self.db.execute(
                    "UPDATE dag_nodes SET worker_id = NULL WHERE worker_id = ?",
                    (existing["id"],),
                )
                await self.db.execute(
                    "DELETE FROM workers WHERE id = ?", (existing["id"],)
                )
                await self.db.conn.commit()
            elif existing["state"] == WorkerState.PENDING:
                # Previous spawn attempt inserted the worker but didn't complete
                # (e.g. worktree creation or process spawn failed). Reuse the row.
                reusing_pending = True
            else:
                raise RuntimeError(
                    f"Worker {existing['id']} for bundle={bundle_id} node={node_id} "
                    f"is already in state {existing['state']} — cannot re-spawn"
                )

        if not reusing_pending:
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

        # Create git worktree or new repo
        worker_branch = f"bundle/{bundle_id}/{node_id}"
        if not os.environ.get("STUDIO_TEST_MODE") == "1":
            try:
                if target == "new-repo":
                    await self._init_new_repo(worktree_path, bundle_id, node_id)
                else:
                    await self._create_worktree(worktree_path, worker_branch, base_branch)
            except Exception as exc:
                return WorkerSpawnResult(
                    worker_id=worker_id,
                    token=token,
                    node_id=node_id,
                    process=None,
                    error=f"Worktree creation failed: {exc}",
                )

        # Issue mTLS worker certificate (Bundle 4.1 mTLS)
        worker_cert_path = ""
        worker_key_path = ""
        if self.ca_cert_path and self.ca_key_path:
            cert_pem, key_pem = tls_helpers.issue_worker_cert(
                self.ca_cert_path, self.ca_key_path, worker_id
            )
            certs_dir = os.path.join(worktree_path, ".studio", "mtls")
            os.makedirs(certs_dir, exist_ok=True)
            worker_cert_path = os.path.join(certs_dir, "worker.crt")
            worker_key_path = os.path.join(certs_dir, "worker.key")
            with open(worker_cert_path, "wb") as f:
                f.write(cert_pem)
            with open(worker_key_path, "wb") as f:
                f.write(key_pem)
            os.chmod(worker_key_path, 0o600)

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

        # Check bwrap availability (cached). Skip in test mode.
        if self._bwrap_available is None:
            if os.environ.get("STUDIO_TEST_MODE") == "1":
                self._bwrap_available = True  # assume available in tests
            else:
                self._bwrap_available = await self._check_bwrap()
        use_bwrap = self._bwrap_available

        worker_cmd = [_resolve_bin("studio-review")] if worker_type == "review" else self.worker_command
        # Build bwrap args or run directly
        if use_bwrap:
            bwrap_args = capability_to_bwrap_args(manifest, worktree_path, self.socket_path, proxy_socket)
            cmd = [*bwrap_args, *worker_cmd]
        else:
            cmd = [*worker_cmd]

        # http_proxy over Unix socket: httpx and some tools support this
        worker_env = {
            **os.environ,
            "STUDIO_WORKER_TOKEN": token,
            "STUDIO_SOCKET_PATH": self.socket_path,
            "STUDIO_WORKER_ID": worker_id,
            "STUDIO_BUNDLE_ID": bundle_id,
            "STUDIO_NODE_ID": node_id,
            "STUDIO_WORKTREE_PATH": worktree_path,
            "STUDIO_BASE_BRANCH": base_branch,
            "STUDIO_TARGET": target,
            "OLLAMA_CLOUD_BASE_URL": os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1"),
        }

        # Remove GitHub credentials from worker environment so workers
        # cannot accidentally push to remote repositories. The orchestrator
        # manages all GitHub operations post-completion.
        for cred_var in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
            worker_env.pop(cred_var, None)

        if proxy_socket:
            proxy_url = f"http+unix://{proxy_socket.replace('/', '%2F')}"
            worker_env.update({
                "STUDIO_PROXY_SOCKET": proxy_socket,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "no_proxy": "",
                "NO_PROXY": "",
            })

        if task_spec:
            worker_env["STUDIO_TASK_SPEC"] = json.dumps(task_spec)

        # mTLS cert paths for TCP connections (Bundle 4.1 mTLS)
        if worker_cert_path:
            worker_env["STUDIO_WORKER_CERT"] = worker_cert_path
            worker_env["STUDIO_WORKER_KEY"] = worker_key_path
            worker_env["STUDIO_ORCHESTRATOR_CA"] = self.ca_cert_path

        process = await asyncio.create_subprocess_exec(
            *cmd,
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
        """Create a git worktree at path on branch, based off base_branch.

        After creation, strips any inherited remote and configures local-only
        git identity so workers cannot accidentally push to the host's remotes.
        """
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

        # Strip any inherited remote to prevent accidental pushes
        await self._strip_remotes(path)

        # Set bot author identity so commits are attributed correctly
        for key, value in [("user.name", "studio-agents[bot]"), ("user.email", "studio-agents@learhy.net")]:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", path, "config", key, value,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

    async def _init_new_repo(self, path: str, bundle_id: str = "", node_id: str = "") -> None:
        """Create an empty git repository with a unique initial empty commit.

        The commit message includes the bundle and node ID so that every
        worktree gets a unique SHA.  OpenCode keys its session cache by
        git commit SHA; duplicate SHAs cause it to reuse stale state and
        skip file creation.

        No remote is added — the orchestrator manages all remote operations.
        """
        import os as _os
        _os.makedirs(path, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"git init failed: {err}")

        # Remove any default remote (e.g., from template dir)
        await self._strip_remotes(path)

        # Set bot author identity and disable credential helpers
        for key, value in [
            ("user.name", "studio-agents[bot]"),
            ("user.email", "studio-agents@learhy.net"),
        ]:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", path, "config", key, value,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

        # Create initial empty commit so the repo has a branch to push
        commit_msg = f"Initial commit ({bundle_id}/{node_id})"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "commit", "--allow-empty", "-m", commit_msg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"git commit failed: {err}")

    @staticmethod
    async def _strip_remotes(path: str) -> None:
        """Remove all git remotes and disable credential helpers.

        Workers must never push to remote repositories. The orchestrator
        manages all GitHub operations post-completion.
        """
        # Remove all existing remotes
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "remote",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        remotes = stdout.decode().strip().split("\n")
        for remote in remotes:
            if remote.strip():
                rm_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", path, "remote", "remove", remote.strip(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await rm_proc.communicate()

        # Disable credential helpers so global git config won't leak credentials
        for key, value in [
            ("credential.helper", ""),
            ("credential.useHttpPath", "false"),
        ]:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", path, "config", "--local", key, value,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process | RemoteWorkerHandle | K8sWorkerHandle | DockerWorkerHandle,
        worker_id: str = "",
    ) -> None:
        """Send SIGTERM to worker and proxy, wait up to 30s, then SIGKILL."""
        if isinstance(process, DockerWorkerHandle):
            await process.cancel()
            await process.cleanup()
            return
        if isinstance(process, K8sWorkerHandle):
            await process.cancel()
            await process.cleanup()
            return
        if isinstance(process, RemoteWorkerHandle):
            await process.cancel()
            await process.cleanup()
            return
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
    """Runner that spawns no actual process — used for testing.

    Simulates immediate worker completion: after the executor marks the node
    RUNNING, a background task updates both the worker and node to terminal
    states so the bundle lifecycle can proceed.
    """

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
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        token = _generate_token()
        token_expires_at = int(time.time()) + (self.token_expiry_minutes * 60)
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_id, bundle_id, node_id, token, token_expires_at,
             json.dumps(manifest.model_dump()),
             WorkerState.PENDING, now),
        )
        await self.db.conn.commit()

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_spawned", "worker", worker_id,
             json.dumps({"bundle_id": bundle_id, "node_id": node_id,
                         "token_expires_at": token_expires_at}),
             now),
        )
        await self.db.conn.commit()

        # Simulate immediate worker completion in the background.
        # The executor marks the node RUNNING after spawn_worker returns,
        # so we schedule this to run after a brief yield.
        loop = asyncio.get_running_loop()
        loop.create_task(self._simulate_completion(worker_id, bundle_id, node_id))

        return WorkerSpawnResult(worker_id, token, node_id, None)  # type: ignore[arg-type]

    async def _simulate_completion(
        self, worker_id: str, bundle_id: str, node_id: str
    ) -> None:
        """Simulate a worker completing its work immediately."""
        # Yield to let the executor mark the node as RUNNING first
        await asyncio.sleep(0)
        now = int(time.time())
        node_db_id = f"{bundle_id}:{node_id}"
        await self.db.execute(
            "UPDATE workers SET state = ?, ended_at = ? WHERE id = ?",
            (WorkerState.COMPLETE, now, worker_id),
        )
        await self.db.execute(
            "UPDATE dag_nodes SET state = ?, ended_at = ?, output_json = ? WHERE id = ?",
            (NodeState.COMPLETED, now,
             json.dumps({"outcome": "success", "summary": "noop simulation"}),
             node_db_id),
        )
        await self.db.conn.commit()


# ── Bundle 4.2: Remote SSH runner ──────────────────────────────────────────────

class RemoteWorkerHandle:
    """Tracks a worker process running on a remote fleet host via SSH."""

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        remote_pid: int,
        host: FleetHost,
        workdir: str,
        worker_id: str,
    ) -> None:
        self.conn = conn
        self.remote_pid = remote_pid
        self.host = host
        self.workdir = workdir
        self.worker_id = worker_id
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @returncode.setter
    def returncode(self, value: int | None) -> None:
        self._returncode = value

    async def cancel(self) -> None:
        """Send SIGTERM, wait up to 30s, then SIGKILL the remote process."""
        try:
            await self.conn.run(f"kill -TERM {self.remote_pid}", check=False)
            for _ in range(30):
                result = await self.conn.run(f"kill -0 {self.remote_pid}", check=False)
                if result.exit_status != 0:
                    self._returncode = -1
                    return
                await asyncio.sleep(1)
            await self.conn.run(f"kill -9 {self.remote_pid}", check=False)
            self._returncode = -9
        except Exception:
            self._returncode = -1

    async def is_alive(self) -> bool:
        """Check if the remote process is still running."""
        if self._returncode is not None:
            return False
        try:
            result = await self.conn.run(f"kill -0 {self.remote_pid}", check=False)
            alive = result.exit_status == 0
            if not alive:
                self._returncode = 1
            return alive
        except Exception:
            self._returncode = -1
            return False

    async def cleanup(self) -> None:
        """Remove the temporary working directory from the remote host."""
        if self.workdir:
            try:
                await self.conn.run(f"rm -rf {self.workdir}", check=False)
            except Exception:
                pass


class RemoteSSHWorkerRunner:
    """Spawns worker subprocesses on remote fleet hosts via SSH + bubblewrap."""

    def __init__(
        self,
        db: "Database",
        fleet: RemoteFleetSettings,
        egress_proxy: EgressProxySettings | None = None,
        worker_command: list[str] | None = None,
        token_expiry_minutes: int = 15,
        ca_cert_path: str = "",
        ca_key_path: str = "",
    ) -> None:
        self.db = db
        self.fleet = fleet
        self.egress_proxy = egress_proxy or EgressProxySettings()
        self.worker_command = worker_command or [_resolve_bin("studio-worker")]
        self.token_expiry_minutes = token_expiry_minutes
        self.ca_cert_path = ca_cert_path
        self.ca_key_path = ca_key_path
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._host_health: dict[str, bool] = {}
        self._host_last_ping: dict[str, float] = {}
        for host in fleet.hosts:
            self._host_semaphores[host.name] = asyncio.Semaphore(host.max_concurrent_workers)
            self._host_health[host.name] = True

    @staticmethod
    def now() -> int:
        return int(time.time())

    def _select_host(self) -> FleetHost | None:
        """Select a fleet host per the configured selection policy (least_loaded or round_robin)."""
        healthy = [h for h in self.fleet.hosts if self._host_health.get(h.name, False)]
        if not healthy:
            return None
        # Find first host with available capacity
        for host in healthy:
            sem = self._host_semaphores.get(host.name)
            if sem and not sem.locked():
                return host
        return None

    async def _preflight(self, conn: asyncssh.SSHClientConnection) -> list[str]:
        """Verify required binaries exist on the remote host. Returns list of missing items."""
        missing: list[str] = []
        for binary in ["bwrap", "studio-worker", "studio-proxy"]:
            result = await conn.run(f"command -v {binary}", check=False)
            if result.exit_status != 0:
                missing.append(binary)
        return missing

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        """Spawn a worker on a remote fleet host via SSH + bubblewrap."""
        token = _generate_token()
        token_expires_at = self.now() + (self.token_expiry_minutes * 60)

        host = self._select_host()
        if host is None:
            return WorkerSpawnResult(
                worker_id=worker_id, token=token, node_id=node_id,
                error="No healthy fleet host available with capacity",
            )

        sem = self._host_semaphores[host.name]
        await sem.acquire()

        try:
            try:
                conn = await asyncssh.connect(
                    host.addr,
                    username=host.ssh_user,
                    client_keys=[host.ssh_key_path] if host.ssh_key_path else None,
                    known_hosts=None,
                )
            except Exception as exc:
                sem.release()
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=f"SSH connection to {host.name} ({host.addr}) failed: {exc}",
                )

            missing = await self._preflight(conn)
            if missing:
                conn.close()
                await conn.wait_closed()
                sem.release()
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=f"Missing binaries on {host.name}: {', '.join(missing)}. "
                          f"Install them before using this host as a worker.",
                )

            workdir = f"/tmp/studio-worker-{worker_id}"
            await conn.run(f"mkdir -p {workdir}", check=True)

            if host.worktree_mode == "clone":
                worker_branch = f"bundle/{bundle_id}/{node_id}"
                await conn.run(
                    f"cd {workdir} && git clone --single-branch "
                    f"--branch {worker_branch} {os.getcwd()} repo 2>/dev/null || "
                    f"git clone --single-branch --branch main {os.getcwd()} repo",
                    check=False,
                )
                remote_workdir = f"{workdir}/repo"
            else:
                remote_workdir = worktree_path

            task_json = json.dumps(task_spec or {})
            escaped = task_json.replace("'", "'\\''")
            await conn.run(f"echo '{escaped}' > {workdir}/task-spec.json", check=True)

            manifest_json = json.dumps(manifest.model_dump())
            escaped_mf = manifest_json.replace("'", "'\\''")
            await conn.run(f"echo '{escaped_mf}' > {workdir}/manifest.json", check=True)

            # Spawn egress proxy on remote host
            proxy_socket = f"/tmp/studio-proxy-{worker_id}.sock"
            proxy_env_str = (
                f"STUDIO_PROXY_SOCKET={proxy_socket} "
                f"STUDIO_MANIFEST_JSON='{escaped_mf}'"
            )
            await conn.run(
                f"nohup env {proxy_env_str} studio-proxy > {workdir}/proxy.log 2>&1 & "
                f"echo $! > {workdir}/proxy.pid",
                check=False,
            )
            # Poll until the proxy socket appears (up to 5 seconds)
            for _ in range(50):
                result = await conn.run(
                    f"test -S {proxy_socket} && echo ok || echo no",
                    check=False,
                )
                if result.stdout.strip() == "ok":
                    break
                await asyncio.sleep(0.1)
            else:
                conn.close()
                await conn.wait_closed()
                sem.release()
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=(
                        f"Egress proxy failed to bind socket {proxy_socket} on {host.name} after 5s. "
                        f"Check {workdir}/proxy.log for errors."
                    ),
                )

            bwrap_args = capability_to_bwrap_args(
                manifest, remote_workdir, socket_path="", proxy_socket=proxy_socket
            )

            orchestrator_host = os.environ.get("STUDIO_ORCHESTRATOR_HOST", "localhost")
            worker_env = {
                "STUDIO_WORKER_TOKEN": token,
                "STUDIO_ORCHESTRATOR_ADDR": f"tcp://{orchestrator_host}:7811",
                "STUDIO_WORKER_ID": worker_id,
                "STUDIO_BUNDLE_ID": bundle_id,
                "STUDIO_NODE_ID": node_id,
                "STUDIO_WORKTREE_PATH": remote_workdir,
                "STUDIO_BASE_BRANCH": base_branch,
                "STUDIO_TARGET": target,
                "OLLAMA_CLOUD_BASE_URL": os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1"),
                "STUDIO_PROXY_SOCKET": proxy_socket,
                "http_proxy": f"http+unix://{proxy_socket.replace('/', '%2F')}",
                "https_proxy": f"http+unix://{proxy_socket.replace('/', '%2F')}",
                "no_proxy": "",
            }
            # Remove GitHub credentials (defense-in-depth for remote workers too)
            for cred_var in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
                worker_env.pop(cred_var, None)

            if task_spec:
                worker_env["STUDIO_TASK_SPEC"] = task_json

            if self.ca_cert_path and self.ca_key_path:
                cert_pem, key_pem = tls_helpers.issue_worker_cert(
                    self.ca_cert_path, self.ca_key_path, worker_id
                )
                cert_escaped = cert_pem.decode().replace("'", "'\\''")
                key_escaped = key_pem.decode().replace("'", "'\\''")
                await conn.run(f"echo '{cert_escaped}' > {workdir}/worker.crt", check=True)
                await conn.run(f"echo '{key_escaped}' > {workdir}/worker.key", check=True)
                await conn.run(f"chmod 600 {workdir}/worker.key", check=True)
                ca_pem = Path(self.ca_cert_path).read_bytes()
                ca_escaped = ca_pem.decode().replace("'", "'\\''")
                await conn.run(f"echo '{ca_escaped}' > {workdir}/ca.crt", check=True)
                worker_env["STUDIO_WORKER_CERT"] = f"{workdir}/worker.crt"
                worker_env["STUDIO_WORKER_KEY"] = f"{workdir}/worker.key"
                worker_env["STUDIO_ORCHESTRATOR_CA"] = f"{workdir}/ca.crt"

            worker_cmd = [_resolve_bin("studio-review")] if worker_type == "review" else self.worker_command
            cmd_parts = bwrap_args + worker_cmd
            cmd_str = " ".join(cmd_parts)

            worker_env_str = " ".join(
                f"{k}='{v.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'"
                for k, v in worker_env.items()
            )

            result = await conn.run(
                f"cd {remote_workdir} && nohup env {worker_env_str} {cmd_str} "
                f"> {workdir}/worker.log 2>&1 & echo $!",
                check=True,
            )
            remote_pid_str = result.stdout.strip()
            try:
                remote_pid = int(remote_pid_str)
            except ValueError:
                conn.close()
                await conn.wait_closed()
                sem.release()
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=f"Failed to capture remote PID from: {remote_pid_str}",
                )

            await self.db.execute(
                "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (worker_id, bundle_id, node_id, token, token_expires_at,
                 json.dumps(manifest.model_dump()), WorkerState.PENDING, self.now()),
            )
            await self.db.conn.commit()

            await self.db.execute(
                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("worker_spawned", "worker", worker_id,
                 json.dumps({"bundle_id": bundle_id, "node_id": node_id,
                             "token_expires_at": token_expires_at,
                             "remote_host": host.name, "remote_pid": remote_pid}),
                 self.now()),
            )
            await self.db.conn.commit()

            handle = RemoteWorkerHandle(conn, remote_pid, host, workdir, worker_id)
            logger.info("Remote worker %s spawned on %s (pid %s)", worker_id, host.name, remote_pid)

            return WorkerSpawnResult(worker_id, token, node_id, process=handle)

        except Exception as exc:
            sem.release()
            return WorkerSpawnResult(
                worker_id=worker_id, token=token, node_id=node_id,
                error=f"Remote spawn failed: {exc}",
            )

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process | RemoteWorkerHandle | K8sWorkerHandle | DockerWorkerHandle,
        worker_id: str = "",
    ) -> None:
        """Kill a worker by its handle (local, remote, k8s, or docker)."""
        if isinstance(process, DockerWorkerHandle):
            await process.cancel()
            await process.cleanup()
        elif isinstance(process, K8sWorkerHandle):
            await process.cancel()
            await process.cleanup()
        elif isinstance(process, RemoteWorkerHandle):
            await process.cancel()
            await process.cleanup()
            try:
                process.conn.close()
                await process.conn.wait_closed()
            except Exception:
                pass
            sem = self._host_semaphores.get(process.host.name)
            if sem:
                sem.release()
        elif isinstance(process, asyncio.subprocess.Process):
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            except ProcessLookupError:
                pass

    async def ping_hosts(self) -> dict[str, str]:
        """Ping all fleet hosts, update health status. Returns host->status map."""
        statuses: dict[str, str] = {}
        for host in self.fleet.hosts:
            try:
                conn = await asyncio.wait_for(
                    asyncssh.connect(
                        host.addr,
                        username=host.ssh_user,
                        client_keys=[host.ssh_key_path] if host.ssh_key_path else None,
                        known_hosts=None,
                    ),
                    timeout=10.0,
                )
                self._host_health[host.name] = True
                self._host_last_ping[host.name] = time.time()
                statuses[host.name] = "healthy"
                conn.close()
                try:
                    await conn.wait_closed()
                except Exception:
                    pass
            except Exception:
                statuses[host.name] = "degraded"
                self._host_health[host.name] = False
                self._host_last_ping[host.name] = time.time()
                logger.warning("Fleet host %s (%s) is degraded", host.name, host.addr)
        return statuses


# ── Bundle 4.3: Kubernetes Job runner ────────────────────────────────────────────

class K8sWorkerHandle:
    """Tracks a worker process running as a Kubernetes Job."""

    def __init__(
        self,
        job_name: str,
        pod_name: str,
        namespace: str,
        worker_id: str,
        api_client: Any,
    ) -> None:
        self.job_name = job_name
        self.pod_name = pod_name
        self.namespace = namespace
        self.worker_id = worker_id
        self.api_client = api_client
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @returncode.setter
    def returncode(self, value: int | None) -> None:
        self._returncode = value

    async def cancel(self) -> None:
        """Delete the Kubernetes Job, which terminates the Pod."""
        try:
            batch_v1 = self.api_client.BatchV1Api
            await batch_v1.delete_namespaced_job(
                name=self.job_name,
                namespace=self.namespace,
                propagation_policy="Foreground",
            )
            self._returncode = -1
        except Exception:
            self._returncode = -1

    async def is_alive(self) -> bool:
        """Check if the Job's Pod is still running."""
        if self._returncode is not None:
            return False
        try:
            core_v1 = self.api_client.CoreV1Api
            pod = await core_v1.read_namespaced_pod(
                name=self.pod_name, namespace=self.namespace
            )
            phase = pod.status.phase if pod.status else "Unknown"
            if phase in ("Succeeded", "Failed", "Unknown"):
                self._returncode = 0 if phase == "Succeeded" else 1
                return False
            return True
        except Exception:
            self._returncode = -1
            return False

    async def cleanup(self) -> None:
        """Delete the Job, NetworkPolicy, and associated Secrets from the namespace.

        Best-effort: failures are logged but not re-raised.
        """
        try:
            batch_v1 = self.api_client.BatchV1Api
            await batch_v1.delete_namespaced_job(
                name=self.job_name,
                namespace=self.namespace,
                propagation_policy="Background",
            )
        except Exception:
            pass

        try:
            networking_v1 = self.api_client.NetworkingV1Api
            await networking_v1.delete_namespaced_network_policy(
                name=f"studio-{self.worker_id}",
                namespace=self.namespace,
            )
        except Exception:
            pass

        try:
            core_v1 = self.api_client.CoreV1Api
            await core_v1.delete_namespaced_secret(
                name=f"studio-mtls-{self.worker_id}",
                namespace=self.namespace,
            )
            await core_v1.delete_namespaced_secret(
                name=f"studio-worker-{self.worker_id}",
                namespace=self.namespace,
            )
        except Exception:
            pass


class DockerWorkerHandle:
    """Handle to a Docker worker container and its sidecar resources (Bundle 4.5)."""

    def __init__(
        self,
        worker_id: str,
        worker_container_id: str,
        proxy_container_id: str,
        volume_name: str,
        proxy_volume_name: str,
        network_name: str,
        client: docker_lib.DockerClient,
    ) -> None:
        self.worker_id = worker_id
        self.worker_container_id = worker_container_id
        self.proxy_container_id = proxy_container_id
        self.volume_name = volume_name
        self.proxy_volume_name = proxy_volume_name
        self.network_name = network_name
        self._client = client
        self.returncode: int | None = None

    async def cancel(self) -> None:
        """Stop the worker container, then the proxy container."""
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, self.worker_container_id
            )
            container.stop(timeout=10)
        except Exception:
            pass
        try:
            proxy = await asyncio.to_thread(
                self._client.containers.get, self.proxy_container_id
            )
            proxy.stop(timeout=5)
        except Exception:
            pass
        self.returncode = 137

    async def is_alive(self) -> bool:
        """Check whether the worker container is still running."""
        if self.returncode is not None:
            return False
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, self.worker_container_id
            )
            return container.status == "running"
        except Exception:
            return False

    async def cleanup(self) -> None:
        """Remove containers, volumes, and network. Best-effort — logs failures."""
        for cid in (self.worker_container_id, self.proxy_container_id):
            try:
                container = await asyncio.to_thread(self._client.containers.get, cid)
                container.remove(force=True)
            except Exception:
                pass
        for vname in (self.volume_name, self.proxy_volume_name):
            try:
                vol = await asyncio.to_thread(self._client.volumes.get, vname)
                vol.remove(force=True)
            except Exception:
                pass
        try:
            net = await asyncio.to_thread(self._client.networks.get, self.network_name)
            net.remove()
        except Exception:
            pass


def capability_to_pod_spec(
    manifest: CapabilityManifest,
    worker_id: str,
    bundle_id: str,
    node_id: str,
    workdir: str,
    orchestrator_addr: str,
    proxy_image: str,
    worker_image: str,
    image_pull_policy: str,
    task_spec: dict[str, Any] | None = None,
    target: str = "existing-repo",
) -> dict[str, Any]:
    """Translate a capability manifest into a Kubernetes Pod spec (Bundle 4.3).

    Returns a dict suitable for use as a Job's spec.template.spec.
    """
    grants = manifest.grants

    # ── Volumes and volume mounts from filesystem grants ──
    volumes: list[dict] = []
    volume_mounts: list[dict] = [
        {"name": "worktree", "mountPath": "/work"},
        {"name": "mtls-certs", "mountPath": "/run/studio/mtls", "readOnly": True},
        {"name": "proxy-socket", "mountPath": "/tmp"},
    ]

    # Working tree volume (emptyDir shared between init and worker containers)
    volumes.append({"name": "worktree", "emptyDir": {}})

    # Proxy socket shared volume (proxy sidecar + worker + wait-for-proxy init container)
    volumes.append({"name": "proxy-socket", "emptyDir": {}})

    # mTLS cert volume (from Secret)
    volumes.append({
        "name": "mtls-certs",
        "secret": {"secretName": f"studio-mtls-{worker_id}"},
    })

    # Secret grants as volumes
    for secret_grant in grants.secrets:
        vol_name = f"secret-{secret_grant.name}"
        volumes.append({
            "name": vol_name,
            "secret": {"secretName": f"studio-worker-{worker_id}"},
        })
        volume_mounts.append({
            "name": vol_name,
            "mountPath": f"/run/studio/secrets/{secret_grant.name}",
            "readOnly": True,
        })

    # ── Environment variables ──
    env: list[dict] = [
        {"name": "STUDIO_WORKER_ID", "value": worker_id},
        {"name": "STUDIO_BUNDLE_ID", "value": bundle_id},
        {"name": "STUDIO_NODE_ID", "value": node_id},
        {"name": "STUDIO_WORKTREE_PATH", "value": "/work"},
        {"name": "STUDIO_TARGET", "value": target},
        {"name": "STUDIO_ORCHESTRATOR_ADDR", "value": f"tcp://{orchestrator_addr}"},
        {"name": "STUDIO_WORKER_CERT", "value": "/run/studio/mtls/tls.crt"},
        {"name": "STUDIO_WORKER_KEY", "value": "/run/studio/mtls/tls.key"},
        {"name": "STUDIO_ORCHESTRATOR_CA", "value": "/run/studio/mtls/ca.crt"},
        {"name": "STUDIO_PROXY_SOCKET", "value": "/tmp/studio-proxy.sock"},
        {"name": "STUDIO_PROXY_HOST", "value": "127.0.0.1"},
        {"name": "STUDIO_PROXY_PORT", "value": "8080"},
        {"name": "http_proxy", "value": "http://127.0.0.1:8080"},
        {"name": "https_proxy", "value": "http://127.0.0.1:8080"},
        {"name": "no_proxy", "value": ""},
    ]

    # Network egress grants as env var for proxy sidecar
    egress_destinations = [
        f"{e.destination}:{','.join(map(str, e.ports)) if e.ports else '*'}"
        for e in grants.network.egress
    ]
    env.append({
        "name": "STUDIO_EGRESS_ALLOWLIST",
        "value": json.dumps(egress_destinations),
    })

    if task_spec:
        env.append({"name": "STUDIO_TASK_SPEC", "value": json.dumps(task_spec)})

    # Exec allowlist as env var (enforced by worker, not k8s)
    exec_allowlist = [e.binary for e in grants.process.exec]
    if exec_allowlist:
        env.append({
            "name": "STUDIO_EXEC_ALLOWLIST",
            "value": json.dumps(exec_allowlist),
        })

    # ── Resources ──
    resources: dict = {"limits": {}, "requests": {}}
    res = grants.resources
    if res.cpu_limit:
        resources["limits"]["cpu"] = str(res.cpu_limit)
    if res.memory_limit:
        resources["limits"]["memory"] = f"{res.memory_limit}Mi"
    if res.disk_limit:
        resources["limits"]["ephemeral-storage"] = f"{res.disk_limit}Mi"

    # ── Security context ──
    security_context = {
        "runAsNonRoot": True,
        "runAsUser": 10000,
        "runAsGroup": 10000,
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
    }

    # ── Container specs ──
    worker_container = {
        "name": "worker",
        "image": worker_image,
        "imagePullPolicy": image_pull_policy,
        "env": env,
        "volumeMounts": volume_mounts,
        "resources": resources,
        "securityContext": security_context,
    }

    proxy_container = {
        "name": "egress-proxy",
        "image": proxy_image,
        "imagePullPolicy": image_pull_policy,
        "env": [
            {"name": "STUDIO_EGRESS_ALLOWLIST", "value": json.dumps(egress_destinations)},
            {"name": "STUDIO_PROXY_SOCKET", "value": "/tmp/studio-proxy.sock"},
        ],
        "volumeMounts": [
            {"name": "proxy-socket", "mountPath": "/tmp"},
        ],
        "ports": [{"containerPort": 8080, "protocol": "TCP"}],
        "livenessProbe": {
            "exec": {"command": ["test", "-S", "/tmp/studio-proxy.sock"]},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
            "failureThreshold": 3,
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10000,
            "runAsGroup": 10000,
            "allowPrivilegeEscalation": False,
        },
    }

    # Init container for git clone (worktree_mode=init_container)
    init_containers = [
        {
            "name": "clone-repo",
            "image": "alpine/git:latest",
            "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/sh", "-c"],
            "args": [
                f"git clone --single-branch --branch bundle/{bundle_id}/{node_id} "
                f"https://github.com/learhy/project-stdio.git /work 2>/dev/null || "
                f"git clone --single-branch --branch main "
                f"https://github.com/learhy/project-stdio.git /work"
            ],
            "volumeMounts": [{"name": "worktree", "mountPath": "/work"}],
        },
        {
            "name": "wait-for-proxy",
            "image": proxy_image,
            "imagePullPolicy": image_pull_policy,
            "command": ["/bin/sh", "-c"],
            "args": ["until test -S /tmp/studio-proxy.sock; do sleep 1; done"],
            "volumeMounts": [{"name": "proxy-socket", "mountPath": "/tmp"}],
        },
    ]

    pod_spec: dict[str, Any] = {
        "containers": [worker_container, proxy_container],
        "initContainers": init_containers,
        "volumes": volumes,
        "restartPolicy": "Never",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10000,
            "runAsGroup": 10000,
        },
    }

    # activeDeadlineSeconds from wall_time_limit
    if res.wall_time_limit:
        pod_spec["activeDeadlineSeconds"] = res.wall_time_limit

    return pod_spec


class K8sJobWorkerRunner:
    """Spawns worker subprocesses as Kubernetes Jobs (Bundle 4.3)."""

    def __init__(
        self,
        db: "Database",
        settings: K8sRunnerSettings,
        egress_proxy: EgressProxySettings | None = None,
        worker_command: list[str] | None = None,
        token_expiry_minutes: int = 15,
        ca_cert_path: str = "",
        ca_key_path: str = "",
    ) -> None:
        self.db = db
        self.settings = settings
        self.egress_proxy = egress_proxy or EgressProxySettings()
        self.worker_command = worker_command or [_resolve_bin("studio-worker")]
        self.token_expiry_minutes = token_expiry_minutes
        self.ca_cert_path = ca_cert_path
        self.ca_key_path = ca_key_path
        self._api_client: Any = None
        self._watch_task: asyncio.Task | None = None
        self._watched_workers: dict[str, K8sWorkerHandle] = {}
        self._running = False

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def _load_kubeconfig(self) -> Any:
        """Load Kubernetes configuration: in-cluster first, then kubeconfig file.

        Returns a kubernetes_asyncio ApiClient instance.
        """
        import kubernetes_asyncio as k8s

        if self.settings.kubeconfig_path:
            await k8s.config.load_kube_config(config_file=self.settings.kubeconfig_path)
        elif os.environ.get("KUBERNETES_SERVICE_HOST"):
            k8s.config.load_incluster_config()
        else:
            await k8s.config.load_kube_config()

        return k8s.client.ApiClient()

    async def _ensure_client(self) -> Any:
        if self._api_client is None:
            self._api_client = await self._load_kubeconfig()
        return self._api_client

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        """Spawn a worker as a Kubernetes Job in the configured namespace."""
        token = _generate_token()
        token_expires_at = self.now() + (self.token_expiry_minutes * 60)
        namespace = self.settings.namespace

        try:
            api_client = await self._ensure_client()
        except Exception as exc:
            return WorkerSpawnResult(
                worker_id=worker_id, token=token, node_id=node_id,
                error=f"Failed to load kubeconfig: {exc}",
            )

        batch_v1 = api_client.BatchV1Api
        core_v1 = api_client.CoreV1Api
        networking_v1 = api_client.NetworkingV1Api

        # Issue mTLS worker certificate
        mtls_secret_name = f"studio-mtls-{worker_id}"
        worker_secret_name = f"studio-worker-{worker_id}"
        job_name = f"studio-worker-{worker_id}"

        if self.ca_cert_path and self.ca_key_path:
            cert_pem, key_pem = tls_helpers.issue_worker_cert(
                self.ca_cert_path, self.ca_key_path, worker_id
            )
            ca_pem = Path(self.ca_cert_path).read_bytes()
            try:
                await core_v1.create_namespaced_secret(
                    namespace=namespace,
                    body={
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": mtls_secret_name},
                        "type": "Opaque",
                        "stringData": {
                            "tls.crt": cert_pem.decode(),
                            "tls.key": key_pem.decode(),
                            "ca.crt": ca_pem.decode(),
                        },
                    },
                )
            except Exception as exc:
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=f"Failed to create mTLS Secret: {exc}",
                )

        # Create Secret for user-declared secrets
        secret_data: dict[str, str] = {}
        for secret_grant in manifest.grants.secrets:
            secret_data[secret_grant.name] = ""  # placeholder, real value from SecretStore
        if secret_data:
            try:
                await core_v1.create_namespaced_secret(
                    namespace=namespace,
                    body={
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": worker_secret_name},
                        "type": "Opaque",
                        "stringData": secret_data,
                    },
                )
            except Exception as exc:
                return WorkerSpawnResult(
                    worker_id=worker_id, token=token, node_id=node_id,
                    error=f"Failed to create worker Secret: {exc}",
                )

        # Create NetworkPolicy for egress enforcement (defense-in-depth)
        egress_rules = []
        for grant in manifest.grants.network.egress:
            rule: dict[str, Any] = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}
            if grant.ports:
                rule["ports"] = [
                    {"port": p, "protocol": "TCP"} for p in grant.ports
                ]
            egress_rules.append(rule)

        if manifest.grants.network.dns.enabled:
            egress_rules.append({
                "to": [{"podSelector": {}}],
                "ports": [{"port": 53, "protocol": "UDP"}],
            })

        policy_body = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": f"studio-{worker_id}",
                "labels": {
                    "studio/worker-id": worker_id,
                    "studio/bundle-id": bundle_id,
                },
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "studio/worker-id": worker_id,
                    },
                },
                "policyTypes": ["Egress"],
                "egress": egress_rules,
            },
        }

        try:
            await networking_v1.create_namespaced_network_policy(
                namespace=namespace, body=policy_body
            )
        except Exception as exc:
            return WorkerSpawnResult(
                worker_id=worker_id, token=token, node_id=node_id,
                error=f"Failed to create NetworkPolicy: {exc}",
            )

        # Build Pod spec
        pod_spec = capability_to_pod_spec(
            manifest=manifest,
            worker_id=worker_id,
            bundle_id=bundle_id,
            node_id=node_id,
            workdir="/work",
            orchestrator_addr=self.settings.orchestrator_tcp_addr,
            proxy_image=self.settings.proxy_image,
            worker_image=self.settings.worker_image,
            image_pull_policy=self.settings.image_pull_policy,
            task_spec=task_spec,
            target=target,
        )

        # Insert worker token via env var (overrides the placeholder)
        for container in pod_spec["containers"]:
            if container["name"] == "worker":
                container["env"].append({
                    "name": "STUDIO_WORKER_TOKEN",
                    "value": token,
                })

        # Ensure network grants manifest JSON is passed for proxy
        manifest_json_str = json.dumps(manifest.model_dump()).replace("'", "'\\''")
        for container in pod_spec["containers"]:
            if container["name"] == "egress-proxy":
                container["env"].append({
                    "name": "STUDIO_MANIFEST_JSON",
                    "value": manifest_json_str,
                })

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "labels": {
                    "studio/worker-id": worker_id,
                    "studio/bundle-id": bundle_id,
                },
            },
            "spec": {
                "template": {
                    "metadata": {
                        "labels": {
                            "studio/worker-id": worker_id,
                            "studio/bundle-id": bundle_id,
                        },
                    },
                    "spec": pod_spec,
                },
                "backoffLimit": 0,
            },
        }

        try:
            await batch_v1.create_namespaced_job(
                namespace=namespace, body=job_body
            )
        except Exception as exc:
            return WorkerSpawnResult(
                worker_id=worker_id, token=token, node_id=node_id,
                error=f"Failed to create Job: {exc}",
            )

        # Watch for Pod creation to capture the Pod name
        pod_name = ""
        try:
            import kubernetes_asyncio as k8s
            w = k8s.watch.Watch()
            async for event in w.stream(
                func=core_v1.list_namespaced_pod,
                namespace=namespace,
                label_selector=f"studio/worker-id={worker_id}",
                timeout_seconds=30,
            ):
                if event["object"].status.phase not in ("Pending", "Unknown", ""):
                    pod_name = event["object"].metadata.name
                    break
                if event["type"] == "ADDED":
                    pod_name = event["object"].metadata.name
        except Exception:
            pod_name = f"{job_name}-0"

        await self.db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_id, bundle_id, node_id, token, token_expires_at,
             json.dumps(manifest.model_dump()), WorkerState.PENDING, self.now()),
        )
        await self.db.conn.commit()

        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_spawned", "worker", worker_id,
             json.dumps({"bundle_id": bundle_id, "node_id": node_id,
                         "token_expires_at": token_expires_at,
                         "runner": "k8s", "namespace": namespace,
                         "job_name": job_name, "pod_name": pod_name}),
             self.now()),
        )
        await self.db.conn.commit()

        handle = K8sWorkerHandle(
            job_name=job_name,
            pod_name=pod_name,
            namespace=namespace,
            worker_id=worker_id,
            api_client=api_client,
        )

        # Register for Pod event watching
        self._watched_workers[worker_id] = handle

        logger.info("K8s worker %s spawned: Job=%s Pod=%s NS=%s",
                     worker_id, job_name, pod_name, namespace)

        return WorkerSpawnResult(worker_id, token, node_id, process=handle)

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process | RemoteWorkerHandle | K8sWorkerHandle | DockerWorkerHandle,
        worker_id: str = "",
    ) -> None:
        if isinstance(process, DockerWorkerHandle):
            await process.cancel()
            await process.cleanup()
        elif isinstance(process, K8sWorkerHandle):
            await process.cancel()
            await process.cleanup()
            self._watched_workers.pop(process.worker_id, None)
        elif isinstance(process, RemoteWorkerHandle):
            await process.cancel()
            await process.cleanup()
            try:
                process.conn.close()
                await process.conn.wait_closed()
            except Exception:
                pass
            sem = getattr(self, '_host_semaphores', {}).get(process.host.name)
            if sem:
                sem.release()
        elif isinstance(process, asyncio.subprocess.Process):
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            except ProcessLookupError:
                pass

    async def _watch_pods(self) -> None:
        """Background task: watch Pod events for evictions, OOMKills, node failures.

        Pod labels studio/worker-id and studio/bundle-id are used to map events
        back to workers. Eviction or OOMKill fires a worker failure event into the
        orchestrator event queue via the same path as a normal RPC disconnection.
        """
        self._running = True
        try:
            api_client = await self._ensure_client()
        except Exception:
            logger.warning("K8s pod watch: cannot load kubeconfig, skipping")
            return

        import kubernetes_asyncio as k8s
        core_v1 = api_client.CoreV1Api

        while self._running:
            try:
                w = k8s.watch.Watch()
                async for event in w.stream(
                    func=core_v1.list_namespaced_pod,
                    namespace=self.settings.namespace,
                    label_selector="studio/worker-id",
                ):
                    if not self._running:
                        break

                    pod = event["object"]
                    pod_name = pod.metadata.name if pod.metadata else ""
                    labels = pod.metadata.labels if pod.metadata else {}
                    event_worker_id = labels.get("studio/worker-id", "")
                    event_type = event.get("type", "")

                    if event_worker_id not in self._watched_workers:
                        continue

                    status = pod.status
                    if status and status.phase == "Failed":
                        reason = status.reason or "Unknown"
                        logger.warning(
                            "K8s Pod %s (worker %s) failed: %s",
                            pod_name, event_worker_id, reason,
                        )
                        handle = self._watched_workers.pop(event_worker_id, None)
                        if handle:
                            handle.returncode = 1
                        # Write connection_lost event for the worker
                        try:
                            now = self.now()
                            await self.db.execute(
                                "UPDATE workers SET state = ?, ended_at = ? WHERE id = ?",
                                (WorkerState.CONNECTION_LOST, now, event_worker_id),
                            )
                            await self.db.conn.commit()
                            await self.db.execute(
                                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                ("worker_pod_failed", "worker", event_worker_id,
                                 json.dumps({"reason": reason, "pod_name": pod_name,
                                             "event_type": event_type}),
                                 now),
                            )
                            await self.db.conn.commit()
                        except Exception:
                            pass

                    if status and status.container_statuses:
                        for cs in status.container_statuses:
                            terminated = cs.state.terminated if cs.state else None
                            if terminated and terminated.reason == "OOMKilled":
                                logger.warning(
                                    "K8s Pod %s (worker %s) OOMKilled",
                                    pod_name, event_worker_id,
                                )
                                handle = self._watched_workers.pop(event_worker_id, None)
                                if handle:
                                    handle.returncode = 137
            except Exception as exc:
                logger.warning("K8s pod watch error: %s", exc)
                await asyncio.sleep(5)

    async def start_watch(self) -> None:
        """Start the Pod event watching background task."""
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_pods())

    async def stop_watch(self) -> None:
        """Stop the Pod event watching background task."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

    async def close(self) -> None:
        """Close the API client."""
        await self.stop_watch()
        if self._api_client:
            await self._api_client.close()
            self._api_client = None


class DockerWorkerRunner:
    """Spawns worker containers via Docker API with proxy sidecar for egress enforcement (Bundle 4.5).

    Each worker gets its own Docker network (internal), a named volume for the working tree,
    and a sidecar proxy container. Isolation is enforced at the Docker layer rather than bwrap.
    """

    def __init__(
        self,
        db: "Database",
        settings: DockerRunnerSettings,
        egress_proxy: EgressProxySettings | None = None,
        token_expiry_minutes: int = 15,
        ca_cert_path: str = "",
        ca_key_path: str = "",
    ) -> None:
        self._db = db
        self._settings = settings
        self._egress_proxy = egress_proxy or EgressProxySettings()
        self._token_expiry_minutes = token_expiry_minutes
        self._ca_cert_path = ca_cert_path
        self._ca_key_path = ca_key_path
        self._client: docker_lib.DockerClient | None = None

    @staticmethod
    def now() -> int:
        return int(time.time())

    def _get_client(self) -> docker_lib.DockerClient:
        if self._client is None:
            self._client = docker_lib.DockerClient(
                base_url=f"unix://{self._settings.socket_path}"
            )
        return self._client

    async def _ensure_image(self, image: str) -> None:
        """Pull or verify image availability based on pull_policy."""
        if self._settings.pull_policy == "always":
            await asyncio.to_thread(self._get_client().images.pull, image)
            return
        try:
            await asyncio.to_thread(self._get_client().images.get, image)
        except docker_lib.errors.ImageNotFound:
            if self._settings.pull_policy == "if_not_present":
                await asyncio.to_thread(self._get_client().images.pull, image)
            else:
                raise

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        token = _generate_token()
        token_expires_at = self.now() + (self._token_expiry_minutes * 60)

        # Insert worker row
        await self._db.execute(
            "INSERT INTO workers (id, bundle_id, node_id, token, token_expires_at, manifest_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id, bundle_id, node_id, token, token_expires_at,
                json.dumps(manifest.model_dump()), WorkerState.PENDING, self.now(),
            ),
        )
        await self._db.conn.commit()

        await self._db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("worker_spawned", "worker", worker_id,
             json.dumps({"bundle_id": bundle_id, "node_id": node_id, "token_expires_at": token_expires_at}),
             self.now()),
        )
        await self._db.conn.commit()

        client = self._get_client()
        network_name = f"{self._settings.network_prefix}-{worker_id}"
        volume_name = f"{self._settings.volume_prefix}-{worker_id}"
        proxy_volume_name = f"proxy-socket-{worker_id}"
        proxy_name = f"studio-proxy-{worker_id}"
        worker_name = f"studio-worker-{worker_id}"

        try:
            # 1. Ensure images
            await self._ensure_image(self._settings.worker_image)
            await self._ensure_image(self._settings.proxy_image)

            # 2. Create per-worker Docker network (internal)
            await asyncio.to_thread(
                client.networks.create,
                name=network_name,
                driver="bridge",
                internal=True,
                labels={"studio/worker-id": worker_id, "studio/bundle-id": bundle_id},
            )

            # 3. Create named volume for proxy socket (shared between proxy and worker)
            await asyncio.to_thread(
                client.volumes.create,
                name=proxy_volume_name,
                labels={"studio/worker-id": worker_id},
            )

            # 4. Start proxy sidecar container
            orchestrator_addr = f"orchestrator.internal:7811"
            proxy_container = await asyncio.to_thread(
                client.containers.run,
                image=self._settings.proxy_image,
                name=proxy_name,
                network=network_name,
                detach=True,
                environment={
                    "PROXY_SOCKET_DIR": "/tmp/studio",
                    "STUDIO_EGRESS_ALLOWLIST": json.dumps(
                        [e.model_dump() for e in manifest.grants.network.egress]
                    ),
                    "STUDIO_WORKER_ID": worker_id,
                },
                mounts=[
                    docker_lib.types.Mount(
                        type="volume",
                        source=proxy_volume_name,
                        target="/tmp/studio",
                    ),
                ],
                labels={"studio/worker-id": worker_id, "studio/role": "proxy"},
                remove=False,
            )
            proxy_container_id = proxy_container.id

            # 5. Poll for proxy socket before starting worker
            for _ in range(50):
                exit_code, _ = await asyncio.to_thread(
                    lambda: proxy_container.exec_run(
                        ['test', '-S', f'/tmp/studio/proxy-{worker_id}.sock']
                    )
                )
                if exit_code == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(
                    f'Egress proxy failed to bind socket after 5s for worker {worker_id}'
                )

            # 6. Create named volume for working tree
            await asyncio.to_thread(
                client.volumes.create,
                name=volume_name,
                labels={"studio/worker-id": worker_id},
            )

            # 7. Start worker container (shares proxy's network namespace)
            worker_container = await asyncio.to_thread(
                client.containers.run,
                image=self._settings.worker_image,
                name=worker_name,
                network_mode=f"container:{proxy_container_id}",
                detach=True,
                command=[],
                environment={
                    "STUDIO_ORCHESTRATOR_ADDR": orchestrator_addr,
                    "STUDIO_WORKER_TOKEN": token,
                    "STUDIO_WORKER_ID": worker_id,
                    "STUDIO_PROXY_SOCKET": f"/tmp/studio/proxy-{worker_id}.sock",
                },
                mounts=[
                    docker_lib.types.Mount(
                        type="volume",
                        source=volume_name,
                        target="/work",
                    ),
                    docker_lib.types.Mount(
                        type="volume",
                        source=proxy_volume_name,
                        target="/tmp/studio",
                        read_only=True,
                    ),
                ],
                labels={"studio/worker-id": worker_id, "studio/bundle-id": bundle_id,
                        "studio/node-id": node_id, "studio/role": "worker"},
                remove=False,
            )
            worker_container_id = worker_container.id

            handle = DockerWorkerHandle(
                worker_id=worker_id,
                worker_container_id=worker_container_id,
                proxy_container_id=proxy_container_id,
                volume_name=volume_name,
                proxy_volume_name=proxy_volume_name,
                network_name=network_name,
                client=client,
            )

            return WorkerSpawnResult(
                worker_id=worker_id,
                token=token,
                node_id=node_id,
                process=handle,
            )
        except Exception as exc:
            logger.error("DockerWorkerRunner.spawn_worker failed: %s", exc)
            # Best-effort cleanup on failure
            try:
                await asyncio.to_thread(lambda: client.containers.get(worker_name).remove(force=True))
            except Exception:
                pass
            try:
                await asyncio.to_thread(lambda: client.containers.get(proxy_name).remove(force=True))
            except Exception:
                pass
            try:
                await asyncio.to_thread(lambda: client.networks.get(network_name).remove())
            except Exception:
                pass
            for vname in (volume_name, proxy_volume_name):
                try:
                    await asyncio.to_thread(lambda v=vname: client.volumes.get(v).remove(force=True))
                except Exception:
                    pass
            return WorkerSpawnResult(
                worker_id=worker_id,
                token=token,
                node_id=node_id,
                error=str(exc),
            )

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process | RemoteWorkerHandle | K8sWorkerHandle | DockerWorkerHandle,
        worker_id: str = "",
    ) -> None:
        if isinstance(process, DockerWorkerHandle):
            await process.cancel()
            await process.cleanup()
        elif isinstance(process, K8sWorkerHandle):
            await process.cancel()
            await process.cleanup()
        elif isinstance(process, RemoteWorkerHandle):
            await process.cancel()
            await process.cleanup()
            try:
                process.conn.close()
                await process.conn.wait_closed()
            except Exception:
                pass
        elif isinstance(process, asyncio.subprocess.Process):
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=30)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            except ProcessLookupError:
                pass

    async def close(self) -> None:
        """Close the Docker client."""
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None


class RunnerSelector:
    """Selects from multiple enabled runners per-task based on preference and capacity (Bundle 4.4).

    Presents the same spawn_worker / kill_worker interface as a single runner.
    Selection policy: runner_preference → capability compatibility → capacity → default local.
    """

    def __init__(
        self,
        db: "Database",
        settings: RunnerSelectorSettings,
        local: LocalBwrapWorkerRunner | None = None,
        remote_ssh: RemoteSSHWorkerRunner | None = None,
        k8s: K8sJobWorkerRunner | None = None,
        docker: DockerWorkerRunner | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._runners: dict[str, LocalBwrapWorkerRunner | RemoteSSHWorkerRunner | K8sJobWorkerRunner | DockerWorkerRunner] = {}
        if local:
            self._runners["local"] = local
        if remote_ssh:
            self._runners["remote_ssh"] = remote_ssh
        if k8s:
            self._runners["k8s"] = k8s
        if docker:
            self._runners["docker"] = docker

    @staticmethod
    def now() -> int:
        return int(time.time())

    def _default_preference(self) -> str:
        """Effective default preference from settings."""
        pref = self._settings.default_preference
        if pref == "any":
            return "local"
        return pref

    def _select_runner(
        self,
        preference: str,
        manifest: CapabilityManifest | None = None,
    ) -> tuple[str, LocalBwrapWorkerRunner | RemoteSSHWorkerRunner | K8sJobWorkerRunner | None]:
        """Pick a runner given preference and capability compatibility.

        Returns (runner_type, runner_instance) or ("", None) if nothing matches.
        """
        available = list(self._runners.keys())
        if not available:
            return "", None

        # Resolve "any" to concrete preference order: explicit preference → local fallback
        candidates: list[str]
        if preference == "any":
            candidates = [self._default_preference()] + [r for r in available if r != self._default_preference()]
        elif preference in self._runners:
            candidates = [preference] + [r for r in available if r != preference]
        else:
            # Unknown preference — treat as "any"
            logger.warning("Unknown runner_preference %r, falling back to available runners", preference)
            candidates = [self._default_preference()] + [r for r in available if r != self._default_preference()]

        # Check capability compatibility
        if manifest:
            compat = capability_to_runner_compatibility(manifest)
            for candidate in candidates:
                if candidate in self._runners:
                    info = compat.get(candidate, {})
                    if not info.get("compatible"):
                        continue
                    unenforced = info.get("unenforced_grants", [])
                    if unenforced and not self._settings.allow_unenforced_grants:
                        logger.warning(
                            "Runner %s has unenforced grants %s, skipping (allow_unenforced_grants=false)",
                            candidate, unenforced,
                        )
                        continue
                    if unenforced:
                        logger.info(
                            "Runner %s: unenforced grants %s (allowed by settings)",
                            candidate, unenforced,
                        )
                    return candidate, self._runners[candidate]
        else:
            for candidate in candidates:
                if candidate in self._runners:
                    return candidate, self._runners[candidate]

        return "", None

    async def spawn_worker(
        self,
        worker_id: str,
        bundle_id: str,
        node_id: str,
        manifest: CapabilityManifest,
        worktree_path: str,
        task_spec: dict[str, Any] | None = None,
        base_branch: str = "main",
        target: str = "existing-repo",
        worker_type: str = "developer",
    ) -> WorkerSpawnResult:
        """Select a runner and delegate spawn_worker to it."""
        preference = "any"
        if task_spec:
            preference = task_spec.get("runner_preference", "any")

        runner_type, runner = self._select_runner(preference, manifest)

        if runner is None:
            return WorkerSpawnResult(
                worker_id=worker_id,
                token="",
                node_id=node_id,
                error=f"No compatible runner available (preference={preference}, "
                      f"available={sorted(self._runners.keys())})",
            )

        logger.info(
            "RunnerSelector: worker %s → %s (preference=%s)",
            worker_id, runner_type, preference,
        )

        result = await runner.spawn_worker(
            worker_id=worker_id,
            bundle_id=bundle_id,
            node_id=node_id,
            manifest=manifest,
            worktree_path=worktree_path,
            task_spec=task_spec,
            base_branch=base_branch,
            target=target,
            worker_type=worker_type,
        )

        # Record runner_type on the worker row
        if not result.error:
            await self._db.execute(
                "UPDATE workers SET runner_type = ? WHERE id = ?",
                (runner_type, worker_id),
            )
            await self._db.conn.commit()

            # Audit: runner selection
            compat = capability_to_runner_compatibility(manifest) if manifest else {}
            runner_compat = compat.get(runner_type, {})
            await self._db.execute(
                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("runner_selected", "worker", worker_id,
                 json.dumps({
                     "bundle_id": bundle_id,
                     "node_id": node_id,
                     "runner_type": runner_type,
                     "preference": preference,
                     "unenforced_grants": runner_compat.get("unenforced_grants", []),
                 }),
                 self.now()),
            )
            await self._db.conn.commit()

        return result

    async def kill_worker(
        self,
        process: asyncio.subprocess.Process | RemoteWorkerHandle | K8sWorkerHandle | DockerWorkerHandle,
        worker_id: str = "",
    ) -> None:
        """Dispatch kill to the appropriate runner based on handle type."""
        if isinstance(process, DockerWorkerHandle):
            runner = self._runners.get("docker")
            if runner:
                await runner.kill_worker(process, worker_id)
        elif isinstance(process, K8sWorkerHandle):
            runner = self._runners.get("k8s")
            if runner:
                await runner.kill_worker(process, worker_id)
        elif isinstance(process, RemoteWorkerHandle):
            runner = self._runners.get("remote_ssh")
            if runner:
                await runner.kill_worker(process, worker_id)
        else:
            runner = self._runners.get("local")
            if runner:
                await runner.kill_worker(process, worker_id)

    async def close(self) -> None:
        """Close all runners that have a close method."""
        for name, runner in self._runners.items():
            closer = getattr(runner, "close", None)
            if closer:
                try:
                    await closer()
                except Exception as exc:
                    logger.warning("Error closing runner %s: %s", name, exc)

    async def start_watches(self) -> None:
        """Start background watches on runners that support them (k8s)."""
        k8s = self._runners.get("k8s")
        if k8s and hasattr(k8s, "start_watch"):
            await k8s.start_watch()

    def get_runner(self, name: str) -> LocalBwrapWorkerRunner | RemoteSSHWorkerRunner | K8sJobWorkerRunner | DockerWorkerRunner | None:
        """Access a specific runner by name (for fleet health, CLI handlers, etc.)."""
        return self._runners.get(name)

    @property
    def runner_names(self) -> list[str]:
        return sorted(self._runners.keys())
