"""Worker runner: spawns isolated worker subprocesses with bubblewrap.

Phase 3: always-unshare-net with per-worker egress proxy for network enforcement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

from .models import WorkerState, NodeState, CapabilityManifest, EgressProxySettings, RemoteFleetSettings, FleetHost
from . import tls as tls_helpers

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


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

    # Network: always isolate — no host network
    args.append("--unshare-net")

    # Proc
    args.extend(["--proc", "/proc"])

    # Dev
    args.extend(["--dev", "/dev"])

    return args


class WorkerSpawnResult:
    def __init__(
        self,
        worker_id: str,
        token: str,
        node_id: str,
        process: asyncio.subprocess.Process | "RemoteWorkerHandle | None" = None,
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
        self.worker_command = worker_command or ["studio-worker"]
        self.token_expiry_minutes = token_expiry_minutes
        self.ca_cert_path = ca_cert_path
        self.ca_key_path = ca_key_path
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

        # Build bwrap args (always --unshare-net)
        bwrap_args = capability_to_bwrap_args(manifest, worktree_path, self.socket_path, proxy_socket)

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

        # mTLS cert paths for TCP connections (Bundle 4.1 mTLS)
        if worker_cert_path:
            worker_env["STUDIO_WORKER_CERT"] = worker_cert_path
            worker_env["STUDIO_WORKER_KEY"] = worker_key_path
            worker_env["STUDIO_ORCHESTRATOR_CA"] = self.ca_cert_path

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
        self.worker_command = worker_command or ["studio-worker"]
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
            await asyncio.sleep(0.5)

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
                "STUDIO_PROXY_SOCKET": proxy_socket,
                "http_proxy": f"http+unix://{proxy_socket.replace('/', '%2F')}",
                "https_proxy": f"http+unix://{proxy_socket.replace('/', '%2F')}",
                "no_proxy": "",
            }

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

            cmd_parts = bwrap_args + self.worker_command
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
        process: asyncio.subprocess.Process | RemoteWorkerHandle,
        worker_id: str = "",
    ) -> None:
        """Kill a remote worker by its handle."""
        if isinstance(process, RemoteWorkerHandle):
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
