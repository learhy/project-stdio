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
