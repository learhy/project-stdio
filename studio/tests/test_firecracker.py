"""Tests for firecracker.py — Firecracker VM pool, rootfs build, kernel download."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from studio.orchestrator.firecracker import (
    FirecrackerVm,
    FirecrackerVmConfig,
    VmPool,
    build_rootfs,
    check_firecracker_available,
    download_kernel,
    _build_agent_frame,
    _parse_agent_response,
)


# ── Guest agent protocol ────────────────────────────────────────────────────────


def test_agent_frame_roundtrip():
    frame = _build_agent_frame("ping")
    assert len(frame) > 4
    # Verify length prefix
    payload_len = int.from_bytes(frame[:4], "big")
    assert payload_len == len(frame) - 4


def test_agent_frame_exec():
    frame = _build_agent_frame("exec", argv=["/bin/echo", "hello"], env={"FOO": "bar"})
    payload_len = int.from_bytes(frame[:4], "big")
    payload = json.loads(frame[4:])
    assert payload["cmd"] == "exec"
    assert payload["argv"] == ["/bin/echo", "hello"]
    assert payload["env"] == {"FOO": "bar"}


def test_parse_agent_response_ok():
    resp = _parse_agent_response(b'{"ok": true, "pid": 1001}')
    assert resp.ok
    assert resp.data["pid"] == 1001


def test_parse_agent_response_error():
    resp = _parse_agent_response(b'{"ok": false, "error": "exec failed"}')
    assert not resp.ok
    assert resp.error == "exec failed"


def test_parse_agent_response_invalid_json():
    resp = _parse_agent_response(b"not json")
    assert not resp.ok
    assert "not json" not in resp.error  # error is the JSONDecodeError message


# ── check_firecracker_available ─────────────────────────────────────────────────


def test_firecracker_not_available_no_kvm():
    """test_firecracker_not_available: graceful result when /dev/kvm not present."""
    with patch("os.path.exists") as mock_exists:
        mock_exists.side_effect = lambda p: p != "/dev/kvm"
        result = check_firecracker_available()
        assert not result["available"]
        assert "/dev/kvm not found" in result["reason"]
        assert not result["kvm"]


def test_firecracker_not_available_no_binary():
    with patch("os.path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("shutil.which", return_value=None):
            result = check_firecracker_available()
            assert not result["available"]
            assert "not found in PATH" in result["reason"]
            assert result["kvm"]
            assert not result["binary"]


def test_firecracker_not_available_no_kernel():
    with patch("os.path.exists") as mock_exists:
        mock_exists.side_effect = lambda p: p != "/var/lib/studio/firecracker/vmlinux"
        with patch("shutil.which", return_value="/usr/bin/firecracker"):
            result = check_firecracker_available()
            assert not result["available"]
            assert "download-kernel" in result["reason"]
            assert result["kvm"]
            assert result["binary"]
            assert not result["kernel"]


def test_firecracker_available():
    with patch("os.path.exists", return_value=True):
        with patch("shutil.which", return_value="/usr/bin/firecracker"):
            result = check_firecracker_available()
            assert result["available"]
            assert result["kvm"]
            assert result["binary"]
            assert result["kernel"]


# ── VmPool ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vm_pool_start_creates_vms():
    """test_vm_pool_prewarms: pool.start() creates pool_size VMs."""
    with patch("studio.orchestrator.firecracker.FirecrackerVm.start", new_callable=AsyncMock) as mock_start:
        pool = VmPool(
            pool_size=2,
            rootfs_path="/tmp/test-rootfs.ext4",
            kernel_path="/tmp/test-vmlinux",
        )
        await pool.start()
        assert mock_start.call_count == 2
        assert pool._available.qsize() == 2
        await pool.stop()


@pytest.mark.asyncio
async def test_vm_pool_acquire_release():
    """test_vm_pool_acquire_release: acquire returns VM, release returns it to pool."""
    with patch("studio.orchestrator.firecracker.FirecrackerVm.start", new_callable=AsyncMock):
        with patch("studio.orchestrator.firecracker.FirecrackerVm.reset", new_callable=AsyncMock):
            with patch("studio.orchestrator.firecracker.FirecrackerVm.stop", new_callable=AsyncMock):
                pool = VmPool(
                    pool_size=1,
                    rootfs_path="/tmp/test-rootfs.ext4",
                    kernel_path="/tmp/test-vmlinux",
                )
                await pool.start()
                assert pool._available.qsize() == 1

                vm = await pool.acquire()
                assert isinstance(vm, FirecrackerVm)
                assert pool._available.qsize() == 0

                await pool.release(vm)
                assert pool._available.qsize() == 1

                await pool.stop()


@pytest.mark.asyncio
async def test_vm_pool_acquire_empty_cold_starts():
    """test: empty pool cold-starts a new VM."""
    with patch("studio.orchestrator.firecracker.FirecrackerVm.start", new_callable=AsyncMock) as mock_start:
        pool = VmPool(
            pool_size=0,
            rootfs_path="/tmp/test-rootfs.ext4",
            kernel_path="/tmp/test-vmlinux",
        )
        await pool.start()
        assert pool._available.qsize() == 0

        vm = await pool.acquire()
        # One for pool (none, since size=0), plus one cold-start
        assert mock_start.call_count == 1
        assert isinstance(vm, FirecrackerVm)
        await pool.stop()


@pytest.mark.asyncio
async def test_vm_pool_release_reset_failure_replaces_vm():
    """test: release with failed reset discards and replaces VM."""
    with patch("studio.orchestrator.firecracker.FirecrackerVm.start", new_callable=AsyncMock) as mock_start:
        with patch("studio.orchestrator.firecracker.FirecrackerVm.reset", new_callable=AsyncMock) as mock_reset:
            mock_reset.side_effect = RuntimeError("reset failed")
            with patch("studio.orchestrator.firecracker.FirecrackerVm.stop", new_callable=AsyncMock):
                pool = VmPool(
                    pool_size=1,
                    rootfs_path="/tmp/test-rootfs.ext4",
                    kernel_path="/tmp/test-vmlinux",
                )
                await pool.start()
                vm = await pool.acquire()
                # start called once for pool pre-warm
                assert mock_start.call_count == 1

                await pool.release(vm)
                # reset failed, so a new VM was created as replacement
                assert mock_start.call_count == 2
                assert pool._available.qsize() == 1
                await pool.stop()


@pytest.mark.asyncio
async def test_vm_overlay_reset_reboot_mode():
    """test_vm_overlay_reset: reboot mode calls stop then start."""
    config = FirecrackerVmConfig(reset_mode="reboot")
    vm = FirecrackerVm(vm_id=99, config=config)
    vm.stop = AsyncMock()
    vm.start = AsyncMock()

    await vm.reset()
    vm.stop.assert_called_once()
    vm.start.assert_called_once()


@pytest.mark.asyncio
async def test_vm_overlay_reset_overlay_mode():
    """test: overlay_only mode sends agent reset command."""
    config = FirecrackerVmConfig(reset_mode="overlay_only")
    vm = FirecrackerVm(vm_id=99, config=config)
    vm._agent_send = AsyncMock(return_value=MagicMock(ok=True))

    await vm.reset()
    vm._agent_send.assert_called_once_with("reset")


# ── Settings / Models ───────────────────────────────────────────────────────────


def test_firecracker_settings_defaults():
    from studio.orchestrator.models import Settings, FirecrackerSettings

    settings = Settings()
    assert settings.firecracker.enabled is False
    assert settings.firecracker.pool_size == 3
    assert settings.firecracker.default_vcpus == 1
    assert settings.firecracker.default_memory_mb == 512
    assert settings.firecracker.jailer_enabled is False
    assert settings.firecracker.reset_mode == "reboot"
    assert settings.firecracker.tap_bridge == "studio-fc-br0"
    assert settings.firecracker.ip_range == "172.16.0.0/24"


def test_firecracker_settings_enabled_parsing():
    from studio.orchestrator.models import Settings

    raw = {
        "firecracker": {
            "enabled": True,
            "pool_size": 5,
            "kernel_path": "/custom/vmlinux",
        }
    }
    settings = Settings.model_validate(raw)
    assert settings.firecracker.enabled is True
    assert settings.firecracker.pool_size == 5
    assert settings.firecracker.kernel_path == "/custom/vmlinux"


# ── Download kernel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_kernel():
    """test: download_kernel fetches kernel from remote URL."""
    fake_content = b"FAKE_KERNEL_BINARY_DATA_FOR_TESTING"
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.content = fake_content

    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "vmlinux")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await download_kernel(output, version="v1.7")

        assert result["path"] == output
        assert result["size_bytes"] == len(fake_content)
        assert "sha256" in result
        assert os.path.exists(output)
        with open(output, "rb") as f:
            assert f.read() == fake_content


# ── Build rootfs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_rootfs_dockerfile_not_found():
    """test_rootfs_build: graceful error when Dockerfile is missing."""
    with patch("studio.orchestrator.firecracker.Path.exists", return_value=False):
        with pytest.raises(FileNotFoundError, match="Dockerfile not found"):
            await build_rootfs("/tmp/test-rootfs.ext4")


# ── FirecrackerVm config ────────────────────────────────────────────────────────


def test_firecracker_vm_config_defaults():
    config = FirecrackerVmConfig()
    assert config.vcpus == 1
    assert config.memory_mb == 512
    assert config.reset_mode == "reboot"
    assert not config.jailer_enabled


def test_firecracker_vm_cid_allocation():
    from studio.orchestrator.firecracker import _FC_CID_BASE
    config = FirecrackerVmConfig()
    vm = FirecrackerVm(vm_id=0, config=config)
    assert vm._cid == _FC_CID_BASE  # 3


# ── CLI commands ────────────────────────────────────────────────────────────────


def test_cmd_build_worker_image_function_exists():
    from studio.orchestrator.cli import cmd_build_worker_image
    assert callable(cmd_build_worker_image)


def test_cmd_download_kernel_function_exists():
    from studio.orchestrator.cli import cmd_download_kernel
    assert callable(cmd_download_kernel)


def test_cmd_vm_status_function_exists():
    from studio.orchestrator.cli import cmd_vm_status
    assert callable(cmd_vm_status)


@pytest.mark.asyncio
async def test_cmd_build_worker_image_no_dockerfile():
    with patch("studio.orchestrator.firecracker.Path.exists", return_value=False):
        from studio.orchestrator.cli import cmd_build_worker_image
        exit_code = await cmd_build_worker_image("/tmp/test.ext4")
        assert exit_code == 1


@pytest.mark.asyncio
async def test_cmd_download_kernel_http_error():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("Network error")
        from studio.orchestrator.cli import cmd_download_kernel
        exit_code = await cmd_download_kernel("/tmp/test-vmlinux", version="v1.7")
        assert exit_code == 1


# ── Bundle 7.2: FirecrackerWorkerRunner ─────────────────────────────────────────

from studio.orchestrator.runner import (
    FirecrackerWorkerHandle,
    FirecrackerWorkerRunner,
    LocalBwrapWorkerRunner,
    VmConfig,
    capability_to_vm_config,
    capability_to_runner_compatibility,
    RunnerSelector,
    WorkerSpawnResult,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    FilesystemGrants,
    NetworkGrants,
    EgressGrant,
    ProcessGrants,
    ExecGrant,
    RpcGrants,
    ResourceGrants,
    Grants,
    ManifestSubject,
    ManifestMetadata,
    RunnerSelectorSettings,
)


def make_fc_manifest(**overrides) -> CapabilityManifest:
    grants = Grants(
        filesystem=FilesystemGrants(
            reads=[FilesystemPathGrant(path="/usr/lib", recursive=True)],
            writes=[FilesystemWriteGrant(path="/tmp/build", recursive=True, create=True)],
        ),
        network=NetworkGrants(
            egress=[EgressGrant(destination="pypi.org", ports=["443"], protocol="https", rationale="packages")],
        ),
        process=ProcessGrants(
            exec=[ExecGrant(binary="/usr/bin/python3", args_pattern="*", rationale="run code")],
        ),
        rpc=RpcGrants(methods=["worker.*", "cap.*"]),
        resources=ResourceGrants(cpu_limit=2, memory_limit=1024),
    )
    return CapabilityManifest(
        schema_version="1.0",
        subject=ManifestSubject(kind="bundle", id="test-fc"),
        grants=grants,
        metadata=ManifestMetadata(rationale="test"),
    )


# ── VmConfig / capability_to_vm_config ─────────────────────────────────────────


def test_capability_to_vm_config():
    manifest = make_fc_manifest()
    config = capability_to_vm_config(manifest)
    assert config.vcpus == 2
    assert config.memory_mb == 1024
    assert "pypi.org" in config.egress_allowlist
    assert "/usr/bin/python3" in config.exec_allowlist


def test_capability_to_vm_config_defaults():
    manifest = CapabilityManifest(
        schema_version="1.0",
        subject=ManifestSubject(kind="bundle", id="test"),
        grants=Grants(),
        metadata=ManifestMetadata(rationale="test"),
    )
    config = capability_to_vm_config(manifest)
    assert config.vcpus == 1
    assert config.memory_mb == 512
    assert config.egress_allowlist == []
    assert config.exec_allowlist == []


# ── capability_to_runner_compatibility ─────────────────────────────────────────


def test_capability_runner_compat_includes_firecracker():
    manifest = make_fc_manifest()
    compat = capability_to_runner_compatibility(manifest)
    assert "firecracker" in compat
    assert compat["firecracker"]["compatible"] is True
    assert compat["firecracker"]["unenforced_grants"] == []


# ── FirecrackerWorkerHandle ────────────────────────────────────────────────────


def test_handle_returncode():
    vm = MagicMock()
    handle = FirecrackerWorkerHandle(vm=vm, worker_id="w1")
    assert handle.returncode is None


@pytest.mark.asyncio
async def test_handle_is_alive_no_pid():
    vm = MagicMock()
    handle = FirecrackerWorkerHandle(vm=vm, worker_id="w1")
    assert await handle.is_alive() is False


@pytest.mark.asyncio
async def test_handle_is_alive_with_pid():
    vm = MagicMock()
    vm.is_process_running = AsyncMock(return_value=True)
    handle = FirecrackerWorkerHandle(vm=vm, worker_id="w1", _worker_pid=1000)
    assert await handle.is_alive() is True
    vm.is_process_running.assert_called_once_with(1000)


@pytest.mark.asyncio
async def test_handle_cancel():
    vm = MagicMock()
    vm.exec_signal = AsyncMock()
    vm.stop = AsyncMock()
    handle = FirecrackerWorkerHandle(vm=vm, worker_id="w1", _worker_pid=1000)
    await handle.cancel()
    vm.exec_signal.assert_called_once_with(1000, "TERM")
    vm.stop.assert_called_once()


# ── FirecrackerWorkerRunner ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_worker_firecracker():
    """test_spawn_worker_firecracker: spawn a worker, verify VM is running."""
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.conn = MagicMock()
    mock_db.conn.commit = AsyncMock()

    mock_vm = MagicMock()
    mock_vm.exec = AsyncMock(return_value=1001)
    mock_vm.mount_worktree = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_vm)

    runner = FirecrackerWorkerRunner(
        db=mock_db,
        pool=mock_pool,
        orchestrator_host="172.16.0.1",
    )

    manifest = make_fc_manifest()

    with patch("studio.orchestrator.runner._generate_token", return_value="test-token"):
        with patch("studio.orchestrator.runner.capability_to_bwrap_args", return_value=[]):
            with patch("studio.orchestrator.runner.Path.mkdir"):
                with patch("studio.orchestrator.runner.Path.write_text"):
                    with patch("studio.orchestrator.runner.FirecrackerWorkerRunner._spawn_proxy_tcp",
                               new_callable=AsyncMock) as mock_proxy:
                        mock_proxy.return_value = MagicMock()
                        result = await runner.spawn_worker(
                        worker_id="w-test-1",
                        bundle_id="b-test",
                        node_id="n-test",
                        manifest=manifest,
                        worktree_path="/tmp/test-worktree",
                    )

    assert result.error == ""
    assert result.worker_id == "w-test-1"
    assert result.token == "test-token"
    assert isinstance(result.process, FirecrackerWorkerHandle)
    assert result.process._worker_pid == 1001
    mock_pool.acquire.assert_called_once()
    mock_vm.mount_worktree.assert_called_once_with("/tmp/test-worktree")
    mock_vm.exec.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_worker_firecracker():
    """test_cancel_worker_firecracker: cancel mid-execution, VM stopped, returned to pool."""
    mock_vm = MagicMock()
    mock_vm.exec_signal = AsyncMock()
    mock_vm.stop = AsyncMock()
    mock_vm.extract_worktree_changes = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.release = AsyncMock()

    handle = FirecrackerWorkerHandle(vm=mock_vm, worker_id="w-test", _worker_pid=1001)

    runner = FirecrackerWorkerRunner(
        db=MagicMock(),
        pool=mock_pool,
    )
    runner._active_handles["w-test"] = handle

    await runner.kill_worker(handle, "w-test")

    mock_vm.exec_signal.assert_called_once_with(1001, "TERM")
    mock_vm.stop.assert_called_once()
    mock_pool.release.assert_called_once()


# ── RunnerSelector with firecracker ─────────────────────────────────────────────


def test_runner_selector_registers_firecracker():
    """test_runner_selector_prefers_firecracker: with firecracker registered, it is available."""
    local = MagicMock(spec=LocalBwrapWorkerRunner)
    fc = MagicMock(spec=FirecrackerWorkerRunner)

    selector = RunnerSelector(
        db=MagicMock(),
        settings=RunnerSelectorSettings(),
        local=local,
        firecracker=fc,
    )

    assert "firecracker" in selector._runners
    assert selector._runners["firecracker"] is fc


def test_runner_selector_default_preference_firecracker():
    """When firecracker is registered, default preference is firecracker."""
    fc = MagicMock(spec=FirecrackerWorkerRunner)

    selector = RunnerSelector(
        db=MagicMock(),
        settings=RunnerSelectorSettings(default_preference="any"),
        firecracker=fc,
    )
    assert selector._default_preference() == "firecracker"


def test_runner_selector_default_preference_local_without_firecracker():
    """Without firecracker, default is local (existing behavior)."""
    local = MagicMock(spec=LocalBwrapWorkerRunner)

    selector = RunnerSelector(
        db=MagicMock(),
        settings=RunnerSelectorSettings(default_preference="any"),
        local=local,
    )
    assert selector._default_preference() == "local"


def test_runner_selector_kill_dispatches_to_firecracker():
    """kill_worker dispatches FirecrackerWorkerHandle to firecracker runner."""
    fc = MagicMock(spec=FirecrackerWorkerRunner)
    fc.kill_worker = AsyncMock()

    selector = RunnerSelector(
        db=MagicMock(),
        settings=RunnerSelectorSettings(),
        firecracker=fc,
    )
    handle = FirecrackerWorkerHandle(vm=MagicMock(), worker_id="w1")

    loop = asyncio.new_event_loop()
    loop.run_until_complete(selector.kill_worker(handle, "w1"))
    loop.close()

    fc.kill_worker.assert_called_once()


# ── _drain_worker_pipes for FirecrackerWorkerHandle ────────────────────────────


def test_drain_worker_pipes_noop_for_fc_handle():
    """_drain_worker_pipes is a no-op for non-Process handles."""
    from studio.orchestrator.executor import DagExecutor
    handle = FirecrackerWorkerHandle(vm=MagicMock(), worker_id="w1")
    # Should not raise
    DagExecutor._drain_worker_pipes(handle, "w1")
