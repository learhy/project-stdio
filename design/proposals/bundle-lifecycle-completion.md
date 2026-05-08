# Bundle Lifecycle Completion: I/O Schema, Target Semantics, State Machine, and Steering Mechanics

## Overview

This document closes the four remaining architectural gaps in the v1.1 agent orchestration spec that would cause an implementing agent to guess or stall. Each gap is addressed with concrete, typed specifications suitable for direct translation to code: the `target:` field and control-plane/product boundary, the bundle-level input and output schemas, the complete bundle state machine, and the Pause/Redirect/Abort/Rollback executor mechanics. Four open questions flagged for PM decision are ratified and recorded here.

This document is a machine-readable specification. Schemas use typed YAML with commented types. State transitions are enumerable. Error cases are named. Every design choice states its rationale. The implementing agent should be able to resolve edge cases by applying the stated rationale, not by guessing.

---

## Bundle input schema

The bundle input is the typed contract between whoever files work and the bundler agent. The orchestrator validates it before handing it to the bundler. The task-level I/O spec (Task DAG schema, node spec inputs/outputs) is the structural model; the bundle-level schema mirrors it at a higher abstraction level.

### Schema

```yaml
bundle_input:
  # REQUIRED
  idea: str                         # free-text request, up to 64KB
  filed_by: str                     # identity string of the submitter
  filed_at: str                     # ISO8601 timestamp, orchestrator-populated on receipt
  filed_via: enum                   # idea_forum | cli | mcp | github_issue | agent_generated

  # OPTIONAL
  target_hint: str | null           # "new-repo" | "existing-repo:<name>" | "control-plane" | null
  priority_hint: enum | null        # "low" | "normal" | "high" | null
  deadline: str | null              # ISO8601 timestamp or null
  requested_capabilities: [str]     # capability names the submitter thinks will be needed, default []

  # LINEAGE (all optional, default null)
  parent_bundle_id: str | null      # ULID of the bundle that spawned this one
  supersedes_bundle_id: str | null  # ULID of a prior bundle this one replaces
  related_bundle_ids: [str]         # ULIDs of related bundles for context, default []

  # ATTACHMENTS (optional, default [])
  attachments:
    - name: str                     # human-readable label
      content_type: str             # MIME-like, e.g. "image/png", "text/plain"
      data_ref: str | null          # artifact descriptor if bytes already stored
      url: str | null               # URL to fetch if bytes not yet stored
```

### Field specifications

**`idea`** (required, string, max 65536 bytes). Free-text description of the work requested. Deliberately unstructured. The bundler's job is to structure it. The orchestrator rejects empty strings and strings over 64KB with error `INVALID_INPUT: idea must be 1-65536 bytes`.

**`filed_by`** (required, string). Identity of whoever submitted the idea. In v1.1 this is always the single human reviewer, but typed as a string to avoid coupling to the identity model. The orchestrator does not validate this field against a known-identity list; it is recorded for audit and calibration.

**`filed_at`** (required, string, ISO8601). Set by the orchestrator at input receipt time, not by the submitter. If the submitter provides a value, it is overwritten. This prevents timestamp forgery.

**`filed_via`** (required, enum). The surface the input arrived through. Values: `idea_forum`, `cli`, `mcp`, `github_issue`, `agent_generated`. The orchestrator sets this based on which surface received the input; the submitter cannot override it. Used for calibration (do CLI-filed ideas produce better bundler proposals than MCP-filed ones?).

**`target_hint`** (optional, string or null). The submitter's preference for where work should land. Values: `"new-repo"`, `"existing-repo:<name>"`, `"control-plane"`, or null (no preference). This is a hint, not a constraint. The bundler may override it; the override reason appears in the proposal's concerns section. Validation: if the value matches the pattern `existing-repo:<name>`, the `<name>` must match `[a-z0-9-]+` (1-64 chars). An invalid pattern causes the orchestrator to reject the input with `INVALID_INPUT: target_hint must be "new-repo", "existing-repo:<name>", "control-plane", or null`.

**`priority_hint`** (optional, enum or null). Values: `"low"`, `"normal"`, `"high"`, or null. Advisory. The bundler may override. No effect on scheduling in v1.1 (scheduler is FIFO); reserved for future priority-based scheduling.

**`deadline`** (optional, ISO8601 string or null). Advisory timestamp. The bundler may surface a concern if the estimated wall-clock duration exceeds the available time. The orchestrator does not enforce deadlines in v1.1.

**`requested_capabilities`** (optional, list of strings, default []). Capability names the submitter anticipates the bundle will need. Example: `["github-api-repo-create", "sendgrid-send"]`. The bundler treats these as suggestions during capability manifest construction, not as pre-grants. Each name must match `[a-z][a-z0-9-]*` (1-64 chars). Invalid names are rejected with `INVALID_INPUT: requested_capabilities[<i>] "<name>" is not a valid capability name`.

**`parent_bundle_id`** (optional, ULID string or null). Set when this bundle is spawned from another bundle's action: an Investigate decision on a capability request, an agent-proposed follow-up, or a Redirect re-plan (where the original bundle is the parent). Not a scheduling dependency; provenance metadata only. The orchestrator validates that the ULID exists in the `bundles` table; if not, rejects with `INVALID_INPUT: parent_bundle_id "<id>" does not exist`.

**`supersedes_bundle_id`** (optional, ULID string or null). Set when this bundle replaces a prior bundle (e.g., a rejected bundle that was completely re-thought). The superseded bundle is referenced in this bundle's proposal. The orchestrator validates ULID existence same as `parent_bundle_id`.

**`related_bundle_ids`** (optional, list of ULID strings, default []). Loose references for bundler context: "this is like bundle X." No validation beyond ULID format. Non-existent ULIDs are silently ignored (they may be from a different environment).

**`attachments`** (optional, list of attachment objects, default []). Each attachment has: `name` (required, string, 1-256 chars), `content_type` (required, MIME-like string), `data_ref` (optional, artifact descriptor string, mutually exclusive with `url`), `url` (optional, URL string, mutually exclusive with `data_ref`). Exactly one of `data_ref` or `url` must be provided per attachment. On receipt: attachments with `data_ref` are resolved by the orchestrator and injected into the bundle's artifact namespace as `bundle:attachment-<n>` artifacts before the bundler runs. Attachments with `url` are fetched by the bundler during planning; the fetched content is cached as a bundle-scoped artifact.

### Orchestrator validation (pre-bundler)

Before handing the input to the bundler agent, the orchestrator runs these validations in order:

1. `idea` is a non-empty string <= 65536 bytes.
2. `filed_via` is one of the five enum values.
3. `target_hint` is null or matches one of the three patterns (exact string `"new-repo"`, regex `^existing-repo:[a-z0-9-]{1,64}$`, or exact string `"control-plane"`).
4. `priority_hint` is null, `"low"`, `"normal"`, or `"high"`.
5. `deadline` is null or a valid ISO8601 string parseable by `datetime.fromisoformat`.
6. Each `requested_capabilities` entry matches `^[a-z][a-z0-9-]*$` and is 1-64 chars.
7. `parent_bundle_id` is null or a ULID that exists in `bundles.id`.
8. `supersedes_bundle_id` is null or a ULID that exists in `bundles.id`.
9. Each attachment has exactly one of `data_ref` or `url` set.

Any validation failure returns a structured error to the submitting surface:

```json
{
  "error": "INVALID_INPUT",
  "detail": "<human-readable description of which field failed and why>",
  "field": "<dotted path to the failing field, e.g. 'requested_capabilities[2]'>"
}
```

Validated inputs are assigned a ULID, written to a new row in the `bundles` table with `state = proposed`, and dispatched to the bundler agent as a planning task.

---

## Bundle output schema

The bundle output is the typed record of everything the bundle produced. It is written incrementally during execution and finalized at terminal state. Consumers: the calibration loop (`memory/calibration/scoring-outcomes.jsonl`), the post-mortem prompt, the approval surface (rendered differently per tier), and future bundles that consult memory.

### Schema

```yaml
bundle_output:
  # POPULATED AT PROPOSAL TIME (before execution)
  proposal:
    complexity_score: int           # 0-10
    risk_score: int                 # 0-10
    complexity_factors: {str: int}  # per-factor breakdown for reviewer inspection
    risk_factors: {str: int}        # per-factor breakdown for reviewer inspection
    estimated_loc: int              # estimated lines of code
    estimated_duration_seconds: int # estimated wall-clock duration
    estimated_worker_count: int     # planned worker nodes in DAG
    estimated_tokens: int           # estimated total token consumption
    target: str                     # "new-repo" | "existing-repo:<name>" | "control-plane"
    target_rationale: str           # why the bundler chose this target
    concerns: [str]                 # bundler's concerns section, required non-empty

  # POPULATED AT COMPLETION TIME (terminal state)
  outcome:
    status: enum                    # shipped | parked | killed | failed_verification | aborted | rejected
    rationale: str                  # human-readable explanation

  product_artifacts:
    spawned_repos:
      - name: str                   # repo slug
        url: str                    # full GitHub URL
        registry_key: str           # key in memory/products/registry.json
    merged_prs:
      - repo: str                   # repo slug
        pr_number: int
        pr_url: str
        merge_commit_sha: str

  artifact_manifest:
    global_artifacts_published:
      - descriptor: {namespace: str, name: str, version: str, content_type: str}
        hash: str                   # BLAKE3 hex
    bundle_artifact_index_ref: str  # artifact descriptor for the index of all bundle artifacts

  verification:
    plan_ref: str                   # artifact descriptor for the Verification Plan
    report_ref: str | null          # artifact descriptor for the Verification Report
    outcome: enum | null            # passed | failed | partial | null (if not yet verified)
    failed_criteria: [str]          # list of failed acceptance criteria
    rollback_triggered: bool
    rollback_bundle_id: str | null  # ULID of the rollback bundle if spawned

  # POPULATED INCREMENTALLY DURING EXECUTION, FINALIZED AT COMPLETION
  calibration:
    actual_loc: int
    actual_duration_seconds: int    # active worker time, excluding pause time
    actual_worker_count: int        # total workers spawned (including retries)
    actual_tokens: int
    retry_count: int
    expansion_count: int            # number of dynamic expansions that occurred
    divergence_threshold_exceeded: [str]  # list of axis names exceeding 50% divergence

  cost:
    llm_tokens:
      input_total: int
      output_total: int
      by_model: {str: {input: int, output: int}}
    worker_hours_total: float
    peak_ram_bytes: int
    peak_disk_bytes: int

  memory_pointers:
    decision_ref: str               # path in memory/decisions/
    post_mortem_ref: str | null     # path in memory/post-mortems/ or null
    calibration_ref: str            # path in memory/calibration/
    security_findings_refs: [str]   # paths in memory/security-findings/

  steering_events:
    pause_count: int
    redirect_count: int
    modification_count: int
    mid_flight_decisions:           # reviewer actions during execution
      - action: str                 # pause | resume | redirect | abort
        at: str                     # ISO8601
        by: str                     # identity string
        note: str | null

  identity:
    bundle_id: str                  # ULID
    created_at: str                 # ISO8601
    completed_at: str               # ISO8601, set at terminal transition
    total_wall_clock_seconds: int   # created_at to completed_at, includes pause time
```

### Population lifecycle

**At proposal time** (before the bundle reaches the approval matrix): the `proposal` block is fully populated by the bundler. The `identity.bundle_id` and `identity.created_at` are already set by the orchestrator from input receipt. All other blocks are empty or null.

**During execution**: the `calibration` block fields are updated after each worker completes (loc, duration, tokens, retries, expansions are accumulated). The `steering_events` block is updated on each mid-flight reviewer action. The `cost` block is accumulated as workers consume resources. These are written to the `bundles` row's `outcome_json` column on each update, inside the same SQLite transaction as the triggering event.

**At terminal state**: all remaining fields are populated. `outcome`, `product_artifacts`, `artifact_manifest`, `verification`, and `memory_pointers` are finalized. `identity.completed_at` and `identity.total_wall_clock_seconds` are set.

### Approval surface rendering per tier

The approval surface (MCP resource `studio://bundles/{id}`, GitHub Issue body, CLI `studio show`) renders different subsets of the output depending on tier:

**Auto-approve tier**: `bundle_id`, `created_at`, `proposal.complexity_score`, `proposal.risk_score`, `proposal.target`, `proposal.concerns` (truncated to first 3). A single sentence summary: "Bundle <id> auto-approved: <target> change scored C=<n> R=<n>."

**Summary tier**: All of `proposal` block. `outcome` if terminal. `calibration` divergence flags if any. `verification.outcome` if complete. Does not include full `cost` breakdown, full `artifact_manifest`, or `memory_pointers`.

**Full review tier**: The complete `bundle_output`, including all pre-execution track findings (adversarial, security, verification plan) inlined into the proposal body. The reviewer sees every field.

**Full review with cooldown**: Same as full review, with the cooldown timer displayed prominently.
