"""Tests for Bundle 4.3: K8sWorkerHandle, K8sJobWorkerRunner, capability_to_pod_spec, k8s-status CLI."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.runner import (
    K8sWorkerHandle,
    K8sJobWorkerRunner,
    capability_to_pod_spec,
    WorkerSpawnResult,
    _generate_token,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    K8sRunnerSettings,
)


def make_k8s_settings(**kwargs):
    defaults = {
        "enabled": True,
        "namespace": "studio-workers",
        "orchestrator_tcp_addr": "orchestrator:7811",
        "image_pull_policy": "IfNotPresent",
        "worktree_mode": "init_container",
        "worker_image": "studio-worker:latest",
        "proxy_image": "studio-proxy:latest",
    }
    defaults.update(kwargs)
    return K8sRunnerSettings(**defaults)


def make_manifest():
    return CapabilityManifest(
        schema_version="1.0",
        grants={
            "filesystem": {"reads": [], "writes": []},
            "network": {"egress": [], "ingress": {"enabled": False}, "dns": {"enabled": True}},
            "process": {"exec": [], "spawn_subtasks": {"enabled": False, "max_depth": 0, "max_count": 0}},
            "secrets": [],
            "rpc": {"methods": ["worker.*"], "artifact_access": {"reads": [], "writes": []}},
            "resources": {"cpu_limit": 0, "memory_limit": 0, "disk_limit": 0, "wall_time_limit": 0,
                          "llm_token_budget": {"input_tokens": 0, "output_tokens": 0, "by_model": {}}},
        },
        metadata={"rationale": "test"},
    )


def make_manifest_with_resources(cpu=0, memory=0, wall_time=0, egress=None, secrets=None, exec_list=None):
    grants = {
        "filesystem": {"reads": [], "writes": []},
        "network": {"egress": egress or [], "ingress": {"enabled": False}, "dns": {"enabled": True}},
        "process": {"exec": exec_list or [], "spawn_subtasks": {"enabled": False, "max_depth": 0, "max_count": 0}},
        "secrets": secrets or [],
        "rpc": {"methods": ["worker.*"], "artifact_access": {"reads": [], "writes": []}},
        "resources": {"cpu_limit": cpu, "memory_limit": memory, "disk_limit": 0, "wall_time_limit": wall_time,
                      "llm_token_budget": {"input_tokens": 0, "output_tokens": 0, "by_model": {}}},
        "metadata": {"rationale": "test"},
    }
    return CapabilityManifest(schema_version="1.0", grants=grants, metadata={"rationale": "test"})


class TestK8sWorkerHandle:
    def test_handle_stores_attributes(self):
        api_client = MagicMock()
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        assert handle.job_name == "job-1"
        assert handle.pod_name == "pod-1"
        assert handle.namespace == "ns"
        assert handle.worker_id == "w1"
        assert handle.returncode is None

    def test_returncode_settable(self):
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", MagicMock())
        assert handle.returncode is None
        handle.returncode = 0
        assert handle.returncode == 0

    @pytest.mark.asyncio
    async def test_cancel_deletes_job(self):
        api_client = MagicMock()
        api_client.BatchV1Api = MagicMock()
        api_client.BatchV1Api.delete_namespaced_job = AsyncMock()
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        await handle.cancel()
        assert handle.returncode == -1
        api_client.BatchV1Api.delete_namespaced_job.assert_called_once_with(
            name="job-1", namespace="ns", propagation_policy="Foreground",
        )

    @pytest.mark.asyncio
    async def test_cancel_handles_api_error(self):
        api_client = MagicMock()
        api_client.BatchV1Api = MagicMock()
        api_client.BatchV1Api.delete_namespaced_job = AsyncMock(side_effect=Exception("gone"))
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        await handle.cancel()
        assert handle.returncode == -1

    @pytest.mark.asyncio
    async def test_is_alive_returns_true_when_running(self):
        api_client = MagicMock()
        api_client.CoreV1Api = MagicMock()
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        api_client.CoreV1Api.read_namespaced_pod = AsyncMock(return_value=mock_pod)
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        alive = await handle.is_alive()
        assert alive is True
        assert handle.returncode is None

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_when_succeeded(self):
        api_client = MagicMock()
        api_client.CoreV1Api = MagicMock()
        mock_pod = MagicMock()
        mock_pod.status.phase = "Succeeded"
        api_client.CoreV1Api.read_namespaced_pod = AsyncMock(return_value=mock_pod)
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        alive = await handle.is_alive()
        assert alive is False
        assert handle.returncode == 0

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_when_failed(self):
        api_client = MagicMock()
        api_client.CoreV1Api = MagicMock()
        mock_pod = MagicMock()
        mock_pod.status.phase = "Failed"
        api_client.CoreV1Api.read_namespaced_pod = AsyncMock(return_value=mock_pod)
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        alive = await handle.is_alive()
        assert alive is False
        assert handle.returncode == 1

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_when_already_completed(self):
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", MagicMock())
        handle.returncode = 0
        alive = await handle.is_alive()
        assert alive is False

    @pytest.mark.asyncio
    async def test_is_alive_handles_api_error(self):
        api_client = MagicMock()
        api_client.CoreV1Api = MagicMock()
        api_client.CoreV1Api.read_namespaced_pod = AsyncMock(side_effect=Exception("gone"))
        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        alive = await handle.is_alive()
        assert alive is False
        assert handle.returncode == -1

    @pytest.mark.asyncio
    async def test_cleanup_removes_all_resources(self):
        api_client = MagicMock()
        api_client.BatchV1Api = MagicMock()
        api_client.BatchV1Api.delete_namespaced_job = AsyncMock()
        api_client.NetworkingV1Api = MagicMock()
        api_client.NetworkingV1Api.delete_namespaced_network_policy = AsyncMock()
        api_client.CoreV1Api = MagicMock()
        api_client.CoreV1Api.delete_namespaced_secret = AsyncMock()

        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", api_client)
        await handle.cleanup()

        api_client.BatchV1Api.delete_namespaced_job.assert_called_once()
        api_client.NetworkingV1Api.delete_namespaced_network_policy.assert_called_once_with(
            name="studio-w1", namespace="ns",
        )
        assert api_client.CoreV1Api.delete_namespaced_secret.call_count == 2


class TestCapabilityToPodSpec:
    def test_basic_pod_spec_structure(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        assert len(spec["containers"]) == 2
        assert spec["containers"][0]["name"] == "worker"
        assert spec["containers"][1]["name"] == "egress-proxy"
        assert spec["restartPolicy"] == "Never"

    def test_init_container_for_clone(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        assert len(spec["initContainers"]) == 2
        init_names = [c["name"] for c in spec["initContainers"]]
        assert "clone-repo" in init_names
        assert "wait-for-proxy" in init_names

    def test_volumes_include_worktree_and_mtls(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        volume_names = [v["name"] for v in spec["volumes"]]
        assert "worktree" in volume_names
        assert "mtls-certs" in volume_names

    def test_resources_limits_from_manifest(self):
        manifest = make_manifest_with_resources(cpu=2, memory=1024, wall_time=3600)
        spec = capability_to_pod_spec(
            manifest=manifest,
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker = spec["containers"][0]
        assert worker["resources"]["limits"]["cpu"] == "2"
        assert worker["resources"]["limits"]["memory"] == "1024Mi"
        assert spec["activeDeadlineSeconds"] == 3600

    def test_no_resource_limits_when_zero(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker = spec["containers"][0]
        assert "cpu" not in worker["resources"]["limits"]

    def test_security_context_hardcoded(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        sc = spec["containers"][0]["securityContext"]
        assert sc["runAsNonRoot"] is True
        assert sc["runAsUser"] == 10000
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    def test_egress_allowlist_passed_as_env(self):
        manifest = make_manifest_with_resources(egress=[
            {"destination": "api.example.com", "ports": [443], "protocol": "https", "rationale": "API access"},
        ])
        spec = capability_to_pod_spec(
            manifest=manifest,
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker_env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        assert "STUDIO_EGRESS_ALLOWLIST" in worker_env
        allowlist = json.loads(worker_env["STUDIO_EGRESS_ALLOWLIST"])
        assert "api.example.com:443" in allowlist

    def test_exec_allowlist_passed_as_env(self):
        manifest = make_manifest_with_resources(exec_list=[
            {"binary": "python3", "args_pattern": None, "rationale": ""},
            {"binary": "node", "args_pattern": None, "rationale": ""},
        ])
        spec = capability_to_pod_spec(
            manifest=manifest,
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker_env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        assert "STUDIO_EXEC_ALLOWLIST" in worker_env
        allowlist = json.loads(worker_env["STUDIO_EXEC_ALLOWLIST"])
        assert "python3" in allowlist
        assert "node" in allowlist

    def test_no_exec_allowlist_when_empty(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker_env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        assert "STUDIO_EXEC_ALLOWLIST" not in worker_env

    def test_secret_volume_mounts(self):
        manifest = make_manifest_with_resources(secrets=[
            {"name": "github_token", "purpose": "github_auth", "delivery": "env", "rationale": ""},
        ])
        spec = capability_to_pod_spec(
            manifest=manifest,
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        volume_names = [v["name"] for v in spec["volumes"]]
        assert "secret-github_token" in volume_names

    def test_task_spec_passed_as_env(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
            task_spec={"objective": "test"},
        )
        worker_env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        assert "STUDIO_TASK_SPEC" in worker_env

    def test_orchestrator_addr_set_correctly(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch.internal:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        worker_env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        assert worker_env["STUDIO_ORCHESTRATOR_ADDR"] == "tcp://orch.internal:7811"

    def test_pod_spec_has_proxy_liveness_probe(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        proxy = spec["containers"][1]
        assert proxy["name"] == "egress-proxy"
        assert "livenessProbe" in proxy
        probe = proxy["livenessProbe"]
        assert probe["exec"]["command"] == ["test", "-S", "/tmp/studio-proxy.sock"]
        assert probe["initialDelaySeconds"] == 5
        assert probe["periodSeconds"] == 10
        assert probe["failureThreshold"] == 3

    def test_pod_spec_has_wait_for_proxy_init_container(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        init_containers = spec["initContainers"]
        assert len(init_containers) == 2
        wait_container = init_containers[1]
        assert wait_container["name"] == "wait-for-proxy"
        assert "until test -S /tmp/studio-proxy.sock" in wait_container["args"][0]
        assert any(v["name"] == "proxy-socket" for v in wait_container["volumeMounts"])

    def test_proxy_socket_shared_volume(self):
        spec = capability_to_pod_spec(
            manifest=make_manifest(),
            worker_id="w1", bundle_id="b1", node_id="n1",
            workdir="/work",
            orchestrator_addr="orch:7811",
            proxy_image="proxy:latest",
            worker_image="worker:latest",
            image_pull_policy="IfNotPresent",
        )
        volume_names = [v["name"] for v in spec["volumes"]]
        assert "proxy-socket" in volume_names
        worker = spec["containers"][0]
        assert any(v["name"] == "proxy-socket" for v in worker["volumeMounts"])
        proxy = spec["containers"][1]
        assert any(v["name"] == "proxy-socket" for v in proxy["volumeMounts"])


class TestK8sJobWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        settings = make_k8s_settings()
        return K8sJobWorkerRunner(db_mock, settings)

    def test_init_stores_settings(self, runner):
        assert runner.settings.namespace == "studio-workers"
        assert runner._api_client is None
        assert runner._watch_task is None

    @pytest.mark.asyncio
    async def test_ensure_client_loads_kubeconfig(self, runner):
        mock_client = MagicMock()
        with patch("kubernetes_asyncio.config.load_kube_config", AsyncMock()), \
             patch("kubernetes_asyncio.client.ApiClient", return_value=mock_client):
            client = await runner._ensure_client()
            assert client is mock_client
            assert runner._api_client is mock_client

    @pytest.mark.asyncio
    async def test_ensure_client_uses_incluster_when_env_set(self, runner):
        mock_client = MagicMock()
        with patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}), \
             patch("kubernetes_asyncio.config.load_incluster_config", MagicMock()), \
             patch("kubernetes_asyncio.client.ApiClient", return_value=mock_client):
            client = await runner._ensure_client()
            assert client is mock_client

    @pytest.mark.asyncio
    async def test_ensure_client_caches(self, runner):
        mock_client = MagicMock()
        runner._api_client = mock_client
        client = await runner._ensure_client()
        assert client is mock_client

    @pytest.mark.asyncio
    async def test_spawn_worker_kubeconfig_failure(self, runner):
        with patch.object(runner, "_load_kubeconfig", AsyncMock(side_effect=Exception("no config"))):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )
            assert "Failed to load kubeconfig" in result.error
            assert result.process is None

    @pytest.mark.asyncio
    async def test_spawn_worker_success(self, runner):
        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_api.BatchV1Api.create_namespaced_job = AsyncMock()
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.create_namespaced_secret = AsyncMock()
        mock_api.CoreV1Api.list_namespaced_pod = AsyncMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.create_namespaced_network_policy = AsyncMock()

        # Pod watch stream: simulate a single ADDED event
        def _make_pod():
            pod = MagicMock()
            pod.metadata.name = "pod-1"
            pod.status.phase = "Pending"
            return pod

        async def fake_stream(*args, **kwargs):
            yield {"type": "ADDED", "object": _make_pod()}

        with patch.object(runner, "_ensure_client", AsyncMock(return_value=mock_api)), \
             patch("kubernetes_asyncio.watch.Watch.stream", fake_stream):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )

        assert result.error == ""
        assert isinstance(result.process, K8sWorkerHandle)
        assert result.process.job_name == "studio-worker-w1"
        assert result.process.pod_name == "pod-1"

        # Verify Job was created
        mock_api.BatchV1Api.create_namespaced_job.assert_called_once()
        # Verify worker row inserted (just check the query was called)
        assert any("INSERT INTO workers" in str(call)
                   for call in runner.db.execute.call_args_list)

    @pytest.mark.asyncio
    async def test_spawn_worker_network_policy_failure(self, runner):
        mock_api = MagicMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.create_namespaced_network_policy = AsyncMock(
            side_effect=Exception("policy denied")
        )
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.create_namespaced_secret = AsyncMock()

        with patch.object(runner, "_ensure_client", AsyncMock(return_value=mock_api)):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )
            assert "Failed to create NetworkPolicy" in result.error

    @pytest.mark.asyncio
    async def test_spawn_worker_creates_network_policy_with_egress(self, runner):
        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_api.BatchV1Api.create_namespaced_job = AsyncMock()
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.create_namespaced_secret = AsyncMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.create_namespaced_network_policy = AsyncMock()

        def _make_pod():
            pod = MagicMock()
            pod.metadata.name = "pod-1"
            pod.status.phase = "Running"
            return pod

        async def fake_stream(*args, **kwargs):
            yield {"type": "ADDED", "object": _make_pod()}

        manifest = make_manifest_with_resources(egress=[
            {"destination": "ollama.com", "ports": [443], "protocol": "https", "rationale": ""},
        ])

        with patch.object(runner, "_ensure_client", AsyncMock(return_value=mock_api)), \
             patch("kubernetes_asyncio.watch.Watch.stream", fake_stream):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", manifest, "/tmp/work",
            )

        assert result.error == ""
        call_args = mock_api.NetworkingV1Api.create_namespaced_network_policy.call_args
        policy = call_args.kwargs["body"]
        assert policy["metadata"]["name"] == "studio-w1"
        assert policy["metadata"]["labels"]["studio/worker-id"] == "w1"
        assert len(policy["spec"]["egress"]) >= 1

    @pytest.mark.asyncio
    async def test_spawn_worker_creates_mtls_secret(self, runner):
        runner.ca_cert_path = "/tmp/ca.crt"
        runner.ca_key_path = "/tmp/ca.key"

        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_api.BatchV1Api.create_namespaced_job = AsyncMock()
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.create_namespaced_secret = AsyncMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.create_namespaced_network_policy = AsyncMock()

        def _make_pod():
            pod = MagicMock()
            pod.metadata.name = "pod-1"
            pod.status.phase = "Running"
            return pod

        async def fake_stream(*args, **kwargs):
            yield {"type": "ADDED", "object": _make_pod()}

        with patch.object(runner, "_ensure_client", AsyncMock(return_value=mock_api)), \
             patch("kubernetes_asyncio.watch.Watch.stream", fake_stream), \
             patch("studio.orchestrator.tls.issue_worker_cert", return_value=(b"cert", b"key")), \
             patch("pathlib.Path.read_bytes", return_value=b"ca-cert"), \
             patch("studio.orchestrator.runner._generate_token", return_value="test-token"):
            result = await runner.spawn_worker(
                "w1", "b1", "n1", make_manifest(), "/tmp/work",
            )

        assert result.error == ""
        # Check mTLS secret was created with stringData (not data)
        secret_calls = [c for c in mock_api.CoreV1Api.create_namespaced_secret.call_args_list
                        if "mtls" in str(c.kwargs.get("body", {}).get("metadata", {}).get("name", ""))]
        assert len(secret_calls) == 1
        body = secret_calls[0].kwargs["body"]
        assert "stringData" in body
        assert "data" not in body

    @pytest.mark.asyncio
    async def test_secret_uses_string_data(self, runner):
        """Verify mTLS Secret uses stringData (not data) for plain PEM strings."""
        runner.ca_cert_path = "/tmp/ca.crt"
        runner.ca_key_path = "/tmp/ca.key"

        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_api.BatchV1Api.create_namespaced_job = AsyncMock()
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.create_namespaced_secret = AsyncMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.create_namespaced_network_policy = AsyncMock()

        def _make_pod():
            pod = MagicMock()
            pod.metadata.name = "pod-1"
            pod.status.phase = "Running"
            return pod

        async def fake_stream(*args, **kwargs):
            yield {"type": "ADDED", "object": _make_pod()}

        with patch.object(runner, "_ensure_client", AsyncMock(return_value=mock_api)), \
             patch("kubernetes_asyncio.watch.Watch.stream", fake_stream), \
             patch("studio.orchestrator.tls.issue_worker_cert", return_value=(b"cert", b"key")), \
             patch("pathlib.Path.read_bytes", return_value=b"ca-cert"):
            await runner.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")

        body = mock_api.CoreV1Api.create_namespaced_secret.call_args.kwargs["body"]
        assert "stringData" in body, "mTLS Secret must use stringData for PEM strings"
        assert "data" not in body, "mTLS Secret must not use data field"

    @pytest.mark.asyncio
    async def test_kill_worker_handles_k8s_handle(self, runner):
        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_api.BatchV1Api.delete_namespaced_job = AsyncMock()
        mock_api.NetworkingV1Api = MagicMock()
        mock_api.NetworkingV1Api.delete_namespaced_network_policy = AsyncMock()
        mock_api.CoreV1Api = MagicMock()
        mock_api.CoreV1Api.delete_namespaced_secret = AsyncMock()

        handle = K8sWorkerHandle("job-1", "pod-1", "ns", "w1", mock_api)
        runner._watched_workers["w1"] = handle
        await runner.kill_worker(handle, "w1")

        assert handle.returncode == -1
        assert "w1" not in runner._watched_workers

    @pytest.mark.asyncio
    async def test_close_stops_watch_and_closes_client(self, runner):
        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        runner._api_client = mock_client

        await runner.close()

        assert runner._api_client is None
        mock_client.close.assert_called_once()


class TestK8sStatusCli:
    @pytest.mark.asyncio
    async def test_k8s_status_no_k8s_runner(self):
        from studio.orchestrator.main import _cli_k8s_status
        app = MagicMock()
        app.runner = MagicMock()  # not a K8sJobWorkerRunner
        result = await _cli_k8s_status(app, {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_k8s_status_with_jobs(self):
        from studio.orchestrator.main import _cli_k8s_status

        mock_api = MagicMock()
        mock_api.BatchV1Api = MagicMock()
        mock_job = MagicMock()
        mock_job.metadata.name = "studio-worker-w1"
        mock_job.metadata.namespace = "studio-workers"
        mock_job.metadata.labels = {"studio/bundle-id": "b1", "studio/worker-id": "w1"}
        mock_job.metadata.creation_timestamp = MagicMock()
        mock_job.metadata.creation_timestamp.timestamp.return_value = 900
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_api.BatchV1Api.list_namespaced_job = AsyncMock(
            return_value=MagicMock(items=[mock_job])
        )

        app = MagicMock()
        app.settings.k8s_runner = make_k8s_settings()
        app.sm.now.return_value = 1000
        app.runner = MagicMock(spec=K8sJobWorkerRunner)
        app.runner._ensure_client = AsyncMock(return_value=mock_api)

        result = await _cli_k8s_status(app, {})
        assert "jobs" in result
        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["name"] == "studio-worker-w1"


class TestK8sCliCommand:
    @pytest.mark.asyncio
    async def test_cmd_k8s_status_no_jobs(self):
        from studio.orchestrator.cli import cmd_k8s_status, _send_rpc
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "result": {"jobs": [], "namespace": "studio-workers"},
        })):
            result = await cmd_k8s_status()
            assert result == 0

    @pytest.mark.asyncio
    async def test_cmd_k8s_status_with_jobs(self):
        from studio.orchestrator.cli import cmd_k8s_status, _send_rpc
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "result": {
                "jobs": [
                    {"name": "studio-worker-w1", "bundle_id": "b1",
                     "active": 1, "succeeded": 0, "failed": 0, "age": 60},
                ],
                "namespace": "studio-workers",
            },
        })):
            result = await cmd_k8s_status()
            assert result == 0

    @pytest.mark.asyncio
    async def test_cmd_k8s_status_rpc_error(self):
        from studio.orchestrator.cli import cmd_k8s_status, _send_rpc
        with patch("studio.orchestrator.cli._send_rpc", AsyncMock(return_value={
            "error": {"message": "socket not found"},
        })):
            result = await cmd_k8s_status()
            assert result == 1
