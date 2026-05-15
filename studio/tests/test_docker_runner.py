"""Tests for DockerWorkerHandle, capability_to_docker_args, DockerWorkerRunner, and CLI (Bundle 4.5)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from studio.orchestrator.models import (
    CapabilityManifest,
    Grants,
    ProcessGrants,
    FilesystemGrants,
    NetworkGrants,
    ResourceGrants,
    ExecGrant,
    SecretGrant,
    DockerRunnerSettings,
)
from studio.orchestrator.runner import (
    capability_to_runner_compatibility,
    capability_to_docker_args,
    DockerWorkerHandle,
    DockerWorkerRunner,
    WorkerSpawnResult,
)


def make_manifest(exec_grants=None, secrets=None, resources=None):
    grants = Grants(
        resources=resources or ResourceGrants(),
    )
    if exec_grants:
        grants.process = ProcessGrants(exec=exec_grants)
    if secrets:
        grants.secrets = secrets
    return CapabilityManifest(schema_version="1.0", grants=grants)


class TestCapabilityToRunnerCompatibility:
    def test_docker_compatible_by_default(self):
        compat = capability_to_runner_compatibility(make_manifest())
        assert compat["docker"]["compatible"] is True
        assert compat["docker"]["unenforced_grants"] == []

    def test_docker_reports_exec_allowlist_unenforced(self):
        manifest = make_manifest(exec_grants=[ExecGrant(binary="python")])
        compat = capability_to_runner_compatibility(manifest)
        assert compat["docker"]["unenforced_grants"] == ["exec_allowlist"]
        assert compat["k8s"]["unenforced_grants"] == ["exec_allowlist"]


class TestCapabilityToDockerArgs:
    def test_security_defaults(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        assert "--read-only" in args
        assert "--no-new-privileges" in args
        assert "--cap-drop" in args
        assert "ALL" in args
        assert "--user" in args
        assert "10000:10000" in args

    def test_tmpfs_present(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        assert "--tmpfs" in args

    def test_resource_limits_applied(self):
        resources = ResourceGrants(cpu_limit=2, memory_limit=512)
        args = capability_to_docker_args(make_manifest(resources=resources), "w1", "orch:7811", "tok")
        assert "--cpus" in args
        assert "2" in args
        assert "--memory" in args
        assert "512m" in args

    def test_pids_limit(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        assert "--pids-limit" in args

    def test_orchestrator_env_vars(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch.example.com:7811", "mytoken")
        joined = " ".join(args)
        assert "STUDIO_ORCHESTRATOR_ADDR=orch.example.com:7811" in joined
        assert "STUDIO_WORKER_TOKEN=mytoken" in joined
        assert "STUDIO_WORKER_ID=w1" in joined

    def test_proxy_env_vars(self):
        args = capability_to_docker_args(
            make_manifest(), "w1", "orch:7811", "tok",
            proxy_env={"PROXY_SOCKET": "/tmp/proxy.sock"},
        )
        joined = " ".join(args)
        assert "PROXY_SOCKET=/tmp/proxy.sock" in joined

    def test_secrets_as_env(self):
        manifest = make_manifest(secrets=[SecretGrant(name="GITHUB_TOKEN", purpose="github_auth")])
        args = capability_to_docker_args(manifest, "w1", "orch:7811", "tok")
        joined = " ".join(args)
        assert "GITHUB_TOKEN=github_auth" in joined

    def test_dns_resolvers(self):
        manifest = make_manifest()
        manifest.grants.network = NetworkGrants(
            dns={"enabled": True, "resolvers": ["8.8.8.8", "8.8.4.4"]},
        )
        args = capability_to_docker_args(manifest, "w1", "orch:7811", "tok")
        assert "--dns" in args
        assert "8.8.8.8" in args
        assert "8.8.4.4" in args

    def test_dns_disabled(self):
        manifest = make_manifest()
        manifest.grants.network = NetworkGrants(
            dns={"enabled": False},
        )
        args = capability_to_docker_args(manifest, "w1", "orch:7811", "tok")
        assert "--dns" in args
        assert "0.0.0.0" in args

    def test_studio_labels(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        joined = " ".join(args)
        assert "studio/worker-id=w1" in joined
        assert "studio/runner=docker" in joined

    def test_workdir_set(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        assert "--workdir" in args
        assert "/work" in args

    def test_no_resource_limits_when_zero(self):
        args = capability_to_docker_args(make_manifest(), "w1", "orch:7811", "tok")
        # cpu and memory should not be set when 0
        cpus_idx = None
        mem_idx = None
        try:
            cpus_idx = args.index("--cpus")
        except ValueError:
            pass
        try:
            mem_idx = args.index("--memory")
        except ValueError:
            pass
        assert cpus_idx is None
        assert mem_idx is None


class TestDockerWorkerHandle:
    def _make_handle(self, client=None):
        if client is None:
            client = MagicMock()
        return DockerWorkerHandle(
            worker_id="w1",
            worker_container_id="abc123",
            proxy_container_id="def456",
            volume_name="studio-worktree-w1",
            proxy_volume_name="proxy-socket-w1",
            network_name="studio-worker-w1",
            client=client,
        )

    @pytest.mark.asyncio
    async def test_cancel_stops_containers(self):
        client = MagicMock()
        worker_ctr = MagicMock()
        proxy_ctr = MagicMock()
        client.containers.get.side_effect = [worker_ctr, proxy_ctr]

        handle = self._make_handle(client)
        await handle.cancel()

        worker_ctr.stop.assert_called_once_with(timeout=10)
        proxy_ctr.stop.assert_called_once_with(timeout=5)
        assert handle.returncode == 137

    @pytest.mark.asyncio
    async def test_is_alive_when_running(self):
        client = MagicMock()
        ctr = MagicMock()
        ctr.status = "running"
        client.containers.get.return_value = ctr

        handle = self._make_handle(client)
        alive = await handle.is_alive()
        assert alive is True

    @pytest.mark.asyncio
    async def test_is_alive_when_returncode_set(self):
        handle = self._make_handle()
        handle.returncode = 137
        alive = await handle.is_alive()
        assert alive is False

    @pytest.mark.asyncio
    async def test_is_alive_not_found(self):
        client = MagicMock()
        client.containers.get.side_effect = Exception("not found")
        handle = self._make_handle(client)
        alive = await handle.is_alive()
        assert alive is False

    @pytest.mark.asyncio
    async def test_cleanup_removes_all_resources(self):
        client = MagicMock()
        worker_ctr = MagicMock()
        proxy_ctr = MagicMock()
        vol = MagicMock()
        net = MagicMock()
        client.containers.get.side_effect = [worker_ctr, proxy_ctr]
        client.volumes.get.return_value = vol
        client.networks.get.return_value = net

        handle = self._make_handle(client)
        await handle.cleanup()

        worker_ctr.remove.assert_called_once_with(force=True)
        proxy_ctr.remove.assert_called_once_with(force=True)
        assert vol.remove.call_count == 2
        vol.remove.assert_called_with(force=True)
        net.remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_missing_resources(self):
        client = MagicMock()
        client.containers.get.side_effect = Exception("not found")
        client.volumes.get.side_effect = Exception("not found")
        client.networks.get.side_effect = Exception("not found")

        handle = self._make_handle(client)
        # Should not raise
        await handle.cleanup()


class TestDockerWorkerRunner:
    def _make_settings(self, **kwargs):
        defaults = {
            "enabled": True,
            "socket_path": "/var/run/docker.sock",
            "worker_image": "project-stdio-worker:latest",
            "proxy_image": "project-stdio-proxy:latest",
            "network_prefix": "studio-worker",
            "volume_prefix": "studio-worktree",
            "pull_policy": "never",
        }
        defaults.update(kwargs)
        return DockerRunnerSettings(**defaults)

    def _make_runner(self, settings=None):
        settings = settings or self._make_settings()
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()

        with patch("studio.orchestrator.runner.docker_lib.DockerClient") as mock_dc:
            mock_client = MagicMock()
            mock_dc.return_value = mock_client
            runner = DockerWorkerRunner(db, settings)
            # Inject pre-created client so _get_client doesn't create a new one
            runner._client = mock_client
            return runner, db, mock_client

    @pytest.mark.asyncio
    async def test_spawn_worker_creates_containers_and_network(self):
        runner, db, client = self._make_runner()

        worker_ctr = MagicMock()
        worker_ctr.id = "worker-ctr-id"
        proxy_ctr = MagicMock()
        proxy_ctr.id = "proxy-ctr-id"
        # exec_run returns exit_code=0 (socket exists) on first poll
        proxy_ctr.exec_run.return_value = (0, "ok")

        client.containers.run.side_effect = [proxy_ctr, worker_ctr]
        client.images.get.return_value = MagicMock()
        client.networks.create.return_value = MagicMock()
        client.volumes.create.return_value = MagicMock()

        result = await runner.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")

        assert result.error == ""
        assert result.worker_id == "w1"
        assert result.token != ""
        assert isinstance(result.process, DockerWorkerHandle)
        assert result.process.worker_container_id == "worker-ctr-id"
        assert result.process.proxy_container_id == "proxy-ctr-id"
        assert result.process.proxy_volume_name == "proxy-socket-w1"

        # Verify proxy volume was created first
        vol_calls = client.volumes.create.call_args_list
        assert vol_calls[0].kwargs["name"] == "proxy-socket-w1"
        assert vol_calls[1].kwargs["name"] == "studio-worktree-w1"

        # Verify container starts
        assert client.containers.run.call_count == 2
        # First call: proxy, second: worker
        proxy_call = client.containers.run.call_args_list[0]
        assert proxy_call.kwargs["name"] == "studio-proxy-w1"
        assert any(
            m["Source"] == "proxy-socket-w1" and m["Target"] == "/tmp/studio"
            for m in proxy_call.kwargs["mounts"]
        )
        worker_call = client.containers.run.call_args_list[1]
        assert worker_call.kwargs["image"] == "project-stdio-worker:latest"
        assert worker_call.kwargs["network_mode"] == "container:proxy-ctr-id"
        worker_mount_sources = {m["Source"] for m in worker_call.kwargs["mounts"]}
        assert "studio-worktree-w1" in worker_mount_sources
        assert "proxy-socket-w1" in worker_mount_sources

    @pytest.mark.asyncio
    async def test_spawn_failure_cleans_up(self):
        runner, db, client = self._make_runner()
        client.images.get.return_value = MagicMock()
        client.networks.create.return_value = MagicMock()
        client.volumes.create.return_value = MagicMock()
        # Fail on proxy create
        client.containers.run.side_effect = Exception("docker error")

        result = await runner.spawn_worker("w2", "b1", "n2", make_manifest(), "/tmp/work")
        assert result.error != ""
        assert "docker error" in result.error

    @pytest.mark.asyncio
    async def test_proxy_volume_shared(self):
        """Verify proxy and worker both mount the same proxy-socket volume at /tmp/studio."""
        runner, db, client = self._make_runner()

        proxy_ctr = MagicMock()
        proxy_ctr.id = "proxy-ctr-id"
        proxy_ctr.exec_run.return_value = (0, "ok")
        worker_ctr = MagicMock()
        worker_ctr.id = "worker-ctr-id"

        client.containers.run.side_effect = [proxy_ctr, worker_ctr]
        client.images.get.return_value = MagicMock()
        client.networks.create.return_value = MagicMock()
        client.volumes.create.return_value = MagicMock()

        await runner.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")

        proxy_call = client.containers.run.call_args_list[0]
        worker_call = client.containers.run.call_args_list[1]

        # Both mount proxy-socket-w1 at /tmp/studio
        proxy_mounts = {m["Source"]: m["Target"] for m in proxy_call.kwargs["mounts"]}
        worker_mounts = {m["Source"]: m["Target"] for m in worker_call.kwargs["mounts"]}
        assert proxy_mounts.get("proxy-socket-w1") == "/tmp/studio"
        assert worker_mounts.get("proxy-socket-w1") == "/tmp/studio"

        # Worker mount is read-only
        worker_proxy_mount = [m for m in worker_call.kwargs["mounts"] if m["Source"] == "proxy-socket-w1"][0]
        assert worker_proxy_mount["ReadOnly"] is True

    @pytest.mark.asyncio
    async def test_proxy_socket_poll_timeout(self):
        """When exec_run never returns 0, RuntimeError is raised."""
        runner, db, client = self._make_runner()

        proxy_ctr = MagicMock()
        proxy_ctr.id = "proxy-ctr-id"
        proxy_ctr.exec_run.return_value = (1, "not found")

        client.containers.run.side_effect = [proxy_ctr]
        client.images.get.return_value = MagicMock()
        client.networks.create.return_value = MagicMock()
        client.volumes.create.return_value = MagicMock()

        result = await runner.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")
        assert result.error != ""
        assert "failed to bind socket after 5s" in result.error.lower()

    @pytest.mark.asyncio
    async def test_proxy_socket_poll_success(self):
        """After a few failures, exec_run returns 0 and spawn proceeds."""
        runner, db, client = self._make_runner()

        proxy_ctr = MagicMock()
        proxy_ctr.id = "proxy-ctr-id"
        # First two polls fail, third succeeds
        proxy_ctr.exec_run.side_effect = [(1, ""), (1, ""), (0, "ok")]

        worker_ctr = MagicMock()
        worker_ctr.id = "worker-ctr-id"

        client.containers.run.side_effect = [proxy_ctr, worker_ctr]
        client.images.get.return_value = MagicMock()
        client.networks.create.return_value = MagicMock()
        client.volumes.create.return_value = MagicMock()

        result = await runner.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")
        assert result.error == ""
        assert result.process.worker_container_id == "worker-ctr-id"
        assert proxy_ctr.exec_run.call_count == 3

    @pytest.mark.asyncio
    async def test_kill_worker_docker_handle(self):
        runner, db, client = self._make_runner()
        handle = MagicMock(spec=DockerWorkerHandle)
        handle.cancel = AsyncMock()
        handle.cleanup = AsyncMock()

        await runner.kill_worker(handle, "w1")
        handle.cancel.assert_called_once()
        handle.cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_worker_subprocess(self):
        runner, db, client = self._make_runner()
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)

        await runner.kill_worker(proc, "w1")
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_closes_client(self):
        runner, db, client = self._make_runner()
        await runner.close()
        client.close.assert_called_once()
        assert runner._client is None

    @pytest.mark.asyncio
    async def test_ensure_image_pulls_when_missing_if_not_present(self):
        runner, db, client = self._make_runner(self._make_settings(pull_policy="if_not_present"))
        client.images.get.side_effect = __import__("docker").errors.ImageNotFound("nope")

        await runner._ensure_image("some-image:latest")
        client.images.pull.assert_called_once_with("some-image:latest")

    @pytest.mark.asyncio
    async def test_ensure_image_skip_pull_when_present(self):
        runner, db, client = self._make_runner()
        client.images.get.return_value = MagicMock()

        await runner._ensure_image("some-image:latest")
        client.images.pull.assert_not_called()


class TestDockerCliHandlers:
    @pytest.mark.asyncio
    async def test_docker_status_no_runner(self):
        from studio.orchestrator.main import _cli_docker_status
        app = MagicMock()
        app._docker_runner = None
        result = await _cli_docker_status(app, {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_docker_status_with_containers(self):
        from studio.orchestrator.main import _cli_docker_status
        app = MagicMock()
        app._docker_runner = MagicMock()
        mock_ctr = MagicMock()
        mock_ctr.short_id = "abc123"
        mock_ctr.name = "studio-worker-w1"
        mock_ctr.labels = {"studio/bundle-id": "b1", "studio/worker-id": "w1"}
        mock_ctr.status = "running"
        mock_ctr.image = MagicMock()
        mock_ctr.image.tags = ["project-stdio-worker:latest"]
        mock_ctr.attrs = {"Created": "2025-01-01T00:00:00Z"}

        client = MagicMock()
        client.containers.list.return_value = [mock_ctr]
        app._docker_runner._get_client.return_value = client

        result = await _cli_docker_status(app, {})
        assert "containers" in result
        assert result["count"] == 1
        assert result["containers"][0]["name"] == "studio-worker-w1"

    @pytest.mark.asyncio
    async def test_docker_images_no_runner(self):
        from studio.orchestrator.main import _cli_docker_images
        app = MagicMock()
        app._docker_runner = None
        result = await _cli_docker_images(app, {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_docker_images_with_images(self):
        from studio.orchestrator.main import _cli_docker_images
        app = MagicMock()
        app._docker_runner = MagicMock()
        app.settings = MagicMock()
        app.settings.docker_runner = MagicMock()
        app.settings.docker_runner.worker_image = "project-stdio-worker:latest"
        app.settings.docker_runner.proxy_image = "project-stdio-proxy:latest"

        mock_img = MagicMock()
        mock_img.short_id = "sha:abc"
        mock_img.tags = ["project-stdio-worker:latest"]
        mock_img.attrs = {"Created": "2025-01-01T00:00:00Z", "Size": 100_000_000}

        client = MagicMock()
        client.images.list.return_value = [mock_img]
        app._docker_runner._get_client.return_value = client

        result = await _cli_docker_images(app, {})
        assert "images" in result
        assert len(result["images"]) == 1


class TestDockerCliCommands:
    @pytest.mark.asyncio
    async def test_cmd_docker_status(self):
        from studio.orchestrator.cli import cmd_docker_status
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "result": {"containers": [
                {"container_id": "abc", "name": "w1", "bundle_id": "b1",
                 "status": "running", "image": "img"},
            ], "count": 1},
        })):
            result = await cmd_docker_status()
            assert result == 0

    @pytest.mark.asyncio
    async def test_cmd_docker_images(self):
        from studio.orchestrator.cli import cmd_docker_images
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "result": {
                "images": [
                    {"id": "sha:abc", "tags": ["img"], "size": 100, "created": "2025"},
                ],
                "expected_worker": "img:latest",
                "expected_proxy": "proxy:latest",
            },
        })):
            result = await cmd_docker_images()
            assert result == 0

    @pytest.mark.asyncio
    async def test_cmd_docker_status_empty(self):
        from studio.orchestrator.cli import cmd_docker_status
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "result": {"containers": [], "count": 0},
        })):
            result = await cmd_docker_status()
            assert result == 0


class TestRunnerSelectorDocker:
    def test_runner_names_includes_docker(self):
        from studio.orchestrator.runner import RunnerSelector
        from studio.orchestrator.models import RunnerSelectorSettings
        db = MagicMock()
        local = MagicMock()
        docker = MagicMock(spec=DockerWorkerRunner)
        sel = RunnerSelector(db, RunnerSelectorSettings(), local=local, docker=docker)
        assert "docker" in sel.runner_names

    def test_select_docker_when_preferred(self):
        from studio.orchestrator.runner import RunnerSelector
        from studio.orchestrator.models import RunnerSelectorSettings
        db = MagicMock()
        local = MagicMock()
        docker = MagicMock(spec=DockerWorkerRunner)
        sel = RunnerSelector(db, RunnerSelectorSettings(), local=local, docker=docker)

        runner_type, runner = sel._select_runner("docker")
        assert runner_type == "docker"
        assert runner is docker

    def test_get_runner_docker(self):
        from studio.orchestrator.runner import RunnerSelector
        from studio.orchestrator.models import RunnerSelectorSettings
        db = MagicMock()
        docker = MagicMock(spec=DockerWorkerRunner)
        sel = RunnerSelector(db, RunnerSelectorSettings(), docker=docker)
        assert sel.get_runner("docker") is docker
