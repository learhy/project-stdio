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

---

## The `target:` field

The `target:` field declares where the bundle's output lands. Three values: `new-repo`, `existing-repo:<name>`, `control-plane`. This section specifies the decision rule, the control-plane/product boundary, the mechanics for each value, approval matrix interaction, and cross-target policy.

### Decision rule: how `target:` is set

The `target:` field is set by the **bundler during planning**. The submitter may provide a `target_hint` in the bundle input; this is advisory. The bundler follows this algorithm:

```python
def determine_target(input: BundleInput, proposal: BundleProposal) -> tuple[str, str]:
    """Returns (target_value, rationale_string)."""
    hint = input.target_hint

    # Step 1: classify the work
    is_new_product = proposal_creates_new_deployable_unit(proposal)
    modifies_existing = references_existing_repo_in_registry(proposal)
    is_control_plane_only = all_changes_are_control_plane_content(proposal)

    # Step 2: resolve classification against hint
    if hint is None:
        # No hint: bundler decides
        if is_new_product and not modifies_existing:
            return ("new-repo", "bundle creates a new deployable product")
        elif modifies_existing and not is_new_product:
            repo = resolve_existing_repo(proposal)
            return (f"existing-repo:{repo}", f"bundle modifies existing repo '{repo}'")
        elif is_control_plane_only:
            return ("control-plane", "all changes are internal to the control plane")
        else:
            # Ambiguous: escalate to reviewer
            raise AmbiguousTargetError(
                "cannot determine target automatically",
                candidates=["new-repo", "control-plane", "existing-repo:..."]
            )

    # Step 3: hint provided, check coherence
    if hint == "new-repo":
        if modifies_existing and not is_new_product:
            # Hint contradicts analysis: surface concern, follow analysis
            return ("existing-repo:...", "target_hint was 'new-repo' but bundle modifies existing repo; overridden")
        return ("new-repo", "matches target_hint: creates new deployable product")

    elif hint.startswith("existing-repo:"):
        repo_name = hint.split(":", 1)[1]
        if not repo_exists_in_registry(repo_name):
            raise InvalidTargetError(f"target_hint references non-existent repo '{repo_name}'")
        return (hint, f"matches target_hint: modifies '{repo_name}'")

    elif hint == "control-plane":
        if not is_control_plane_only:
            # Hint contradicts: surface concern, escalate
            raise AmbiguousTargetError(
                "target_hint is 'control-plane' but proposal includes non-control-plane changes",
                candidates=["control-plane", "new-repo", "existing-repo:..."]
            )
        return ("control-plane", "matches target_hint: all changes are control-plane")

    # unreachable
```

**Classification helpers:**

`proposal_creates_new_deployable_unit(proposal) -> bool` returns True when the proposal's primary output is a new service, frontend, CLI tool, or other self-contained deployable. The bundler makes this call by analyzing the requirements: if the proposal describes a thing that has its own deploy step, its own port, its own data store, or its own user-facing surface distinct from existing products, it's a new deployable unit. This is a judgment call; ambiguous cases are escalated to the reviewer.

`references_existing_repo_in_registry(proposal) -> bool` returns True when the proposal explicitly names a repo from `memory/products/registry.json` as a modification target. The bundler checks the registry during planning. If the proposal references a repo name that does not exist in the registry, the bundler surfaces this as a concern and does not set the target to that repo.

`all_changes_are_control_plane_content(proposal) -> bool` returns True when all files the proposal plans to touch are classified as control-plane content per the boundary specification below.

`resolve_existing_repo(proposal) -> str` returns the repo slug. If the proposal references exactly one existing repo, that slug. If it references multiple, the bundler escalates (cross-target not supported).

**When the bundler cannot determine the target**, it raises `AmbiguousTargetError` which is surfaced to the reviewer as a concern. The reviewer resolves by providing a specific target during approval or by issuing `/modify target: <value>`. The bundle stays in `in_review` until the target is resolved.

### Control-plane vs. product content boundary

The boundary is a file-classification rule. An agent classifying a file as control-plane or product content applies this decision tree without judgment:

**Control-plane content** (all of the following):
- Any file under `specs/`, `design/`, `templates/`, `memory/` in the control-plane repo
- `settings.json`, `settings.local.json`, `.claude/settings.json`
- Any file under `.github/` in the control-plane repo
- Orchestrator source code: any `.py` file under paths matching `orchestrator/`, `mcp_server/`, `worker_runner/`
- Worker base-image Dockerfiles: any `Dockerfile` under `docker/`
- Agent prompt templates: files matching `prompts/*.md` or `prompts/*.yaml` in the control-plane repo
- Product-specific agent overrides: files under `memory/products/<slug>/agent-overrides.yaml` (these are product content semantically but live in the control-plane repo administratively; they are classified as control-plane for target-determination purposes)
- The control-plane repo's own `AGENTS.md` at the repo root

**Product content** (all of the following):
- Any file in a product repo (any repo listed in `memory/products/registry.json`)
- Application source code, tests, product Dockerfiles, product CI workflows
- Product documentation: `docs/`, `README.md`, `INSTALL.md`, `DEPLOY.md`, `CHANGELOG.md` in a product repo
- A product repo's `AGENTS.md` at the product repo root (distinct from the control-plane `AGENTS.md`)

**Ambiguous cases resolved explicitly:**

- **Agent prompts that are product-specific.** They live under `memory/products/<slug>/agent-overrides.yaml` in the control-plane repo. Classified as control-plane content because they are centrally managed configuration, not product source code. Modifying them requires a `control-plane` target bundle.
- **Templates (`templates/new-product-repo/`).** These are control-plane content. When instantiated into a new product repo during new-repo flow, the instantiated copy becomes product content. Modifying templates requires a `control-plane` target bundle and affects only future product repos.
- **Shared utility libraries.** If a library is used by multiple products, it belongs in its own product repo (`existing-repo:<library-name>`). If it's part of the orchestration system (e.g., a shared RPC client library used by workers), it's control-plane content.
- **`AGENTS.md` files.** The `AGENTS.md` at the control-plane repo root is control-plane content. The `AGENTS.md` at each product repo root is product content. They are distinct files in different repos and are never confused.

### Mechanics per value

#### `new-repo`

The complete sequence from bundle approval to repo existence:

**Worker class.** A `lightweight` worker executes the repo-creation sequence (does not need developer-class resources; it's making API calls and writing scaffold files). Capability grants needed:
- `secrets: github-app-installation-token` (delivery: rpc)
- `network: api.github.com` (egress, HTTPS)
- `filesystem: write` to the worker's scratch directory for scaffold generation
- `process.exec: git, gh` (for git operations)
- `rpc.methods: [artifact.publish]`

**Sequence:**

1. **Repo name resolution.** The worker reads the bundle proposal's suggested repo name (the slug, derived from the bundle title per the naming convention in `settings.json`). The worker calls the GitHub API (`GET /repos/{org}/{slug}`) to check for name collisions. On collision: the worker appends a numeric suffix (`-2`, `-3`, ...) and re-checks until an available name is found. The resolved name is written to the proposal's `target` field (updated to `existing-repo:<resolved-slug>` after creation, but during execution the target remains `new-repo`).

2. **Scaffold generation.** The worker checks out `templates/new-product-repo/` from the control-plane repo and instantiates it with template variable substitution:
   - `{{PRODUCT_NAME}}` → human-readable name from the bundle title
   - `{{PRODUCT_SLUG}}` → resolved repo slug
   - `{{PRODUCT_DESCRIPTION}}` → first paragraph of the bundle RFC
   - `{{ORIGINATING_BUNDLE_ID}}` → the bundle's ULID
   - `{{CREATED_AT}}` → ISO8601 timestamp
   - `{{DEFAULT_BRANCH}}` → "main"

   The scaffold directory structure:
   ```
   <slug>/
     README.md
     docs/
       architecture.md          # placeholder with bundle RFC summary
       api-reference.md         # empty placeholder
       data-model.md            # empty placeholder
       decisions.md             # initialized with "Created by bundle <id>"
     INSTALL.md
     DEPLOY.md
     AGENTS.md                  # pre-populated with product context
     LICENSE                    # from template
     .github/
       ISSUE_TEMPLATE.md
       PULL_REQUEST_TEMPLATE.md
       CODEOWNERS               # populated per below
       workflows/
         ci.yaml                # lint + test + build pipeline
     CHANGELOG.md               # initial entry from bundle RFC
     docker-compose.yaml        # reproducible deploy mechanism
   ```

3. **CODEOWNERS population.** The `CODEOWNERS` file is generated with the following rules:
   - `*` → the reviewer's GitHub username (from `settings.json` under `reviewer.github_username`)
   - `.github/` → the reviewer's GitHub username
   - The GitHub App bot identity is NOT added as a code owner, so bot-authored PRs are not auto-approved by CODEOWNERS

4. **Repo creation.** The worker calls GitHub API:
   ```
   POST /orgs/{org}/repos
   {
     "name": "<slug>",
     "description": "<first line of bundle RFC>",
     "private": true,             # from settings.json default
     "has_issues": true,
     "has_projects": false,
     "has_wiki": false,
     "default_branch": "main",
     "auto_init": false
   }
   ```
   Authentication: GitHub App installation token fetched via `secrets.fetch("github-app-installation-token")`.

5. **Initial push.** The worker initializes a git repo in the scaffold directory, adds all files, commits with message `"Initial scaffold from bundle <bundle-id>"`, and pushes to `main`:
   ```
   git init
   git add -A
   git commit -m "Initial scaffold from bundle <bundle-id>"
   git remote add origin https://x-access-token:{token}@github.com/{org}/{slug}.git
   git push -u origin main
   ```

6. **Branch protection.** The worker calls GitHub API:
   ```
   PUT /repos/{org}/{slug}/branches/main/protection
   {
     "required_status_checks": {"strict": true, "contexts": ["ci"]},
     "enforce_admins": false,
     "required_pull_request_reviews": {
       "required_approving_review_count": 1,
       "dismiss_stale_reviews": true,
       "require_code_owner_reviews": true
     },
     "restrictions": null,
     "allow_force_pushes": false,
     "allow_deletions": false
   }
   ```

7. **Registry update.** The worker publishes a `registry-update` artifact and the orchestrator appends to `memory/products/registry.json`:
   ```json
   {
     "product_slug": "<slug>",
     "repo_name": "<org>/<slug>",
     "repo_url": "https://github.com/<org>/<slug>",
     "originating_bundle_id": "<bundle-id>",
     "created_at": "<ISO8601>",
     "status": "active"
   }
   ```

8. **Artifact publication.** The worker publishes:
   - `new-repo-result` artifact: `{slug, url, clone_url, created_at}`
   - This is referenced by subsequent worker tasks in the same bundle that need to operate on the new repo

9. **Subsequent workers.** The remaining worker tasks in the bundle's DAG target the new repo. They clone it (via the GitHub App token), create per-worker branches off `main`, and follow the standard execution flow from Bundle lifecycle: execution and integration.

**If the bundle is aborted after repo creation:** the repo is left in place with its scaffold and any partial feature branches. The registry entry's `status` is set to `abandoned`. The repo's README is not modified (it still points to the originating bundle). A follow-up bundle can target the repo with `target: existing-repo:<slug>`.

**Bundle outcome_json record for new repos:**
```json
{
  "outcome": {
    "status": "shipped",
    "rationale": "new product repo created and verified"
  },
  "product_artifacts": {
    "spawned_repos": [{
      "name": "<slug>",
      "url": "https://github.com/<org>/<slug>",
      "registry_key": "<slug>"
    }]
  }
}
```

**New repo README reference to originating bundle:**
The scaffold populates the README with:
```markdown
# <product-name>

<product-description>

---
*Created by [bundle <bundle-id>](<control-plane-repo-url>/issues/<issue-number>)*
```

#### `existing-repo:<name>`

**Repo name resolution.** The `<name>` is resolved against `memory/products/registry.json` by exact match on `product_slug`. If the name does not exist in the registry, the orchestrator rejects the bundle at planning time (before execution begins) with error: `INVALID_TARGET: repo "<name>" not found in memory/products/registry.json`. The bundler can also query the GitHub API as a fallback (to detect repos that exist but are not in the registry), but the registry is authoritative; a repo that exists on GitHub but not in the registry cannot be targeted.

**Execution flow.** Workers operate in the target product repo using the same pattern as the control-plane flow (Bundle lifecycle: execution and integration):
- A bundle base branch is created off the target repo's default branch (`main`), named `bundle/<bundle-id>`.
- Each worker gets its own worktree on a sub-branch: `bundle/<bundle-id>/worker-<n>`.
- DAG-order merging proceeds identically: workers read from the merged state of their predecessors.
- Final integration merge goes to `bundle/<bundle-id>`.
- On verification pass, the bundle branch is merged to `main` via a PR (same as control-plane flow).

**What's different from control-plane flow:** Nothing mechanical. The repo is different, the permissions are the same (the GitHub App has access to all repos in the org), and the worker lifecycle is identical. The difference is semantic: `existing-repo` bundles are product changes, not control-plane changes, so mandatory-review triggers for control-plane modification do not apply. The repo's own security-sensitive path patterns (from `settings.json`) determine whether the bundle gets the auth/billing/secrets/PII elevated review.

#### `control-plane`

**Execution flow.** The bundle operates against the control-plane repo itself. Mechanically identical to `existing-repo:<control-plane-slug>` except:

1. **Pre-execution snapshot.** Before the first worker task begins, the orchestrator creates a `control-plane-snapshot` global artifact containing the full state of the control-plane repo at that moment (a tarball or git bundle of the current HEAD). This is stored with extended retention (90 days) and is distinct from git history, providing a clean rollback baseline.

2. **Mandatory-review triggers.** In addition to the existing "modification to control-plane code or `settings.json`" trigger, the following are also mandatory-review for control-plane bundles:
   - Any modification to `AGENTS.md` at the control-plane repo root
   - Any modification to `memory/capabilities/manifest.md`
   - Any modification to agent prompt templates (`prompts/*.md`, `prompts/*.yaml`)
   - Any modification to worker base-image Dockerfiles (`docker/*`)
   - Any modification to `templates/new-product-repo/`

3. **No auto-ship.** Control-plane bundles can never auto-ship, regardless of complexity and risk scores. This is enforced by the approval matrix evaluator (see Pre-execution review track integration with approval matrix in the main spec).

### Approval matrix interaction

**`target: new-repo` is a mandatory-review trigger.** Added to the `mandatory_review_triggers` list in `settings.json`. Rationale: creating a repository is an irreversible namespace action (deleting a repo burns the URL and fragments clone history), changes the org's repo inventory permanently, and should always require explicit human consent.

**`target: control-plane` triggers the existing mandatory-review rule** for "modifying control-plane code or `settings.json`." The additional control-plane triggers listed above extend this rule. All control-plane bundles are full human review, never auto-ship.

**`target: existing-repo:<name>` does not trigger additional mandatory review** beyond what the bundle's content triggers (auth, billing, secrets, PII, etc.). The repo's security-sensitive path patterns in `settings.json` govern.

### Cross-target bundles: **rejected for v1.1**

A bundle with a single `target:` value cannot modify files in both the control-plane and a product repo, nor in two product repos. This is an explicit constraint.

**What the bundler does when an idea naturally spans both:**

1. The bundler detects the cross-target scope during planning (the classification step in the decision rule algorithm).
2. The bundler does NOT silently split the idea. It surfaces the cross-target scope as a concern: "This idea spans both the control-plane (adding a capability) and the api repo (using the capability). The system requires single-target bundles. Recommended: split into two bundles and link via related_bundle_ids."
3. The reviewer sees the concern during approval. The reviewer can:
   - Accept the recommendation: reject this bundle with `/reject split into two` and file two separate inputs.
   - Force a single target: `/modify target: control-plane` to scope only the control-plane work, deferring the product work.
   - Override: in v1.1, there is no override for cross-target. The system rejects cross-target bundles at schema validation.

**Rationale.** Cross-target bundles complicate every lifecycle operation: approval (which repo's triggers?), execution (workers span repos, integration doesn't exist), rollback (rolling back one repo's changes while leaving the other creates an inconsistent state). The complexity is disproportionate to the use case volume in v1.1. The two-bundle workaround with `related_bundle_ids` covers the common case (capability plus first use). Bundle independence is assumed throughout v1.1; cross-target bundles would make bundles dependent on each other by design.

**Escape hatch.** A `control-plane` bundle may modify `memory/products/<slug>/agent-overrides.yaml`, which is product-specific configuration stored in the control-plane. This is the only sanctioned cross-cutting modification. The escape hatch is narrow and deliberately documented so the implementing agent doesn't generalize it.
