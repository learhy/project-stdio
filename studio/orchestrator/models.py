"""Pydantic models for submission schema, manifests, settings, and state enums."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


# ── Task DAG models ──────────────────────────────────────────────────────────

class TaskSpec(BaseModel):
    objective: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[dict[str, Any]] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 1, "backoff": "immediate"})


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
    bundle_id: str
    hash_: str = Field(alias="hash")
    descriptor_json: str = "{}"
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    scope: Literal["bundle", "task", "global"] = "bundle"
    producer_node_id: str | None = None
    created_at: int = 0
    expires_at: int | None = None

    model_config = ConfigDict(populate_by_name=True)


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
    target_hint: str | None = None
    priority_hint: Literal["low", "normal", "high"] | None = None
    deadline: str | None = None
    requested_capabilities: list[str] = Field(default_factory=list)
    parent_bundle_id: str | None = None
    supersedes_bundle_id: str | None = None
    related_bundle_ids: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    target_repo: str = "control-plane"


# ── Submission schema (Phase 1) ──────────────────────────────────────────────

class Submission(BaseModel):
    """Phase 1 kernel submission JSON schema."""
    schema_version: Literal["1.0-phase-1"] = "1.0-phase-1"
    bundle_input: BundleInput = Field(default_factory=BundleInput)
    capability_manifest: CapabilityManifest = Field(default_factory=CapabilityManifest)
    task_dag: TaskDAG = Field(default_factory=TaskDAG)


# ── Settings models ──────────────────────────────────────────────────────────

class KernelSettings(BaseModel):
    mode: bool = True
    network_isolation: Literal["permissive", "enforcing"] = "permissive"


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


class Settings(BaseModel):
    kernel: KernelSettings = Field(default_factory=KernelSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    ollama_cloud: OllamaCloudSettings = Field(default_factory=OllamaCloudSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)


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
