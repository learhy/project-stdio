"""Firecracker microVM infrastructure (Phase 7 Bundle 7.1).

Provides VmPool for pre-warmed Firecracker microVMs, FirecrackerVm for single-VM
lifecycle, and utilities for building rootfs images and downloading kernels.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from studio.orchestrator import models

_logger = logging.getLogger(__name__)

# Firecracker API socket path pattern
_FC_API_SOCKET = "/run/studio/firecracker-{vm_id}.api"
# vsock host-side Unix socket path pattern
_FC_VSOCK_PATH = "/run/studio/firecracker-{vm_id}.vsock"
# Guest agent listens on vsock port 52
_FC_AGENT_PORT = 52
# Firecracker kernel download URL (x86_64, v1.7)
_FC_KERNEL_URL = (
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.7/x86_64/vmlinux-5.10"
)
# Expected SHA256 of the v1.7 kernel binary (supply chain verification)
_FC_KERNEL_V1_7_SHA256 = ""
# CID base for VMs (host is 2, guests start at 3)
_FC_CID_BASE = 3

# ── Data structures ────────────────────────────────────────────────────────────


@dataclass
class FirecrackerVmConfig:
    vcpus: int = 1
    memory_mb: int = 512
    kernel_path: str = "/var/lib/studio/firecracker/vmlinux"
    rootfs_path: str = "/var/lib/studio/firecracker/rootfs.ext4"
    tap_bridge: str = "studio-fc-br0"
    ip_range: str = "172.16.0.0/24"
    jailer_enabled: bool = False
    jailer_chroot_base: str = "/var/lib/studio/firecracker/jailer"
    seccomp_filter_path: str = ""
    reset_mode: str = "reboot"  # "reboot" | "overlay_only"
    firecracker_binary: str = "firecracker"


@dataclass
class _AgentResponse:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ── Guest agent protocol ───────────────────────────────────────────────────────


def _build_agent_frame(cmd: str, **kwargs: Any) -> bytes:
    payload = json.dumps({"cmd": cmd, **kwargs})
    # 4-byte big-endian length prefix + JSON payload
    return len(payload).to_bytes(4, "big") + payload.encode()


def _parse_agent_response(data: bytes) -> _AgentResponse:
    try:
        obj = json.loads(data.decode())
        return _AgentResponse(ok=obj.get("ok", False), data=obj, error=obj.get("error", ""))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _AgentResponse(ok=False, error=str(exc))


# ── FirecrackerVm ──────────────────────────────────────────────────────────────


class FirecrackerVm:
    """Manages a single Firecracker microVM lifecycle via the HTTP API."""

    def __init__(self, vm_id: int, config: FirecrackerVmConfig, privileged: bool = False):
        self.vm_id = vm_id
        self.config = config
        self.privileged = privileged
        self._cid = _FC_CID_BASE + vm_id
        self._api_path = _FC_API_SOCKET.format(vm_id=vm_id)
        self._vsock_path = _FC_VSOCK_PATH.format(vm_id=vm_id)
        self._process: asyncio.subprocess.Process | None = None
        self._agent_reader: asyncio.StreamReader | None = None
        self._agent_writer: asyncio.StreamWriter | None = None
        self._rootfs_overlay_path: str | None = None
        self._worktree_drive_path: str | None = None
        self._worktree_host_path: str | None = None
        self._agent_lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot the Firecracker microVM and wait for the guest agent."""
        # Prepare overlay drive (tmpfs-backed writable layer on top of rootfs)
        self._rootfs_overlay_path = await self._create_overlay_drive()

        # Launch firecracker process
        if self.config.jailer_enabled:
            await self._start_jailed()
        else:
            await self._start_direct()

        # Wait for API socket
        await self._wait_api_socket()

        # Configure VM
        async with httpx.AsyncClient() as client:
            await self._api_configure_machine(client)
            await self._api_configure_drives(client)
            await self._api_configure_vsock(client)
            await self._api_configure_network(client)
            await self._api_configure_boot(client)
            await self._api_start(client)

        # Connect to guest agent via vsock
        await self._connect_agent()

    async def stop(self) -> None:
        """Stop the Firecracker microVM."""
        if self._agent_writer:
            try:
                self._agent_writer.close()
            except Exception:
                pass
            self._agent_writer = None
            self._agent_reader = None

        if self._process and self._process.returncode is None:
            try:
                # Send CTRL+ALT+DEL via the API
                async with httpx.AsyncClient() as client:
                    await client.put(
                        f"http://unix{self._api_path}/actions",
                        json={"action_type": "SendCtrlAltDel"},
                        timeout=5,
                    )
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._cleanup_temp_files()

    async def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        use_bwrap: bool = False,
        bwrap_args: list[str] | None = None,
    ) -> int:
        """Launch a process inside the VM via the guest agent. Returns guest PID.

        When use_bwrap is True, the guest agent wraps the command in bubblewrap
        with the provided arguments for exec allowlist enforcement.
        """
        kwargs: dict[str, Any] = {"argv": argv, "env": env or {}}
        if use_bwrap and bwrap_args:
            kwargs["bwrap"] = {"use_bwrap": True, "bwrap_args": bwrap_args}
        resp = await self._agent_send("exec", **kwargs)
        if not resp.ok:
            raise RuntimeError(f"Guest agent exec failed: {resp.error}")
        return resp.data.get("pid", -1)

    async def exec_signal(self, pid: int, signal: str = "TERM") -> None:
        """Send a signal to a process inside the VM."""
        resp = await self._agent_send("signal", pid=pid, signal=signal)
        if not resp.ok:
            raise RuntimeError(f"Guest agent signal failed: {resp.error}")

    async def is_process_running(self, pid: int) -> bool:
        """Check if a process is still running inside the VM."""
        resp = await self._agent_send("is_running", pid=pid)
        return resp.data.get("running", False)

    async def reset(self) -> None:
        """Reset the VM to a clean state and return to pool."""
        if self.config.reset_mode == "reboot":
            await self.stop()
            await self.start()
        else:
            # overlay_only: ask agent to reset overlay, no reboot
            try:
                await self._agent_send("reset")
            except Exception:
                _logger.warning("Guest agent reset failed, falling back to reboot")
                await self.stop()
                await self.start()

    async def mount_worktree(self, host_path: str) -> None:
        """Pre-bake a worktree into a second ext4 drive (/dev/vdb)."""
        self._worktree_host_path = host_path
        self._worktree_drive_path = await self._create_worktree_drive(host_path)

    async def extract_worktree_changes(self) -> None:
        """Copy changed files from the worktree drive back to the host."""
        if not self._worktree_drive_path or not self._worktree_host_path:
            return
        # Mount the drive and rsync changes back
        with tempfile.TemporaryDirectory() as mount_point:
            subprocess.run(
                ["sudo", "mount", "-o", "loop", self._worktree_drive_path, mount_point],
                capture_output=True,
                check=False,
            )
            try:
                subprocess.run(
                    ["rsync", "-a", "--delete", f"{mount_point}/", self._worktree_host_path],
                    capture_output=True,
                    check=False,
                )
            finally:
                subprocess.run(
                    ["sudo", "umount", mount_point],
                    capture_output=True,
                    check=False,
                )

    # ── Firecracker API helpers ────────────────────────────────────────────

    async def _api_configure_machine(self, client: httpx.AsyncClient) -> None:
        await client.put(
            f"http://unix{self._api_path}/machine-config",
            json={"vcpu_count": self.config.vcpus, "mem_size_mib": self.config.memory_mb},
            timeout=5,
        )

    async def _api_configure_drives(self, client: httpx.AsyncClient) -> None:
        # Root drive (read-only shared rootfs + writable overlay)
        await client.put(
            f"http://unix{self._api_path}/drives/rootfs",
            json={
                "drive_id": "rootfs",
                "path_on_host": self._rootfs_overlay_path or self.config.rootfs_path,
                "is_root_device": True,
                "is_read_only": self._rootfs_overlay_path is None,
            },
            timeout=5,
        )
        # Worktree drive (pre-baked ext4, if available)
        if self._worktree_drive_path:
            await client.put(
                f"http://unix{self._api_path}/drives/worktree",
                json={
                    "drive_id": "worktree",
                    "path_on_host": self._worktree_drive_path,
                    "is_root_device": False,
                    "is_read_only": False,
                },
                timeout=5,
            )

    async def _api_configure_vsock(self, client: httpx.AsyncClient) -> None:
        await client.put(
            f"http://unix{self._api_path}/vsock",
            json={"guest_cid": self._cid, "uds_path": self._vsock_path},
            timeout=5,
        )

    async def _api_configure_network(self, client: httpx.AsyncClient) -> None:
        await client.put(
            f"http://unix{self._api_path}/network-interfaces/eth0",
            json={
                "iface_id": "eth0",
                "guest_mac": f"AA:FC:{self._cid:02X}:00:00:01",
                "host_dev_name": f"{self.config.tap_bridge}-{self.vm_id}",
            },
            timeout=5,
        )

    async def _api_configure_boot(self, client: httpx.AsyncClient) -> None:
        boot_args = (
            "console=ttyS0 reboot=k panic=1 pci=off "
            "init=/sbin/init root=/dev/vda rw quiet"
        )
        await client.put(
            f"http://unix{self._api_path}/boot-source",
            json={"kernel_image_path": self.config.kernel_path, "boot_args": boot_args},
            timeout=5,
        )

    async def _api_start(self, client: httpx.AsyncClient) -> None:
        await client.put(
            f"http://unix{self._api_path}/actions",
            json={"action_type": "InstanceStart"},
            timeout=5,
        )

    def _resolve_jailer_uid_gid(self) -> tuple[int, int]:
        """Resolve uid/gid for jailer. Uses studio system user if available."""
        try:
            import pwd
            studio_user = pwd.getpwnam("studio")
            return studio_user.pw_uid, studio_user.pw_gid
        except (ImportError, KeyError):
            return os.getuid(), os.getgid()

    async def _start_direct(self) -> None:
        """Launch firecracker directly (no jailer)."""
        cmd = [
            self.config.firecracker_binary,
            "--api-sock", self._api_path,
        ]
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _start_jailed(self) -> None:
        """Launch firecracker via the jailer binary (chroot + cgroups + seccomp)."""
        uid, gid = self._resolve_jailer_uid_gid()
        jailer_id = str(self.vm_id)

        # Jailer creates chroot at {chroot_base}/firecracker/{id}/root/
        # The API socket lives inside the chroot at /run/firecracker.socket
        chroot_api_socket = f"{self.config.jailer_chroot_base}/firecracker/{jailer_id}/root/run/firecracker.socket"
        self._api_path = chroot_api_socket

        cmd = [
            "jailer",
            "--id", jailer_id,
            "--exec-file", shutil.which(self.config.firecracker_binary) or "/usr/bin/firecracker",
            "--uid", str(uid),
            "--gid", str(gid),
            "--chroot-base-dir", self.config.jailer_chroot_base,
            "--",
            "--api-sock", "/run/firecracker.socket",
        ]
        if self.config.seccomp_filter_path:
            cmd.insert(cmd.index("--"), f"--seccomp-filter={self.config.seccomp_filter_path}")

        _logger.info("Starting jailed Firecracker: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _wait_api_socket(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self._api_path):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"Firecracker API socket {self._api_path} did not appear")

    # ── Guest agent communication ──────────────────────────────────────────

    async def _connect_agent(self, timeout: float = 15.0) -> None:
        """Connect to the guest agent via the vsock Unix socket."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self._vsock_path):
                break
            if self._process and self._process.returncode is not None:
                stderr = (await self._process.stderr.read()).decode() if self._process.stderr else ""
                raise RuntimeError(f"Firecracker exited before vsock ready: {stderr[:500]}")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"vsock socket {self._vsock_path} did not appear")

        reader, writer = await asyncio.open_unix_connection(self._vsock_path)
        self._agent_reader = reader
        self._agent_writer = writer

    async def _agent_send(self, cmd: str, **kwargs: Any) -> _AgentResponse:
        """Send a JSON command to the guest agent and read the response."""
        async with self._agent_lock:
            if not self._agent_writer:
                raise RuntimeError("Agent not connected")
            frame = _build_agent_frame(cmd, **kwargs)
            self._agent_writer.write(frame)
            await self._agent_writer.drain()

            # Read 4-byte length prefix
            len_bytes = await self._agent_reader.readexactly(4)
            payload_len = int.from_bytes(len_bytes, "big")
            payload = await self._agent_reader.readexactly(payload_len)
            return _parse_agent_response(payload)

    # ── Drive management ────────────────────────────────────────────────────

    async def _create_overlay_drive(self) -> str:
        """Create a tmpfs-backed ext4 overlay drive for this VM."""
        overlay_path = f"/run/studio/firecracker-overlay-{self.vm_id}.ext4"
        # Create a small writable ext4 image for the overlay upper dir
        size_mb = max(256, self.config.memory_mb // 2)
        proc = await asyncio.create_subprocess_exec(
            "truncate", "-s", f"{size_mb}M", overlay_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        proc = await asyncio.create_subprocess_exec(
            "mkfs.ext4", "-F", "-q", overlay_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return overlay_path

    async def _create_worktree_drive(self, host_path: str) -> str:
        """Pre-bake a worktree directory into an ext4 drive image."""
        worktree_img = f"/run/studio/firecracker-worktree-{self.vm_id}.ext4"
        # Estimate size: du of host_path + 64MB headroom
        try:
            du = subprocess.run(
                ["du", "-sm", host_path], capture_output=True, text=True, check=False
            )
            size_mb = int(du.stdout.split()[0]) + 64 if du.returncode == 0 else 128
        except (ValueError, IndexError):
            size_mb = 128

        proc = await asyncio.create_subprocess_exec(
            "truncate", "-s", f"{size_mb}M", worktree_img,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        proc = await asyncio.create_subprocess_exec(
            "mkfs.ext4", "-F", "-q", worktree_img,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        # Copy worktree contents into the image
        subprocess.run(
            ["sudo", "mount", "-o", "loop", worktree_img, "/mnt"],
            capture_output=True,
            check=False,
        )
        try:
            subprocess.run(
                ["sudo", "cp", "-a", f"{host_path}/.", "/mnt/"],
                capture_output=True,
                check=False,
            )
        finally:
            subprocess.run(
                ["sudo", "umount", "/mnt"],
                capture_output=True,
                check=False,
            )

        return worktree_img

    def _cleanup_temp_files(self) -> None:
        for path in (self._rootfs_overlay_path, self._worktree_drive_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ── VmPool ─────────────────────────────────────────────────────────────────────


class VmPool:
    """Pool of pre-warmed Firecracker microVMs with optional privileged sub-pool (Bundle 7.5)."""

    def __init__(
        self,
        pool_size: int,
        rootfs_path: str,
        kernel_path: str,
        config: FirecrackerVmConfig | None = None,
        privileged_pool_size: int = 1,
    ):
        self._pool_size = pool_size
        self._privileged_pool_size = privileged_pool_size
        self._rootfs_path = rootfs_path
        self._kernel_path = kernel_path
        self._config = config or FirecrackerVmConfig(
            kernel_path=kernel_path,
            rootfs_path=rootfs_path,
        )
        self._available: asyncio.Queue[FirecrackerVm] = asyncio.Queue(maxsize=pool_size)
        self._all_vms: list[FirecrackerVm] = []
        self._next_vm_id = 0
        self._running = False
        # Bundle 7.5: privileged sub-pool
        self._privileged_available: asyncio.Queue[FirecrackerVm] = asyncio.Queue(maxsize=privileged_pool_size)
        self._privileged_vms: list[FirecrackerVm] = []

    async def start(self) -> None:
        """Pre-warm both standard and privileged pools at startup."""
        self._running = True
        _logger.info("VmPool: pre-warming %d standard VMs...", self._pool_size)
        for _ in range(self._pool_size):
            vm = await self._create_and_boot_vm(privileged=False)
            self._all_vms.append(vm)
            await self._available.put(vm)
        _logger.info("VmPool: %d standard VMs ready", self._pool_size)

        if self._privileged_pool_size > 0:
            _logger.info("VmPool: pre-warming %d privileged VMs...", self._privileged_pool_size)
            for _ in range(self._privileged_pool_size):
                vm = await self._create_and_boot_vm(privileged=True)
                self._privileged_vms.append(vm)
                await self._privileged_available.put(vm)
            _logger.info("VmPool: %d privileged VMs ready", self._privileged_pool_size)

    async def acquire(self, privileged: bool = False) -> FirecrackerVm:
        """Get a pre-warmed VM from the appropriate pool, or cold-start one if empty."""
        if not self._running:
            raise RuntimeError("VmPool not started")
        queue = self._privileged_available if privileged else self._available
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            _logger.info("VmPool: %s pool empty, cold-starting new VM",
                         "privileged" if privileged else "standard")
            return await self._create_and_boot_vm(privileged=privileged)

    async def release(self, vm: FirecrackerVm) -> None:
        """Reset VM and return it to the correct pool, or discard if pool was shrunk."""
        privileged = vm.privileged
        all_vms = self._privileged_vms if privileged else self._all_vms
        pool_size = self._privileged_pool_size if privileged else self._pool_size

        # If pool was shrunk, discard excess
        if len(all_vms) > pool_size:
            _logger.info("VmPool: discarding excess %s VM (pool shrunk to %d)",
                         "privileged" if privileged else "standard", pool_size)
            try:
                await vm.stop()
            except Exception:
                pass
            if vm in all_vms:
                all_vms.remove(vm)
            return

        try:
            await vm.reset()
        except Exception as exc:
            _logger.warning("VmPool: VM reset failed: %s, discarding VM", exc)
            try:
                await vm.stop()
            except Exception:
                pass
            vm = await self._create_and_boot_vm(privileged=privileged)

        if self._running:
            queue = self._privileged_available if privileged else self._available
            await queue.put(vm)

    async def stop(self) -> None:
        """Shut down all VMs in both pools."""
        self._running = False

        _logger.info("VmPool: stopping %d standard VMs...", len(self._all_vms))
        for vm in self._all_vms:
            try:
                await vm.stop()
            except Exception as exc:
                _logger.warning("VmPool: VM stop error: %s", exc)
        while not self._available.empty():
            try:
                vm = self._available.get_nowait()
                await vm.stop()
            except Exception:
                pass
        self._all_vms.clear()

        _logger.info("VmPool: stopping %d privileged VMs...", len(self._privileged_vms))
        for vm in self._privileged_vms:
            try:
                await vm.stop()
            except Exception as exc:
                _logger.warning("VmPool: privileged VM stop error: %s", exc)
        while not self._privileged_available.empty():
            try:
                vm = self._privileged_available.get_nowait()
                await vm.stop()
            except Exception:
                pass
        self._privileged_vms.clear()

    async def resize(self, new_size: int) -> dict[str, Any]:
        """Resize the standard pool at runtime."""
        if new_size < 0:
            raise ValueError(f"pool_size must be >= 0, got {new_size}")
        old_size = self._pool_size
        self._pool_size = new_size

        result: dict[str, Any] = {"old_size": old_size, "new_size": new_size, "started": 0, "drained": 0, "action": "noop"}

        existing: list[FirecrackerVm] = []
        while not self._available.empty():
            try:
                existing.append(self._available.get_nowait())
            except asyncio.QueueEmpty:
                break

        if new_size > old_size:
            to_create = new_size - old_size
            _logger.info("VmPool: growing from %d to %d (creating %d VMs)", old_size, new_size, to_create)
            for _ in range(to_create):
                vm = await self._create_and_boot_vm(privileged=False)
                self._all_vms.append(vm)
                existing.append(vm)
            result["started"] = to_create
            result["action"] = "grown"
        elif new_size < old_size:
            _logger.info("VmPool: shrinking from %d to %d", old_size, new_size)
            result["action"] = "shrinking"
            excess = len(existing) - new_size
            while excess > 0 and existing:
                vm = existing.pop()
                await vm.stop()
                if vm in self._all_vms:
                    self._all_vms.remove(vm)
                excess -= 1
                result["drained"] += 1
            _logger.info("VmPool: drained %d idle VMs", result["drained"])

        self._available = asyncio.Queue(maxsize=new_size)
        for vm in existing:
            await self._available.put(vm)

        return result

    @property
    def pool_size(self) -> int:
        return self._pool_size

    @property
    def privileged_pool_size(self) -> int:
        return self._privileged_pool_size

    async def _create_and_boot_vm(self, privileged: bool = False) -> FirecrackerVm:
        vm_id = self._next_vm_id
        self._next_vm_id += 1
        config = FirecrackerVmConfig(
            vcpus=self._config.vcpus,
            memory_mb=self._config.memory_mb,
            kernel_path=self._config.kernel_path,
            rootfs_path=self._config.rootfs_path,
            tap_bridge=self._config.tap_bridge,
            ip_range=self._config.ip_range,
            jailer_enabled=self._config.jailer_enabled,
            reset_mode=self._config.reset_mode,
            firecracker_binary=self._config.firecracker_binary,
        )
        vm = FirecrackerVm(vm_id=vm_id, config=config, privileged=privileged)
        await vm.start()
        return vm


# ── Utility: build rootfs from Docker ───────────────────────────────────────────


async def build_rootfs(output_path: str, no_cache: bool = False) -> dict[str, Any]:
    """Build worker rootfs ext4 image from docker/Dockerfile.worker.

    Steps:
    1. Build Docker image
    2. Export container filesystem to temp directory
    3. Create ext4 image using mkfs.ext4
    Returns dict with path, size_bytes, sha256.
    """
    dockerfile = Path("docker/Dockerfile.worker")
    if not dockerfile.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")

    image_tag = "project-stdio-worker:latest"
    _logger.info("Building Docker image %s...", image_tag)

    build_args = ["docker", "build", "-t", image_tag, "-f", str(dockerfile), "."]
    if no_cache:
        build_args.append("--no-cache")

    proc = await asyncio.create_subprocess_exec(
        *build_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Docker build failed: {stderr.decode()[:1000]}")

    # Export container filesystem to a temp directory
    _logger.info("Exporting container filesystem...")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a container from the image and export it
        proc = await asyncio.create_subprocess_exec(
            "docker", "create", image_tag,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        container_id, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Docker create failed: {stderr.decode()[:500]}")
        container_id = container_id.decode().strip()

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "export", container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            tar_stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Docker export failed: {stderr.decode()[:500]}")

            # Extract tar to temp directory
            extract_dir = os.path.join(tmpdir, "rootfs")
            os.makedirs(extract_dir)
            proc = await asyncio.create_subprocess_exec(
                "tar", "-xf", "-", "-C", extract_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate(input=tar_stdout)
            if proc.returncode != 0:
                raise RuntimeError("Failed to extract container filesystem")

            # Generate rootfs manifest: hash all binaries for content-hash exec verification
            manifest: dict[str, str] = {}
            bin_dirs = ["usr/bin", "usr/local/bin", "usr/sbin", "sbin", "bin"]
            for bin_dir in bin_dirs:
                walk_dir = os.path.join(extract_dir, bin_dir)
                if not os.path.isdir(walk_dir):
                    continue
                for fname in os.listdir(walk_dir):
                    fpath = os.path.join(walk_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    if os.path.islink(fpath):
                        continue
                    try:
                        with open(fpath, "rb") as bf:
                            file_hash = hashlib.sha256(bf.read()).hexdigest()
                        manifest[f"/{bin_dir}/{fname}"] = file_hash
                    except (OSError, PermissionError):
                        pass
            manifest_path = output_path + "-manifest.json"
            Path(manifest_path).write_text(json.dumps(manifest, indent=2))
            _logger.info("Rootfs manifest written to %s (%d binaries)", manifest_path, len(manifest))

            # Calculate directory size
            du = subprocess.run(
                ["du", "-sm", extract_dir], capture_output=True, text=True, check=False
            )
            size_mb = int(du.stdout.split()[0]) + 128  # 128MB headroom

            # Create ext4 image
            _logger.info("Creating ext4 image (%d MB)...", size_mb)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["truncate", "-s", f"{size_mb}M", output_path],
                check=True,
            )
            subprocess.run(
                ["mkfs.ext4", "-F", "-q", output_path],
                check=True,
            )
            # Use mke2fs -d to populate the image from the directory
            subprocess.run(
                ["mke2fs", "-F", "-q", "-d", extract_dir, output_path],
                check=False,
            )
            # If mke2fs -d failed (old version), mount and copy
            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
                _logger.info("mke2fs -d not supported, using mount+copy...")
                with tempfile.TemporaryDirectory() as mnt:
                    subprocess.run(
                        ["sudo", "mount", "-o", "loop", output_path, mnt],
                        check=True,
                    )
                    try:
                        subprocess.run(
                            ["sudo", "cp", "-a", f"{extract_dir}/.", f"{mnt}/"],
                            check=True,
                        )
                    finally:
                        subprocess.run(["sudo", "umount", mnt], check=False)

        finally:
            subprocess.run(["docker", "rm", container_id], capture_output=True)

    # Compute size and hash
    output = Path(output_path)
    size_bytes = output.stat().st_size
    sha256 = hashlib.sha256(output.read_bytes()).hexdigest()

    # Write Dockerfile hash sidecar for freshness check
    dockerfile_hash = hashlib.sha256(Path("docker/Dockerfile.worker").read_bytes()).hexdigest()
    sidecar_path = Path(output_path + ".sha256")
    sidecar_path.write_text(dockerfile_hash)
    _logger.info("Dockerfile hash written to %s: %s", sidecar_path, dockerfile_hash[:16])

    _logger.info("Rootfs built: %s (%d bytes, sha256=%s)", output_path, size_bytes, sha256[:16])
    return {"path": str(output), "size_bytes": size_bytes, "sha256": sha256, "dockerfile_sha256": dockerfile_hash}


# ── Utility: download Firecracker kernel ────────────────────────────────────────


async def download_kernel(output_path: str, version: str = "v1.7") -> dict[str, Any]:
    """Download a Firecracker-compatible kernel binary.

    Downloads from the Firecracker CI S3 bucket.
    """
    url = _FC_KERNEL_URL
    if version != "v1.7":
        url = url.replace("v1.7", version)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    _logger.info("Downloading kernel from %s...", url)
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        output.write_bytes(resp.content)

    sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    size_bytes = output.stat().st_size

    # Verify against known-good hash (supply chain protection)
    if version == "v1.7" and _FC_KERNEL_V1_7_SHA256:
        if sha256 != _FC_KERNEL_V1_7_SHA256:
            raise RuntimeError(
                f"Kernel SHA256 mismatch!\n"
                f"  Expected: {_FC_KERNEL_V1_7_SHA256[:32]}...\n"
                f"  Got:      {sha256[:32]}...\n"
                f"  The downloaded kernel may have been tampered with."
            )

    _logger.info(
        "Kernel downloaded: %s (%d bytes, sha256=%s)",
        output_path, size_bytes, sha256[:16],
    )
    return {"path": str(output), "size_bytes": size_bytes, "sha256": sha256}


# ── Utility: check Firecracker availability ─────────────────────────────────────


def check_firecracker_available(
    kernel_path: str = "/var/lib/studio/firecracker/vmlinux",
    firecracker_binary: str = "firecracker",
) -> dict[str, Any]:
    """Check if Firecracker can run on this host.

    Returns dict with available (bool), kvm (bool), kernel (bool),
    binary (bool), and a human-readable reason if unavailable.
    """
    result = {"available": False, "kvm": False, "kernel": False, "binary": False, "reason": ""}

    if not os.path.exists("/dev/kvm"):
        result["reason"] = "/dev/kvm not found -- KVM not available on this host"
        return result
    result["kvm"] = True

    if not shutil.which(firecracker_binary):
        result["reason"] = f"Firecracker binary '{firecracker_binary}' not found in PATH"
        return result
    result["binary"] = True

    if not os.path.exists(kernel_path):
        result["reason"] = (
            f"Kernel image not found at {kernel_path}. "
            f"Run 'studio download-kernel' to fetch it."
        )
        return result
    result["kernel"] = True

    result["available"] = True
    return result


def check_rootfs_freshness(rootfs_path: str = "/var/lib/studio/firecracker/rootfs.ext4") -> dict[str, Any]:
    """Compare installed rootfs Dockerfile hash against current Dockerfile.worker.

    Returns dict with fresh (bool), stored_hash (str|None), current_hash (str|None),
    and warning (str|None).
    """
    result: dict[str, Any] = {
        "fresh": True,
        "stored_hash": None,
        "current_hash": None,
        "warning": None,
    }

    dockerfile = Path("docker/Dockerfile.worker")
    if not dockerfile.exists():
        result["warning"] = "Dockerfile.worker not found; cannot check freshness"
        return result

    current_hash = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    result["current_hash"] = current_hash

    sidecar = Path(rootfs_path + ".sha256")
    if not sidecar.exists():
        result["fresh"] = False
        result["warning"] = (
            f"No rootfs hash sidecar found at {sidecar}. "
            f"Rootfs may be out of date — run 'studio build-worker-image' to rebuild."
        )
        return result

    stored_hash = sidecar.read_text().strip()
    result["stored_hash"] = stored_hash

    if stored_hash != current_hash:
        result["fresh"] = False
        result["warning"] = (
            f"Worker rootfs is out of date (Dockerfile.worker has changed). "
            f"Run 'studio build-worker-image' to rebuild."
        )
    return result
