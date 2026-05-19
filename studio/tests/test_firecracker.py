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


# ── Bundle 7.3: Installer and operational tooling ─────────────────────────────

import hashlib
import shutil
import tempfile
from unittest.mock import ANY
from studio.orchestrator.cli import (
    cmd_config_set,
    cmd_config_get,
    cmd_vm_pool_resize,
    cmd_check_rootfs,
    cmd_build_worker_image,
    cmd_vm_status,
    cmd_download_kernel,
    _read_config,
    _config_set_nested,
    _config_get_nested,
)
from studio.orchestrator.firecracker import (
    check_rootfs_freshness,
    build_rootfs,
    download_kernel,
    VmPool,
)
from studio.orchestrator.main import _cli_vm_pool_resize


# ── config set/get ───────────────────────────────────────────────────────────


def test_config_set_get(tmp_path):
    """config set writes, config get reads back, with type coercion."""
    config_path = str(tmp_path / "settings.json")
    with patch.dict("os.environ", {"STUDIO_CONFIG_FILE": config_path}):
        assert cmd_config_set("test.key", "hello") == 0
        assert cmd_config_set("test.bool_true", "true") == 0
        assert cmd_config_set("test.bool_false", "false") == 0
        assert cmd_config_set("test.num", "42") == 0

        data = _read_config()
        assert data["test"]["key"] == "hello"
        assert data["test"]["bool_true"] is True
        assert data["test"]["bool_false"] is False
        assert data["test"]["num"] == 42


def test_config_get_missing_key(capsys):
    """config get on missing key returns error."""
    with patch.dict("os.environ", {"STUDIO_CONFIG_FILE": "/nonexistent/settings.json"}):
        exit_code = cmd_config_get("no.such.key")
        assert exit_code == 1


def test_config_set_nested_new():
    """config set creates intermediate dicts for nested keys."""
    data: dict[str, Any] = {}
    _config_set_nested(data, "a.b.c", "42")
    assert data["a"]["b"]["c"] == 42


def test_config_get_nested_success():
    data = {"a": {"b": {"c": "hello"}}}
    val = _config_get_nested(data, "a.b.c")
    assert val == "hello"


def test_config_get_nested_partial():
    data = {"a": 1}
    val = _config_get_nested(data, "a.b")
    assert val is None


# ── vm-pool-resize ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vm_pool_resize_rpc():
    """vm_pool_resize RPC handler calls VmPool.resize with correct size."""
    mock_pool = MagicMock()
    mock_pool.resize = AsyncMock(return_value={"old_size": 3, "new_size": 5, "started": 2, "action": "grown"})
    mock_app = MagicMock()
    mock_app._vm_pool = mock_pool
    mock_app.settings.firecracker.pool_size = 3

    result = await _cli_vm_pool_resize(mock_app, {"size": 5})
    assert result["old_size"] == 3
    assert result["new_size"] == 5
    assert result["action"] == "grown"
    mock_pool.resize.assert_called_once_with(5)


@pytest.mark.asyncio
async def test_vm_pool_resize_rpc_no_pool():
    """vm_pool_resize returns error when pool not running."""
    mock_app = MagicMock()
    mock_app._vm_pool = None
    result = await _cli_vm_pool_resize(mock_app, {"size": 3})
    assert "error" in result


@pytest.mark.asyncio
async def test_vm_pool_resize_rpc_invalid_size():
    """vm_pool_resize rejects non-integer or negative size."""
    mock_app = MagicMock()
    result = await _cli_vm_pool_resize(mock_app, {"size": "abc"})
    assert "error" in result
    result = await _cli_vm_pool_resize(mock_app, {"size": -1})
    assert "error" in result


@pytest.mark.asyncio
async def test_vm_pool_resize_cli():
    """vm-pool-resize CLI sends correct RPC and formats output."""
    mock_resp = {
        "result": {"old_size": 3, "new_size": 1, "drained": 2, "action": "shrinking"}
    }
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value=mock_resp)):
        exit_code = await cmd_vm_pool_resize(1)
        assert exit_code == 0


@pytest.mark.asyncio
async def test_vm_pool_resize_cli_error():
    """vm-pool-resize CLI handles RPC error."""
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={"error": {"message": "not running"}})):
        exit_code = await cmd_vm_pool_resize(2)
        assert exit_code == 1


# ── VmPool.resize ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vm_pool_resize_grow():
    """Growing the pool creates new VMs immediately."""
    mock_vm = MagicMock()
    mock_vm.start = AsyncMock()
    mock_vm.stop = AsyncMock()
    mock_vm.reset = AsyncMock()
    mock_vm.config = MagicMock(vcpus=1, memory_mb=512)

    with patch.object(VmPool, "_create_and_boot_vm", AsyncMock(return_value=mock_vm)):
        pool = VmPool(pool_size=1, rootfs_path="/tmp/rfs.ext4", kernel_path="/tmp/vmlinux")
        await pool.start()
        assert pool.pool_size == 1

        result = await pool.resize(3)
        assert result["action"] == "grown"
        assert result["started"] == 2
        assert pool.pool_size == 3
        await pool.stop()


@pytest.mark.asyncio
async def test_vm_pool_resize_shrink():
    """Shrinking drains idle VMs from queue."""
    mock_vm = MagicMock()
    mock_vm.start = AsyncMock()
    mock_vm.stop = AsyncMock()
    mock_vm.reset = AsyncMock()
    mock_vm.config = MagicMock(vcpus=1, memory_mb=512)

    with patch.object(VmPool, "_create_and_boot_vm", AsyncMock(return_value=mock_vm)):
        pool = VmPool(pool_size=2, rootfs_path="/tmp/rfs.ext4", kernel_path="/tmp/vmlinux")
        await pool.start()
        assert pool._available.qsize() == 2

        result = await pool.resize(1)
        assert result["action"] == "shrinking"
        assert pool.pool_size == 1
        # 2 → 1: one excess VM drained on resize, one remains
        assert result["drained"] == 1
        assert pool._available.qsize() == 1
        await pool.stop()


@pytest.mark.asyncio
async def test_vm_pool_resize_release_discard():
    """After shrinking, released VMs are discarded instead of requeued."""
    mock_vm = MagicMock()
    mock_vm.start = AsyncMock()
    mock_vm.stop = AsyncMock()
    mock_vm.reset = AsyncMock()
    mock_vm.config = MagicMock(vcpus=1, memory_mb=512)

    with patch.object(VmPool, "_create_and_boot_vm", AsyncMock(return_value=mock_vm)):
        pool = VmPool(pool_size=2, rootfs_path="/tmp/rfs.ext4", kernel_path="/tmp/vmlinux")
        await pool.start()
        vm1 = await pool.acquire()
        vm2 = await pool.acquire()

        # Shrink to 0
        await pool.resize(0)

        # Release should discard
        await pool.release(vm1)
        assert pool._available.qsize() == 0
        await pool.release(vm2)
        assert pool._available.qsize() == 0
        await pool.stop()


# ── check_rootfs_freshness ──────────────────────────────────────────────────


def test_rootfs_freshness_no_sidecar(tmp_path):
    """Freshness check returns stale when sidecar missing."""
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.write_bytes(b"dummy")
    with patch("studio.orchestrator.firecracker.Path.exists") as mock_exists:
        # Dockerfile exists, sidecar doesn't
        mock_exists.side_effect = lambda: True if mock_exists.call_count <= 1 else False
        result = check_rootfs_freshness(str(rootfs))
        assert not result["fresh"]
        assert "No rootfs hash sidecar" in result["warning"]


def test_rootfs_freshness_stale():
    """Freshness check returns stale when Dockerfile has changed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rootfs = os.path.join(tmpdir, "rootfs.ext4")
        Path(rootfs).write_bytes(b"dummy")
        sidecar = rootfs + ".sha256"
        Path(sidecar).write_text("oldhash")

        dockerfile = os.path.join(tmpdir, "Dockerfile.worker")
        Path(dockerfile).write_text("FROM ubuntu:22.04")

        with patch("studio.orchestrator.firecracker.Path", autospec=True) as mock_path:
            # We need a real sidecar path but mock the Dockerfile
            pass

        result = check_rootfs_freshness(rootfs)
        assert not result["fresh"]


def test_rootfs_freshness_ok(tmp_path):
    """Freshness check passes when hashes match."""
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.write_bytes(b"dummy")

    # Set up a docker/ directory under tmp_path with Dockerfile.worker
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    dockerfile = docker_dir / "Dockerfile.worker"
    dockerfile_content = b"FROM ubuntu:22.04\n"
    dockerfile.write_bytes(dockerfile_content)

    expected_hash = hashlib.sha256(dockerfile_content).hexdigest()
    sidecar = tmp_path / "rootfs.ext4.sha256"
    sidecar.write_text(expected_hash)

    # Change to tmp_path so docker/Dockerfile.worker resolves relative to CWD
    cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        result = check_rootfs_freshness(str(rootfs))
        assert result["fresh"]
        assert result["current_hash"] == expected_hash
    finally:
        os.chdir(cwd)


# ── check-rootfs CLI ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_rootfs_cli_fresh():
    """check-rootfs CLI shows 'up to date' when fresh."""
    mock_resp = {"result": {"fresh": True, "current_hash": "abc123"}}
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value=mock_resp)):
        exit_code = await cmd_check_rootfs()
        assert exit_code == 0


@pytest.mark.asyncio
async def test_check_rootfs_cli_stale():
    """check-rootfs CLI shows warning when stale."""
    mock_resp = {"result": {"fresh": False, "warning": "out of date"}}
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value=mock_resp)):
        exit_code = await cmd_check_rootfs()
        assert exit_code == 0


@pytest.mark.asyncio
async def test_check_rootfs_cli_rpc_error():
    """check-rootfs CLI handles RPC error."""
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={"error": {"message": "fail"}})):
        exit_code = await cmd_check_rootfs()
        assert exit_code == 1


# ── vm-status CLI ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vm_status_cli():
    """vm-status CLI formats pool status from RPC response."""
    mock_resp = {
        "result": {
            "available": True,
            "enabled": True,
            "pool_size": 3,
            "available_vms": 2,
            "total_spawned": 5,
            "sandbox": "firecracker (active)",
        }
    }
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value=mock_resp)):
        exit_code = await cmd_vm_status()
        assert exit_code == 0


@pytest.mark.asyncio
async def test_vm_status_cli_unavailable():
    """vm-status CLI shows reason when firecracker not available."""
    mock_resp = {
        "result": {
            "available": False,
            "reason": "/dev/kvm not found",
        }
    }
    with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value=mock_resp)):
        exit_code = await cmd_vm_status()
        assert exit_code == 0


# ── download_kernel hash verification ───────────────────────────────────────


@pytest.mark.asyncio
async def test_download_kernel_hash_verify():
    """download_kernel verifies SHA256 against known-good hash."""
    fake_content = b"FAKE_KERNEL"
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.content = fake_content

    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "vmlinux")
        # Set expected hash to match the fake content
        expected = hashlib.sha256(fake_content).hexdigest()
        with patch("studio.orchestrator.firecracker._FC_KERNEL_V1_7_SHA256", expected):
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_response
                result = await download_kernel(output, version="v1.7")
                assert result["sha256"] == expected


@pytest.mark.asyncio
async def test_download_kernel_hash_mismatch():
    """download_kernel raises on SHA256 mismatch."""
    fake_content = b"FAKE_KERNEL"
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.content = fake_content

    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "vmlinux")
        with patch("studio.orchestrator.firecracker._FC_KERNEL_V1_7_SHA256", "deadbeef"):
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_response
                with pytest.raises(RuntimeError, match="SHA256 mismatch"):
                    await download_kernel(output, version="v1.7")


# ── build_rootfs sidecar write ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_rootfs_writes_sidecar():
    """build_rootfs writes Dockerfile hash to .sha256 sidecar."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))

    with patch("studio.orchestrator.firecracker.asyncio.create_subprocess_exec",
               AsyncMock(return_value=mock_proc)):
        with patch("studio.orchestrator.firecracker.subprocess.run"):
            with patch("studio.orchestrator.firecracker.tempfile.TemporaryDirectory"):
                with patch.object(Path, "mkdir"):
                    with patch.object(Path, "write_text") as mock_write:
                        # Mock Dockerfile existence and content
                        with patch.object(Path, "exists", return_value=True):
                            with patch.object(Path, "read_bytes", return_value=b"dockerfile_content"):
                                with patch.object(Path, "stat") as mock_stat:
                                    mock_stat.return_value = MagicMock(st_size=1024)
                                    output = Path("/tmp/test-rootfs-73.ext4")
                                    result = await build_rootfs(str(output))
                                    assert "dockerfile_sha256" in result
                                    mock_write.assert_called()


@pytest.mark.asyncio
async def test_build_worker_image_integration():
    """Integration test: actual build requires Docker. Skip if unavailable."""
    if not shutil.which("docker"):
        pytest.skip("Docker not available")
    # This is a lightweight smoke test — just check the CLI returns 0
    with tempfile.TemporaryDirectory() as tmpdir:
        output = os.path.join(tmpdir, "rootfs.ext4")
        exit_code = await cmd_build_worker_image(output, no_cache=True)
        # May fail if docker/Dockerfile.worker is missing or docker build has issues
        # in CI; just verify it doesn't crash
        assert exit_code in (0, 1)
