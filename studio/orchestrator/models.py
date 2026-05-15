"""Pydantic models for submission schema, manifests, settings, and state enums."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── Known content schema versions ────────────────────────────────────────────

KNOWN_CAPABILITY_MANIFEST_VERSIONS = frozenset({"1.0"})
KNOWN_TASK_DAG_VERSIONS = frozenset({"1.0"})
KNOWN_SUBMISSION_VERSIONS = frozenset({"1.0-phase-1"})

# ── State enums ──────────────────────────────────────────────────────────────

class BundleState(StrEnum):
    PROPOSED = "proposed"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    REDIRECTING = "redirecting"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    PARKED = "parked"
    FAILED = "failed"
    REJECTED = "rejected"
    ABORTED = "aborted"


class WorkerState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    KILLED = "killed"
    CONNECTION_LOST = "connection_lost"


class NodeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class HeartbeatPhase(StrEnum):
    STARTING = "starting"
    THINKING = "thinking"
    TOOL_CALL = "tool-call"
    WRITING_CODE = "writing-code"
    RUNNING_TESTS = "running-tests"
    IDLE = "idle"


class ApprovalTier(StrEnum):
    AUTO = "auto"
    AUTO_NOTIFY = "auto_notify"
    SUMMARY = "summary"
    FULL_REVIEW = "full_review"
    FULL_REVIEW_COOLDOWN = "full_review_cooldown"


# ── Capability manifest models ───────────────────────────────────────────────

class FilesystemPathGrant(BaseModel):
    path: str
    recursive: bool = False


class FilesystemWriteGrant(FilesystemPathGrant):
    create: bool = True


class WorkingTree(BaseModel):
    branch: str
    base: str
    write_scope: Literal["full", "path_restricted"] = "full"
    restricted_paths: list[str] = Field(default_factory=list)


class FilesystemGrants(BaseModel):
    reads: list[FilesystemPathGrant] = Field(default_factory=list)
    writes: list[FilesystemWriteGrant] = Field(default_factory=list)
    working_tree: WorkingTree | None = None


class EgressGrant(BaseModel):
    destination: str
    ports: list[int] = Field(default_factory=list)
    protocol: Literal["tcp", "udp", "http", "https"] = "tcp"
    rationale: str = ""


class IngressConfig(BaseModel):
    enabled: bool = False


class DnsConfig(BaseModel):
    enabled: bool = True
    resolvers: list[str] = Field(default_factory=list)


class NetworkGrants(BaseModel):
    egress: list[EgressGrant] = Field(default_factory=list)
    ingress: IngressConfig = Field(default_factory=IngressConfig)
    dns: DnsConfig = Field(default_factory=DnsConfig)


class ExecGrant(BaseModel):
    binary: str
    args_pattern: str | None = None
    rationale: str = ""


class SpawnSubtasks(BaseModel):
    enabled: bool = False
    max_depth: int = 0
    max_count: int = 0


class ProcessGrants(BaseModel):
    exec: list[ExecGrant] = Field(default_factory=list)
    spawn_subtasks: SpawnSubtasks = Field(default_factory=SpawnSubtasks)


class SecretGrant(BaseModel):
    name: str
    purpose: Literal["github_auth", "llm_api", "registry_auth", "custom"] = "custom"
    delivery: Literal["env", "file", "rpc"] = "env"
    rationale: str = ""


class ArtifactAccessPattern(BaseModel):
    namespace: str = "*"
    name: str = "*"
    version: str = "*"
    content_type: str = "*"


class ArtifactAccessConfig(BaseModel):
    reads: list[ArtifactAccessPattern] = Field(default_factory=list)
    writes: list[ArtifactAccessPattern] = Field(default_factory=list)


class RpcGrants(BaseModel):
    methods: list[str] = Field(default_factory=list)
    artifact_access: ArtifactAccessConfig = Field(
        default_factory=ArtifactAccessConfig
    )


class LlmTokenBudget(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, dict[str, int]] = Field(default_factory=dict)


class ResourceGrants(BaseModel):
    cpu_limit: int = 0
    memory_limit: int = 0
    disk_limit: int = 0
    wall_time_limit: int = 0
    llm_token_budget: LlmTokenBudget = Field(default_factory=LlmTokenBudget)


class Grants(BaseModel):
    filesystem: FilesystemGrants = Field(default_factory=FilesystemGrants)
    network: NetworkGrants = Field(default_factory=NetworkGrants)
    process: ProcessGrants = Field(default_factory=ProcessGrants)
    secrets: list[SecretGrant] = Field(default_factory=list)
    rpc: RpcGrants = Field(default_factory=RpcGrants)
    resources: ResourceGrants = Field(default_factory=ResourceGrants)


class ManifestMetadata(BaseModel):
    rationale: str = ""
    requested_by: str = ""
    expires_at: str | None = None


class ManifestSubject(BaseModel):
    kind: Literal["bundle", "task"]
    id: str = ""


class CapabilityManifest(BaseModel):
    schema_version: str = "1.0"
    subject: ManifestSubject = Field(default_factory=lambda: ManifestSubject(kind="bundle"))
    grants: Grants = Field(default_factory=Grants)
    metadata: ManifestMetadata = Field(default_factory=ManifestMetadata)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v not in KNOWN_CAPABILITY_MANIFEST_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version: {v!r}. "
                f"Known: {sorted(KNOWN_CAPABILITY_MANIFEST_VERSIONS)}"
            )
        return v


# ── Task DAG models ──────────────────────────────────────────────────────────

class TaskSpec(BaseModel):
    objective: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[dict[str, Any]] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 1, "backoff": "immediate"})
    capability_manifest: dict[str, Any] | None = None
    runner_preference: Literal["local", "remote_ssh", "k8s", "docker", "any"] = "any"


class DAGNode(BaseModel):
    id: str
    kind: Literal["worker", "gate", "aggregator"] = "worker"
    task_manifest_ref: str = ""
    spec: TaskSpec = Field(default_factory=TaskSpec)


class EdgeConditionKind(StrEnum):
    ON_SUCCESS = "on_success"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"
    ON_PROPERTY = "on_property"


class DAGEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    condition: dict[str, str] = Field(default_factory=lambda: {"kind": "on_success"})

    model_config = ConfigDict(populate_by_name=True)


# ── Gate and aggregator models ────────────────────────────────────────────────

class GatePredicateKind(StrEnum):
    ARTIFACT_PROPERTY = "artifact_property"
    RPC_QUERY = "rpc_query"
    HUMAN_APPROVAL = "human_approval"


class GateConfig(BaseModel):
    predicate: GatePredicateKind
    artifact_descriptor: str | None = None
    property_expression: str | None = None
    rpc_method: str | None = None
    rpc_params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 3600
    human_prompt: str = ""


class AggregatorJoinMode(StrEnum):
    ALL = "all"
    ANY = "any"
    QUORUM = "quorum"
    FIRST_SUCCESS = "first_success"


class AggregatorOutputStrategy(StrEnum):
    COLLECT = "collect"
    FIRST = "first"
    REDUCE = "reduce"


class AggregatorConfig(BaseModel):
    join: AggregatorJoinMode = AggregatorJoinMode.ALL
    quorum_count: int | None = None
    cancel_remaining_on_quorum: bool = True
    output_strategy: AggregatorOutputStrategy = AggregatorOutputStrategy.COLLECT
    reducer: str | None = None
    reducer_config: dict[str, Any] = Field(default_factory=dict)


class ExpansionPolicy(BaseModel):
    allow_dynamic_expansion: bool = False
    max_total_nodes: int = 0
    max_depth: int = 0


class DAGMetadata(BaseModel):
    created_by: str = ""
    created_at: str = ""
    rationale: str = ""


class TaskDAG(BaseModel):
    schema_version: str = "1.0"
    bundle_id: str = ""
    bundle_manifest_ref: str = ""
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)
    entry_nodes: list[str] = Field(default_factory=list)
    exit_nodes: list[str] = Field(default_factory=list)
    expansion_policy: ExpansionPolicy = Field(default_factory=ExpansionPolicy)
    metadata: DAGMetadata = Field(default_factory=DAGMetadata)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v not in KNOWN_TASK_DAG_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version: {v!r}. "
                f"Known: {sorted(KNOWN_TASK_DAG_VERSIONS)}"
            )
        return v


# ── on_property expression AST ────────────────────────────────────────────────

class PropExpr(BaseModel):
    """Base for on_property expression nodes. Discriminated by 'type' field."""

    type: str


class FieldAccess(PropExpr):
    type: Literal["field_access"] = "field_access"
    field: str


class StringLiteral(PropExpr):
    type: Literal["string_literal"] = "string_literal"
    value: str


class IntegerLiteral(PropExpr):
    type: Literal["integer_literal"] = "integer_literal"
    value: int


class BooleanLiteral(PropExpr):
    type: Literal["boolean_literal"] = "boolean_literal"
    value: bool


class Comparison(PropExpr):
    type: Literal["comparison"] = "comparison"
    left: PropExpr
    op: Literal["eq", "neq", "gt", "gte", "lt", "lte"]
    right: PropExpr


class ContainsOp(PropExpr):
    type: Literal["contains"] = "contains"
    haystack: PropExpr
    needle: str


class MatchesOp(PropExpr):
    type: Literal["matches"] = "matches"
    value: PropExpr
    pattern: str


class BooleanCombinator(PropExpr):
    type: Literal["and", "or", "not"] = "and"
    operands: list[PropExpr] = Field(default_factory=list)


# ── Dynamic expansion models ──────────────────────────────────────────────────

class DAGFragment(BaseModel):
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)


class ExpansionRequest(BaseModel):
    fragment: DAGFragment = Field(default_factory=DAGFragment)
    graft_point: str = ""
    graft_after_node: str = ""
    rationale: str = ""


class CapRequestParams(BaseModel):
    """Params for cap.request RPC method — expansion or capability-grant request."""
    request_type: Literal["expansion", "capability_grant"] = "expansion"
    expansion: ExpansionRequest | None = None
    requested_scope: dict[str, Any] | None = None
    rationale: str = ""


class CapRequestResult(BaseModel):
    decision: Literal["auto_approved", "escalated", "denied"]
    decision_id: str | None = None


# ── Retry policy ──────────────────────────────────────────────────────────────

class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff: Literal["immediate", "linear", "exponential"] = "immediate"
    delay_seconds: int = 0


# ── Artifact metadata ─────────────────────────────────────────────────────────

class ArtifactMetadata(BaseModel):
    id: int = 0
    namespace: Literal["bundle", "global", "task"] = "bundle"
    name: str = ""
    version: str = ""
    content_type: str = "application/octet-stream"
    hash: str = ""
    size_bytes: int = 0
    inline_data: bytes | None = None
    producer_node_id: str | None = None
    producer_worker_id: str | None = None
    bundle_id: str | None = None
    task_id: str | None = None
    ref_count: int = 0
    created_at: int = 0
    published_at: int = 0
    expires_at: int | None = None
    gc_eligible_at: int | None = None
    gc_d_at: int | None = None


# ── Bundle input models ──────────────────────────────────────────────────────

class Attachment(BaseModel):
    name: str
    content_type: str
    data_ref: str | None = None
    url: str | None = None


class BundleInput(BaseModel):
    idea: str
    filed_by: str = "reviewer"
    filed_at: str = ""
    filed_via: Literal["idea_forum", "cli", "mcp", "github_issue", "agent_generated"] = "cli"
    # target_hint is the spec-compliant advisory field (Phase 2+ bundler path).
    # The bundler may override; the override reason appears in proposal.concerns.
    target_hint: str | None = None
    priority_hint: Literal["low", "normal", "high"] | None = None
    deadline: str | None = None
    requested_capabilities: list[str] = Field(default_factory=list)
    parent_bundle_id: str | None = None
    supersedes_bundle_id: str | None = None
    related_bundle_ids: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    # target_repo is Phase 1 kernel-direct only. Phase 3 cleanup removes it.
    # Use target_hint for the bundler path.
    target_repo: str = "control-plane"


# ── Submission schema (Phase 1) ──────────────────────────────────────────────

class Submission(BaseModel):
    """Phase 1 kernel submission JSON schema."""
    schema_version: str = "1.0-phase-1"
    bundle_input: BundleInput = Field(default_factory=BundleInput)
    capability_manifest: CapabilityManifest = Field(default_factory=CapabilityManifest)
    task_dag: TaskDAG = Field(default_factory=TaskDAG)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v not in KNOWN_SUBMISSION_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version: {v!r}. "
                f"Known: {sorted(KNOWN_SUBMISSION_VERSIONS)}"
            )
        return v


# ── Bundle proposal (bundler agent output) ────────────────────────────────────

class BundleProposal(BaseModel):
    """bundle_output.proposal block produced by the bundler agent (spec lines 2105-2118)."""
    complexity_score: int = Field(default=0, ge=0, le=10)
    risk_score: int = Field(default=0, ge=0, le=10)
    complexity_factors: dict[str, int] = Field(default_factory=dict)
    risk_factors: dict[str, int] = Field(default_factory=dict)
    estimated_loc: int = 0
    estimated_duration_seconds: int = 0
    estimated_worker_count: int = 0
    estimated_tokens: int = 0
    target: str = "control-plane"
    target_rationale: str = ""
    concerns: list[str] = Field(default_factory=list)
    requirements_summary: str = ""
    rfc_summary: str = ""
    implementation_plan: str = ""
    task_dag: dict = Field(default_factory=dict)
    irreversible: bool = False
    tags: list[str] = Field(default_factory=list)
    self_escalation_tier: str | None = None


# ── Review track models ───────────────────────────────────────────────────────

class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    RESOLVED = "resolved"
    ACCEPTED_RISK = "accepted-risk"
    UNRESOLVED = "unresolved"


class ReviewRole(StrEnum):
    ADVERSARIAL = "adversarial"
    SECURITY = "security"
    QA = "qa"


class ReviewFinding(BaseModel):
    severity: Severity = Severity.LOW
    status: FindingStatus = FindingStatus.UNRESOLVED
    category: str = ""
    finding: str = ""
    recommendation: str = ""
    rationale: str = ""


class ThreatModel(BaseModel):
    summary: str = ""
    assets: list[str] = Field(default_factory=list)
    threats: list[str] = Field(default_factory=list)
    mitigations: list[str] = Field(default_factory=list)
    open_risks: list[str] = Field(default_factory=list)


class RollbackPlan(BaseModel):
    machine_executable: bool = False
    auto_rollback_eligible: bool = False
    steps: list[str] = Field(default_factory=list)
    recovery_time_estimate_seconds: int = 0


class VerificationPlan(BaseModel):
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_surface: dict = Field(default_factory=dict)
    pre_merge_gates: list[str] = Field(default_factory=list)
    post_ship_verification: dict = Field(default_factory=dict)
    rollback_plan: RollbackPlan = Field(default_factory=RollbackPlan)


class ReviewTrackOutput(BaseModel):
    role: ReviewRole
    bundle_id: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)
    threat_model: ThreatModel | None = None
    verification_plan: VerificationPlan | None = None
    blocking_issue: bool = False
    blocking_reason: str = ""
    summary: str = ""


# ── Post-execution QA / Verification Report models ────────────────────────────

class CriterionResult(BaseModel):
    criterion: str = ""
    passed: bool = False
    evidence: str = ""
    automated: bool = True


class VerificationReport(BaseModel):
    """Structured Verification Report produced by the post-execution QA agent."""
    bundle_id: str = ""
    outcome: Literal["passed", "failed", "partial"] = "passed"
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    automated_checks: dict = Field(default_factory=dict)
    llm_assessment: str = ""
    failed_criteria: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: str = ""
    produced_at: int = 0


class CalibrationEntry(BaseModel):
    """Estimated vs actual record for a single axis, written to scoring-outcomes.jsonl."""
    bundle_id: str = ""
    recorded_at: int = 0
    estimated_loc: int = 0
    actual_loc: int = 0
    estimated_duration_seconds: int = 0
    actual_duration_seconds: int = 0
    estimated_worker_count: int = 0
    actual_worker_count: int = 0
    estimated_tokens: int = 0
    actual_tokens: int = 0
    divergence_threshold_exceeded: list[str] = Field(default_factory=list)
    blocking_issue: bool = False
    blocking_reason: str = ""
    summary: str = ""


# ── Settings models ──────────────────────────────────────────────────────────

class KernelSettings(BaseModel):
    mode: bool = True


class EgressProxySettings(BaseModel):
    enabled: bool = True
    socket_dir: str = "/run/studio"
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 30


class DefaultTimeoutHours(BaseModel):
    small: int = 2
    medium: int = 4
    large: int = 8


class WorkerSettings(BaseModel):
    global_concurrency: int = 4
    default_timeout_hours: DefaultTimeoutHours = Field(default_factory=DefaultTimeoutHours)
    heartbeat_max_interval_minutes: int = 60
    heartbeat_timeout_multiplier: float = 2.0


class OllamaCloudSettings(BaseModel):
    base_url: str = "https://ollama.com/api"
    health_check_interval_seconds: int = 30
    grace_window_minutes: int = 5


class OrchestratorSettings(BaseModel):
    socket_path: str = "/run/studio/orchestrator.sock"
    db_path: str = "/var/lib/studio/state.db"
    socket_permissions: str = "0660"
    socket_owner: str = "studio:studio"
    memory_root: str = "memory/"
    http_port: int = 7810


class ArtifactsSettings(BaseModel):
    inline_threshold_bytes: int = 4096  # [PROVISIONAL]
    global_storage_cap_bytes: int = 50_000_000_000  # [PROVISIONAL] 50 GB
    per_bundle_cap_bytes: int = 1_000_000_000  # 1 GB
    per_artifact_limit_bytes: int = 100_000_000  # 100 MB
    task_retention_seconds: int = 86400  # [PROVISIONAL] 24h
    bundle_retention_complete_seconds: int = 604800  # [PROVISIONAL] 7 days
    bundle_retention_failed_seconds: int = 2592000  # [PROVISIONAL] 30 days


class McpSettings(BaseModel):
    port: int = 8080
    bearer_token: str = ""


class GitHubSettings(BaseModel):
    enabled: bool = False
    app_id: str = ""
    installation_id: str = ""
    private_key_path: str = ""
    webhook_secret: str = ""
    poll_interval_seconds: int = 60
    owner: str = ""
    repo: str = ""


class ApprovalSettings(BaseModel):
    """Approval matrix configuration (Bundle 2.5)."""
    low_complexity_max: int = 3
    med_complexity_max: int = 6
    low_risk_max: int = 2
    med_risk_max: int = 5
    summary_tier_default_action: str = "hold"
    default_action_overrides: dict[str, str] = Field(default_factory=dict)
    summary_timeout_hours: int = 4
    cooldown_hours_reversible: int = 1
    cooldown_hours_irreversible: int = 24
    mandatory_review_triggers: list[dict] = Field(default_factory=list)


class SecretsConfigEntry(BaseModel):
    name: str
    env_var: str
    purpose: str = "custom"


class OpsSettings(BaseModel):
    """Operational tooling configuration (Bundle 3.3, extended in 3.4)."""
    stall_threshold_hours: int = 8
    escalation_days: list[int] = Field(default_factory=lambda: [5, 10, 21])
    recall_window_hours: int = 48
    acting_soon_hours: int = 12
    worker_token_expiry_minutes: int = 15


class ReviewSettings(BaseModel):
    """Review scheduler and LLM evaluation configuration (Bundle 5.2)."""
    enabled: bool = True
    interval_minutes: int = 10
    time_divergence_threshold: float = 1.5
    checkpoint_silence_minutes: int = 15
    min_interval_seconds: int = 120
    model: str | None = None
    confidence_threshold: float = 0.5
    feedback_threshold_interventions: int = 2


class RemoteWorkersSettings(BaseModel):
    """TCP/TLS listener for remote worker connections with mutual TLS (Bundle 4.1)."""
    enabled: bool = False
    listen_addr: str = "0.0.0.0:7811"
    tls_ca_cert_path: str = "/etc/studio/tls/ca.crt"
    tls_ca_key_path: str = "/etc/studio/tls/ca.key"
    tls_server_cert_path: str = "/etc/studio/tls/server.crt"
    tls_server_key_path: str = "/etc/studio/tls/server.key"


class FleetHost(BaseModel):
    """A single host in the remote worker fleet (Bundle 4.2)."""
    name: str
    addr: str
    ssh_user: str = "studio"
    ssh_key_path: str = ""
    capabilities: list[str] = Field(default_factory=list)
    max_concurrent_workers: int = 4
    arch: str = "x86_64"
    worktree_mode: str = "clone"


class RemoteFleetSettings(BaseModel):
    """Fleet registry for RemoteSSHWorkerRunner (Bundle 4.2)."""
    enabled: bool = False
    hosts: list[FleetHost] = Field(default_factory=list)
    selection_policy: str = "least_loaded"


class K8sRunnerSettings(BaseModel):
    """Kubernetes Job worker runner configuration (Bundle 4.3)."""
    enabled: bool = False
    kubeconfig_path: str | None = None
    namespace: str = "studio-workers"
    orchestrator_tcp_addr: str = "orchestrator.internal:7811"
    image_pull_policy: str = "IfNotPresent"
    worktree_mode: str = "init_container"
    default_storage_class: str | None = None
    worker_image: str = "studio-worker:latest"
    proxy_image: str = "studio-proxy:latest"


class DockerRunnerSettings(BaseModel):
    """Docker worker runner configuration (Bundle 4.5)."""
    enabled: bool = False
    socket_path: str = "/var/run/docker.sock"
    worker_image: str = "project-stdio-worker:latest"
    proxy_image: str = "project-stdio-proxy:latest"
    network_prefix: str = "studio-worker"
    volume_prefix: str = "studio-worktree"
    registry: str | None = None
    pull_policy: str = "if_not_present"


class RunnerSelectorSettings(BaseModel):
    """Runner selection policy configuration (Bundle 4.4)."""
    allow_unenforced_grants: bool = False
    default_preference: Literal["local", "remote_ssh", "k8s", "docker", "any"] = "any"


class Settings(BaseModel):
    kernel: KernelSettings = Field(default_factory=KernelSettings)
    egress_proxy: EgressProxySettings = Field(default_factory=EgressProxySettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    ollama_cloud: OllamaCloudSettings = Field(default_factory=OllamaCloudSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    remote_workers: RemoteWorkersSettings = Field(default_factory=RemoteWorkersSettings)
    remote_fleet: RemoteFleetSettings = Field(default_factory=RemoteFleetSettings)
    k8s_runner: K8sRunnerSettings = Field(default_factory=K8sRunnerSettings)
    docker_runner: DockerRunnerSettings = Field(default_factory=DockerRunnerSettings)
    runner_selector: RunnerSelectorSettings = Field(default_factory=RunnerSelectorSettings)
    artifacts: ArtifactsSettings = Field(default_factory=ArtifactsSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    secrets_config: list[SecretsConfigEntry] = Field(default_factory=list)


# ── RPC message models ───────────────────────────────────────────────────────

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: int | str | None = None


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None
    id: int | str | None = None


# ── RPC method schemas ──────────────────────────────────────────────────────

class HeartbeatParams(BaseModel):
    phase: HeartbeatPhase = HeartbeatPhase.STARTING
    progress: str = ""
    current_step: str | None = None
    estimated_completion_seconds: int | None = None


class LogParams(BaseModel):
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str
    structured_data: dict[str, Any] | None = None


class ProgressReportParams(BaseModel):
    stage: str = ""
    percent: int = Field(ge=0, le=100, default=0)
    message: str = ""


class FinalReportParams(BaseModel):
    outcome: Literal["success", "failure", "paused", "timeout"]
    files_changed: list[str] = Field(default_factory=list)
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    artifacts_produced: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary: str = ""


class CapCheckParams(BaseModel):
    op_descriptor: str


class CapCheckResult(BaseModel):
    allowed: bool
    capability_id: str | None = None


class ArtifactPublishParams(BaseModel):
    descriptor: dict[str, Any]
    data: str  # base64-encoded bytes


class ArtifactFetchParams(BaseModel):
    descriptor: dict[str, Any]


class ArtifactListParams(BaseModel):
    namespace: str | None = None
    name_pattern: str | None = None


class SecretsFetchParams(BaseModel):
    name: str


class AskQuestionParams(BaseModel):
    question_id: str
    question: str
    context: str = ""
    blocking: bool = False
    urgency: Literal["low", "medium", "high"] = "medium"


class ReportCheckpointParams(BaseModel):
    checkpoint_id: str
    phase_completed: str = ""
    phase_starting: str = ""
    summary: str = ""
    concerns: list[str] = Field(default_factory=list)
    estimated_remaining: dict[str, int] = Field(default_factory=dict)


class RespondToQueryParams(BaseModel):
    injection_id: str
    query_type: Literal["describe_progress", "show_artifact"]
    response: dict[str, Any] = Field(default_factory=dict)


class InjectContextParams(BaseModel):
    injection_id: str
    type: Literal["answer", "redirect", "feedback", "question_response"]
    content: str = ""
    question_id: str | None = None
    action: Literal["describe_progress", "show_artifact"] | None = None
    action_path: str | None = None
