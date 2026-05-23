"""Studio Isolation Library — Standalone worker isolation and capability enforcement.

This library extracts the isolation layer (runner, capability enforcement, RPC,
artifact store, approval matrix, TLS) from the Studio orchestrator into a
standalone Python package. It does NOT include the DAG executor, scheduler,
or full orchestrator — those are replaced by LangGraph.

Quick start:
    from studio_isolation import (
        CapabilityManifest, Grants, FilesystemGrants, FilesystemPathGrant,
        NetworkGrants, EgressGrant, ProcessGrants, ExecGrant,
        capability_to_bwrap_args, is_subset,
    )

    manifest = CapabilityManifest(
        grants=Grants(
            filesystem=FilesystemGrants(
                reads=[FilesystemPathGrant(path="/home/user/project")],
            ),
            network=NetworkGrants(
                egress=[EgressGrant(destination="github.com", ports=[443], protocol="https")],
            ),
            process=ProcessGrants(
                exec=[ExecGrant(binary="git"), ExecGrant(binary="python")],
            ),
        ),
    )

    # Check if a task manifest is within a bundle manifest's scope
    ok, reason = is_subset(task_manifest, bundle_manifest)
    assert ok, reason

    # Generate bubblewrap arguments for full isolation
    bwrap_args = capability_to_bwrap_args(manifest, worktree_path="/tmp/work", socket_path="/run/studio/orch.sock")
    # → ['bwrap', '--unshare-all', '--ro-bind', '/usr', '/usr', ...]

Packages:
    runner.py       — 4 runner impls: local bwrap, remote SSH, K8s Jobs, Docker containers
    capability.py   — Manifest subset checking (is_subset), op_descriptor dispatch (check_op)
    rpc.py          — JSON-RPC 2.0 dispatcher, connection manager, worker auth
    artifact.py     — Content-addressed artifact store (BLAKE3), secret storage
    approval.py     — Deterministic approval matrix (complexity+risk → tier)
    tls.py          — mTLS CA generation, worker certificate issuance
    models.py       — Pydantic models for manifests, settings, RPC messages
"""

# ── Capability ──────────────────────────────────────────────────────────────────
from studio_isolation.capability import is_subset, check_op

# ── Approval ────────────────────────────────────────────────────────────────────
from studio_isolation.approval import (
    evaluate_approval_matrix,
    ApprovalDecision,
    matrix_lookup,
    CooldownError,
    MandatoryReviewTrigger,
    SENSITIVE_TAGS,
)

# ── Models ──────────────────────────────────────────────────────────────────────
from studio_isolation.models import (
    # Core manifest
    CapabilityManifest,
    Grants,
    FilesystemGrants,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    WorkingTree,
    NetworkGrants,
    EgressGrant,
    IngressConfig,
    DnsConfig,
    ProcessGrants,
    ExecGrant,
    SpawnSubtasks,
    RpcGrants,
    ArtifactAccessConfig,
    ArtifactAccessPattern,
    ResourceGrants,
    LlmTokenBudget,
    SecretGrant,
    ManifestMetadata,
    ManifestSubject,
    # State enums
    ApprovalTier,
    BundleState,
    WorkerState,
    NodeState,
    HeartbeatPhase,
    # Settings
    EgressProxySettings,
    RemoteFleetSettings,
    FleetHost,
    K8sRunnerSettings,
    DockerRunnerSettings,
    FirecrackerSettings,
    RunnerSelectorSettings,
    # RPC messages
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    HeartbeatParams,
    LogParams,
    ProgressReportParams,
    FinalReportParams,
    CapCheckParams,
    CapCheckResult,
    ArtifactPublishParams,
    ArtifactFetchParams,
    ArtifactListParams,
    SecretsFetchParams,
    AskQuestionParams,
    ReportCheckpointParams,
    RespondToQueryParams,
    InjectContextParams,
)

# ── Runner ──────────────────────────────────────────────────────────────────────
from studio_isolation.runner import (
    LocalBwrapWorkerRunner,
    NoopWorkerRunner,
    RemoteSSHWorkerRunner,
    K8sJobWorkerRunner,
    DockerWorkerRunner,
    FirecrackerWorkerRunner,
    RunnerSelector,
    WorkerSpawnResult,
    VmConfig,
    capability_to_bwrap_args,
    capability_to_docker_args,
    capability_to_vm_config,
    capability_to_pod_spec,
    capability_to_runner_compatibility,
)

# ── Artifact ────────────────────────────────────────────────────────────────────
from studio_isolation.artifact import (
    ArtifactDescriptor,
    ArtifactStore,
    SecretStore,
    glob_match,
)

# ── RPC ─────────────────────────────────────────────────────────────────────────
from studio_isolation.rpc import (
    RpcDispatcher,
    RpcHandlers,
    ConnectionManager,
    create_rpc_system,
)

# ── TLS ─────────────────────────────────────────────────────────────────────────
from studio_isolation.tls import (
    generate_ca,
    issue_worker_cert,
    create_server_tls_context,
    create_client_tls_context,
)

# ── LangGraph Adapter ─────────────────────────────────────────────────────────
from studio_isolation.langgraph_adapter import (
    StudioGraphState,
    StudioGraphRunner,
    build_studio_graph,
    run_studio_graph,
    get_graph_mermaid,
)

# ── Meta-Orchestrator ────────────────────────────────────────────────────────
from studio_isolation.meta_orchestrator import (
    MetaOrchestrator,
    DecomposedIntent,
    ExecutionResult,
    SignalRelay,
)

__version__ = "0.2.0"
