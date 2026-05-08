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

---

## Bundle state machine

The bundle state machine is the authoritative source for what transitions are legal, what triggers them, and what side effects they produce. It is implemented as a single Python class in the orchestrator core, `BundleStateMachine`, with one method per legal transition. Each method validates the current state, performs the transition inside a SQLite transaction, writes audit entries, and enqueues any required events to the executor's event pump.

### States

Twelve states. Each is a string enum value stored in `bundles.state`.

| State | Enum value | Description |
|-------|-----------|-------------|
| `PROPOSED` | `"proposed"` | Bundler has produced a proposal; awaiting pre-execution review |
| `IN_REVIEW` | `"in_review"` | Pre-execution review tracks are running |
| `APPROVED` | `"approved"` | Bundle passed review and approval; awaiting execution start |
| `IN_PROGRESS` | `"in_progress"` | DAG executor is driving worker tasks |
| `PAUSED` | `"paused"` | Execution halted; workers idle, state preserved |
| `REDIRECTING` | `"redirecting"` | Paused bundle is being re-planned; transient |
| `VERIFYING` | `"verifying"` | All workers complete; QA agent running post-execution verification |
| `COMPLETE` | `"complete"` | Terminal: shipped successfully |
| `PARKED` | `"parked"` | Terminal: work completed but not merged; preserved |
| `FAILED` | `"failed"` | Terminal: execution or verification failed; partial state preserved |
| `REJECTED` | `"rejected"` | Terminal: rejected during review; no execution |
| `ABORTED` | `"aborted"` | Terminal: reviewer killed bundle mid-flight; partial state preserved |

### Transition table

Each row is `(from_state, trigger, to_state, actor, side_effects)`. Every transition is one SQLite transaction.

| # | From | Trigger | To | Actor | SQLite writes | Event enqueued |
|---|------|---------|----|-------|---------------|----------------|
| 1 | (none) | `bundle_input_received` | `PROPOSED` | Orchestrator | INSERT `bundles` row, INSERT `audit_log` | (none) |
| 2 | `PROPOSED` | `bundler_completed` | `IN_REVIEW` | Bundler agent | UPDATE `bundles.state`, UPDATE `bundles.proposal_json`, INSERT `audit_log`, INSERT pre-execution track dispatch records | `review_tracks_dispatched` |
| 3 | `IN_REVIEW` | `review_tracks_completed` | `PROPOSED` | Review track agent (on finding blocking issue) | UPDATE `bundles.state`, UPDATE `bundles.concerns_json`, INSERT `audit_log` | (none) |
| 4 | `IN_REVIEW` | `approval_matrix_approved` | `APPROVED` | Orchestrator (auto) or Reviewer (manual) | UPDATE `bundles.state`, UPDATE `bundles.approved_at`, UPDATE `bundles.approved_by`, INSERT `approval_decisions`, INSERT `audit_log` | (none) |
| 5 | `IN_REVIEW` | `approval_matrix_rejected` | `REJECTED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `approval_decisions`, INSERT `audit_log` | (none) |
| 6 | `APPROVED` | `execution_started` | `IN_PROGRESS` | Orchestrator | UPDATE `bundles.state`, INSERT `audit_log` | `bundle_execution_started` |
| 7 | `APPROVED` | `reviewer_rejected` | `REJECTED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json` (superseding prior approve), INSERT `approval_decisions`, INSERT `audit_log` | (none) |
| 8 | `IN_PROGRESS` | `reviewer_paused` | `PAUSED` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log`, INSERT `steering_events` in `outcome_json` | `bundle_pause_requested` |
| 9 | `IN_PROGRESS` | `all_exit_nodes_terminal` | `VERIFYING` | Orchestrator | UPDATE `bundles.state`, INSERT `audit_log` | `verification_requested` |
| 10 | `IN_PROGRESS` | `reviewer_aborted` | `ABORTED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log` | `bundle_abort_requested` |
| 11 | `PAUSED` | `reviewer_resumed` | `IN_PROGRESS` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log`, INSERT `steering_events` in `outcome_json` | `bundle_resume_requested` |
| 12 | `PAUSED` | `reviewer_redirected` | `REDIRECTING` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log`, INSERT `steering_events` in `outcome_json` | `bundle_redirect_requested` |
| 13 | `PAUSED` | `reviewer_aborted` | `ABORTED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log` | `bundle_abort_requested` |
| 14 | `REDIRECTING` | `replan_completed` | `IN_REVIEW` | Bundler agent | UPDATE `bundles.state`, UPDATE `bundles.proposal_json` (new proposal), INSERT `audit_log`, INSERT re-plan provenance in a `dag_expansions`-like record | `review_tracks_dispatched` |
| 15 | `REDIRECTING` | `reviewer_paused` | `PAUSED` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log` | (none; re-plan discarded) |
| 16 | `REDIRECTING` | `reviewer_aborted` | `ABORTED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log` | `bundle_abort_requested` |
| 17 | `VERIFYING` | `verification_passed` | `COMPLETE` | QA agent + Orchestrator (or Reviewer for manual ship) | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json` (full), INSERT `audit_log`. If auto-ship: INSERT `approval_decisions` with `actor = "system"` | (none) |
| 18 | `VERIFYING` | `reviewer_parked` | `PARKED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log`, INSERT `approval_decisions` | (none) |
| 19 | `VERIFYING` | `verification_failed_no_rollback` | `FAILED` | QA agent | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log` | (none) |
| 20 | `VERIFYING` | `verification_failed_auto_rollback` | `IN_PROGRESS` | QA agent + Orchestrator | UPDATE `bundles.state` (not terminal; rollback in flight), INSERT `audit_log` | `rollback_bundle_spawned` |
| 21 | `VERIFYING` | `verification_failed_manual_rollback` | `IN_PROGRESS` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log`, INSERT `approval_decisions` | `rollback_bundle_spawned` |
| 22 | `COMPLETE` | `rollback_requested` | `COMPLETE` | Reviewer | (bundle stays COMPLETE; rollback is a new bundle), INSERT `steering_events` in original bundle's `outcome_json`, INSERT `audit_log` | `rollback_bundle_spawned` |
| 23 | `FAILED` | `verification_retried` | `VERIFYING` | Reviewer | UPDATE `bundles.state`, INSERT `audit_log` | `verification_requested` |
| 24 | `FAILED` | `reviewer_overridden` | `COMPLETE` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.outcome_json` (status changed to shipped with override note), INSERT `audit_log`, INSERT `approval_decisions` | (none) |
| 25 | `IN_PROGRESS` | `bundle_failed_during_execution` | `FAILED` | Orchestrator (on unrecoverable DAG failure) | UPDATE `bundles.state`, UPDATE `bundles.completed_at`, UPDATE `bundles.outcome_json`, INSERT `audit_log` | (none; in-flight workers left running per executor design) |

### Transitions requiring special attention

**Transition 3** (`IN_REVIEW → PROPOSED`): This is triggered when a review track finds a blocking issue and the bundler must revise. Not the same as `/modify` (which is reviewer-initiated). The review track agent sets `bundles.concerns_json` with the blocking finding. The bundler sees this on next planning dispatch.

**Transition 8** (`IN_PROGRESS → PAUSED`): The `bundle_pause_requested` event triggers the Pause executor mechanics (see Mid-flight steering). The transition itself commits immediately; the pause may take seconds-to-minutes for in-flight workers to finish their current step.

**Transition 14** (`REDIRECTING → IN_REVIEW`): The replay provenance record stored in `dag_expansions` (or equivalent) captures the relationship:
```json
{
  "kind": "redirect_replan",
  "prior_dag_hash": "<content hash of the pre-redirect DAG>",
  "new_dag_hash": "<content hash of the post-redirect DAG>",
  "completed_nodes_retained": ["<node_id>", ...],
  "completed_nodes_discarded": ["<node_id>", ...],
  "redirect_instructions": "<reviewer's instructions>",
  "snapshot_artifact_ref": "<descriptor of the bundle-state-snapshot>"
}
```

**Transition 20** (`VERIFYING → IN_PROGRESS`): Auto-rollback. The bundle enters `IN_PROGRESS` because rollback execution is happening. If rollback completes, the bundle transitions to `FAILED` (the original work failed verification). If rollback itself fails, the bundle transitions to `FAILED` with `rollback_failed: true` in `outcome_json`. The terminal state is always `FAILED` for verification-failed bundles, whether rollback succeeded or not. The difference is whether the rollback repaired the damage.

**Transition 22** (`COMPLETE → COMPLETE`): Rollback of a shipped bundle. The bundle stays `COMPLETE` (it was shipped; the net is zero after rollback). The audit trail records the rollback. A bundle that has been shipped and then rolled back is still `COMPLETE`, with `outcome_json.steering_events` recording the rollback.

**Transition 25** (`IN_PROGRESS → FAILED`): Unrecoverable DAG failure during execution (not verification failure). Per the executor design, in-flight workers are NOT auto-cancelled. The reviewer can issue Abort to clean up.

### Illegal transitions

The state machine validates every requested transition. Illegal transitions raise `IllegalTransitionError` with a structured payload:

```python
class IllegalTransitionError(Exception):
    def __init__(self, current_state: str, attempted_transition: str, reason: str):
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        self.reason = reason
```

The error is serialized for external surfaces as:
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32001,
    "message": "illegal_transition",
    "data": {
      "current_state": "complete",
      "attempted_transition": "approve",
      "reason": "Bundle is complete; transitions from terminal states are not allowed."
    }
  },
  "id": null
}
```

Full enumeration of all illegal transitions is impractical (12 states produce 132 possible from-to pairs, minus 25 legal = 107 illegal). The state machine validates by checking: is the (from, to) pair in the legal set? If not, compute the reason from a template. The implementing agent stores the legal transition set as a frozenset of `(from_state, to_state)` tuples and checks membership.

**Error messages for common illegal transitions:**

| Attempted | Reason template |
|-----------|----------------|
| Any transition from a terminal state (`COMPLETE`, `PARKED`, `FAILED`, `REJECTED`, `ABORTED`) | "Bundle is {state}; transitions from terminal states are not allowed." |
| `PROPOSED → IN_PROGRESS` | "Bundle has not been reviewed. Wait for pre-execution review and approval." |
| `APPROVED → PROPOSED` | "Bundle is approved. Use /modify to revise before execution starts." |
| `IN_PROGRESS → APPROVED` | "Bundle is executing. Pause first, then Redirect to re-plan." |
| `PAUSED → VERIFYING` | "Bundle is paused. Resume or Redirect first." |
| `PAUSED → APPROVED` | "Bundle is paused mid-execution. Resume, Redirect, or Abort." |
| `REDIRECTING → APPROVED` | "Bundle is re-planning. Wait for re-plan completion or Abort." |
| `VERIFYING → APPROVED` | "Bundle is in verification. Wait for verification result." |
| Any transition triggered by a non-reviewer actor that requires reviewer authority | "Only the reviewer can {trigger} a bundle. Actor {actor} is not authorized." |

### Surface observability

**SQLite.** The `bundles.state` column is the authoritative source. Updated in the same transaction as the transition. `bundles.outcome_json` accumulates `steering_events` incrementally.

**MCP.** The `studio://bundles/{id}` resource reads `bundles.state` and `bundles.outcome_json` directly. State changes are visible on the next resource fetch. No push-based notification to MCP in v1.1.

**GitHub Issues.** On every state transition, the orchestrator updates the bundle's GitHub Issue:
- Issue body: appends a transition timeline entry: `"[{timestamp}] {from_state} → {to_state} by {actor} via {surface}"`
- Labels: the old state label is removed, the new state label is added. Label names: `state/proposed`, `state/in-review`, `state/approved`, `state/in-progress`, `state/paused`, `state/redirecting`, `state/verifying`, `state/complete`, `state/parked`, `state/failed`, `state/rejected`, `state/aborted`.

**CLI.** `studio show <bundle-id>` displays: current state, transition history (from `audit_log`), and key fields from `outcome_json`.

### Implementation notes

The state machine is a class with:
- `legal_transitions: frozenset[tuple[str, str]]` — the 25 legal (from, to) pairs
- `transition(from_state, trigger, to_state, actor, surface)` — the single entry point for all transitions. Validates legality, opens SQLite transaction, applies side effects, commits, enqueues events, returns.
- One private method per transition (`_transition_1_bundle_input_received`, etc.) for clarity, called by the dispatch dict.

The state machine does NOT mutate executor state directly. It enqueues events (`bundle_pause_requested`, `bundle_abort_requested`, etc.) into the executor's event queue. The executor's event pump picks up these events on the next tick and performs the executor-level actions (sending RPCs to workers, cancelling workers, re-ticking the scheduler). Per the DAG executor design framing: "the executor is event-driven at the edges and synchronous at the core... Each drain tick takes one event, updates SQLite in one transaction."

The bundle state machine and the DAG executor communicate through the event queue and SQLite, not through direct method calls. The state machine writes `bundles.state = PAUSED` and enqueues `bundle_pause_requested`. The executor reads the event, sends `worker.pause()` RPCs, and writes node states to `dag_nodes`. The two state spaces (bundle state and DAG node states) are in separate tables and are not locked together.

---

## Mid-flight steering mechanics

### Pause

**Trigger:** Reviewer issues Pause from any surface. Legal from `IN_PROGRESS` only.

**Complete sequence:**

1. **State machine transition.** `bundles.state` transitions `IN_PROGRESS → PAUSED`. The transition commits immediately. `steering_events` in `outcome_json` records: `{action: "pause", at: <ISO8601>, by: <actor>, note: null}`. The `bundle_pause_requested` event is enqueued.

2. **Executor receives event.** On the next executor tick, the event pump processes `bundle_pause_requested`:
   - Identifies all `dag_nodes` rows for this bundle in state `running`.
   - Sends `worker.pause()` RPC to each such worker.
   - Sets a `scheduler_halted` flag in the executor's in-memory state for this bundle (prevents new dispatches from the ready set).
   - The event pump continues to process other events (worker completions still arrive).

3. **Worker behavior.** On receiving `worker.pause()`:
   - The worker finishes its current step (the in-progress tool call, test run, file write, or LLM API call).
   - The worker does NOT checkpoint mid-step. Rationale: mid-step checkpointing requires workers to serialize and reconstitute state, which is more coupling than the kill-all-on-crash policy accepts (DAG executor, Rejected alternatives: mid-node checkpointing).
   - After completing the current step, the worker calls `worker.final_report` with a `{"paused": true, "current_phase": "<phase>", "progress": {...}}` payload, then halts.
   - The worker does NOT exit the process. It stays alive, connected to the RPC channel, waiting for `worker.resume()`.

4. **Worker state updates.** As each worker completes its pause, the orchestrator transitions the node: `dag_nodes.state: running → paused`. `node_state_history` records: `{from_state: "running", to_state: "paused", reason: "bundle_paused"}`.

5. **Surface state.** While workers are finishing their steps (between transition commit and all workers paused), the surface shows `PAUSED` state. The GitHub Issue body appends: "Pause requested. Waiting for N workers to finish current steps..." When all workers have paused, the body updates: "Paused. All workers idle."

**What is preserved:** All worker worktrees (git worktrees on disk). All `dag_nodes` rows with current states. The SQLite database state. The bundle's feature branch.

**What is logged:** `audit_log`: `{event_type: "bundle_paused", subject_type: "bundle", subject_id: <bundle_id>, payload: {paused_by: <actor>, surface: <surface>}}`. Per-worker: `node_state_history` entries.

### Resume (from Pause)

**Trigger:** Reviewer issues Resume from any surface, optionally with a note string. Legal from `PAUSED` only.

**Complete sequence:**

1. **State machine transition.** `bundles.state` transitions `PAUSED → IN_PROGRESS`. `steering_events` records: `{action: "resume", at: <ISO8601>, by: <actor>, note: "<note or null>"}`. `bundle_resume_requested` event enqueued.

2. **Executor receives event.** On next tick:
   - Reads the `steering_events` note from `outcome_json`.
   - If a note is present, the executor stores it in the bundle's execution context (an in-memory dict in the executor, not a SQLite column; notes are ephemeral and consumed by the next worker dispatch).
   - For each `dag_nodes` row in state `paused`, sends `worker.resume()` RPC.
   - Removes the `scheduler_halted` flag.
   - Re-ticks the scheduler: recomputes the ready set from current node states.

3. **Worker behavior.** On receiving `worker.resume()`:
   - The worker reads its current task spec. If the executor's note is present, the worker prepends it to its working context (visible to the LLM on the next prompt).
   - The worker resumes execution from where it paused (worktree intact, task spec unchanged except for the optional note).

4. **Node state updates.** Each resumed node transitions `paused → running` in `dag_nodes`.

**Note delivery mechanism.** Notes are delivered via `worker.resume()` RPC response, not via `worker.inject_context`. The `resume` RPC returns `{note: "<note or null>"}` alongside the standard ack. Worker reads the note and injects it into its LLM context. This is simpler than a separate `inject_context` call and ensures the note is received before the worker resumes work.

### Redirect

**Trigger:** Reviewer issues Redirect from any surface with a `new_instructions` string. Legal from `PAUSED` only. If the bundle is in `IN_PROGRESS`, the reviewer must Pause first, then Redirect.

**Design ratified:** The natural answer from the deferred items is ratified without modification: "discard current DAG, run planner on current worktree state as fresh bundle, completed work as baseline."

**Complete sequence:**

1. **State machine transition.** `bundles.state` transitions `PAUSED → REDIRECTING`. `steering_events` records: `{action: "redirect", at: <ISO8601>, by: <actor>, note: "<new_instructions>"}`. `bundle_redirect_requested` event enqueued.

2. **Executor receives event.** On next tick:
   - **Snapshot current state.** The executor produces a `bundle-state-snapshot` artifact. This contains:
     - A git tree-ish reference: the merged state of all completed worker sub-branches, resolved to a concrete commit SHA. If no workers completed, the bundle base branch HEAD.
     - A JSON manifest of all `dag_nodes` rows in terminal states (`completed`, `failed`, `skipped`, `cancelled`), each with: `node_id`, `terminal_state`, `output_artifacts` (list of descriptor+hash), and `branch_ref` (the worker's sub-branch name).
     - The prior DAG structure (nodes and edges) for the audit trail.
   - The snapshot is published as a bundle-scoped artifact: `{namespace: bundle, name: redirect-snapshot, version: "<redirect-count>", content_type: application/json}`.

3. **Re-planning dispatch.** The orchestrator spawns a new planning task (a narrow planning action within the existing bundle's identity, not a full new bundle lifecycle). The planner agent receives:
   - The original `bundle_input`.
   - The reviewer's `new_instructions` (from the redirect steering event).
   - The `bundle-state-snapshot` artifact reference.
   - Memory context (calibration data, prior decisions) same as original planning.

4. **Planner produces new DAG.** The planner evaluates which completed work is reusable:
   - If worker-3 completed successfully and its output artifacts are still valid under the new instructions, the new DAG references those artifacts as inputs to successor nodes and does NOT re-execute worker-3's work. Worker-3's node is marked `retained` in the re-plan provenance.
   - If the new instructions invalidate worker-3's work (e.g., "change the auth module to use JWT instead of sessions" when worker-3 implemented session auth), the new DAG includes replacement workers that overwrite or supersede worker-3's output. Worker-3's node is marked `discarded` in the re-plan provenance.
   - The planner decides. The reviewer can override during re-approval.

5. **Capability manifest.** The new DAG's node capability manifests must be subsets of the original bundle manifest (the bundle was approved with that manifest and the approval carries over). If the new instructions require capabilities beyond the original manifest, the planner must request them via the capability-request flow, which escalates to the reviewer. Redirect does not silently expand the capability envelope.

6. **State machine transition.** On planner completion, `bundles.state` transitions `REDIRECTING → IN_REVIEW`. The new proposal replaces the old one in `bundles.proposal_json`. The re-plan provenance record (see Transition 14 in the state machine) is written to a new table or as a JSON field in `bundles.outcome_json`. The `review_tracks_dispatched` event is enqueued (abbreviated review: only the delta from the prior review is examined, unless review track agents self-escalate to full re-review).

7. **Approval.** The new DAG goes through the approval matrix. The bundle transitions `IN_REVIEW → APPROVED → IN_PROGRESS` and execution resumes with the new DAG.

**What "current worktree state" means concretely:** The git merge-base of all completed workers' sub-branches, resolved to a commit SHA. The executor runs: `git merge-base --octopus <worker-1-branch> <worker-2-branch> ... <worker-N-branch>` to find the common ancestor of all completed work. If the merge-base command fails (workers diverged too far), the snapshot falls back to the last successfully merged integration point (the bundle feature branch as of the last completed DAG-level merge). This is a rare edge case.

**Audit trail reconstruction.** A human or agent reading the audit log can reconstruct the full history of a redirected bundle by:
1. Reading `bundles.outcome_json.steering_events` for the sequence of redirect actions.
2. For each redirect, reading the `redirect-snapshot` artifact (referenced by the re-plan provenance record) for the state at redirect time.
3. Reading the sequence of `dag_expansions` (or equivalent) rows with `kind = "redirect_replan"` for the DAG relationship metadata.
4. The bundle id is constant throughout; all records share the same `bundle_id`.

### Abort

**Trigger:** Reviewer issues Abort from any surface. Legal from `APPROVED`, `IN_PROGRESS`, `PAUSED`, `REDIRECTING`. Abort from `PROPOSED` or `IN_REVIEW` is treated as Reject (Transition 5), not Abort.

**Complete sequence:**

1. **State machine transition.** `bundles.state` transitions to `ABORTED`. `bundles.completed_at` set. `bundles.outcome_json` populated with `{outcome: {status: "aborted", rationale: "<reason or 'aborted by <actor>'">}}`. `steering_events` records: `{action: "abort", at: <ISO8601>, by: <actor>, note: null}`. `bundle_abort_requested` event enqueued.

2. **Executor receives event.** On next tick:
   - Identifies all `dag_nodes` rows for this bundle in state `running`, `ready`, `pending`, or `paused`.
   - **Cancellation order: all simultaneously**, not reverse-DAG-order. The executor sends `worker.cancel(reason="bundle_aborted")` RPC to every worker with an active RPC connection. Rationale: serial cancellation would add latency with no benefit; workers don't depend on each other for cleanup.
   - Cancellation protocol per worker (from DAG executor Aggregator mechanics):
     - 30-second grace period for the worker to finish its current step and commit.
     - SIGTERM after grace.
     - SIGKILL 10 seconds after SIGTERM.
   - Workers in `ready` or `pending` state (not yet spawned) are transitioned directly to `cancelled` without RPC.
   - Workers in `paused` state (already idle) are transitioned to `cancelled` without grace period.

3. **Draft PR handling.** If the bundle opened draft PRs (against the control-plane repo or product repos), the orchestrator closes them via GitHub API: `PATCH /repos/{org}/{repo}/pulls/{number} {"state": "closed"}`. A closing comment is posted: "Bundle aborted by reviewer. Feature branch `<branch>` is preserved for recovery."

4. **Worktree handling.** Worker git worktrees are **not deleted**. They remain on disk. The periodic background sweep (Artifact Protocol, Lifecycle and garbage collection) collects worktrees for aborted bundles after the configured retention period (default 30 days, same as failed bundles). The reviewer can force cleanup via `studio cleanup <bundle-id>`.

5. **Terminal SQLite state:**
   - `bundles.state = "aborted"`
   - `bundles.completed_at = <timestamp>`
   - `bundles.outcome_json` = lightweight outcome record
   - `dag_nodes` for the bundle: nodes that were `running` → `cancelled`; nodes that were `paused` → `cancelled`; nodes that were `ready`/`pending` → `cancelled`; nodes that were already `completed`/`failed`/`skipped` → unchanged.
   - `dag_edges`: unchanged (frozen at pre-abort state).

**What is explicitly preserved:**
- `artifact_refs` rows: **preserved.** They remain in the table until the aborted-bundle retention window expires and the periodic GC sweep collects them. This allows recovery of partial work artifacts.
- Capability grants made specifically for this bundle: **revoked.** The grants were for the bundle's work, which is now dead. `capabilities.revoked_at` and `capabilities.revoke_reason = "bundle_aborted"` are set.
- `audit_log` entries: **never deleted.** The full audit trail from bundle creation to abort is immutable.
- `bundles` row: remains with `state = "aborted"`.
- Git branches: feature branch and worker sub-branches remain. Not deleted by the system.

**What is not preserved:**
- In-flight RPC calls are abandoned.
- Workers killed via SIGKILL lose uncommitted working state in their worktree.
- The bundle's in-memory executor state (ready set, scheduler state) is discarded.

### Rollback

**Design decision: rollback is a new bundle**, not a special bundle kind, not a direct orchestrator action.

**Rationale.** Rollback is a software change like any other: it touches the same repos, code paths, and deployment mechanisms as the original work. It needs capability grants, a task DAG, worker execution, and verification. A rollback that fails is itself a failed bundle with its own post-mortem. The alternative (direct orchestrator action: git revert + re-deploy) is simpler for the common case but fails for nontrivial rollbacks (merge conflicts, data migrations that must be reversed, side effects in external systems). A rollback-is-a-bundle model handles trivial and nontrivial rollbacks uniformly. The cost is slightly more overhead for simple rollbacks (which get full bundle lifecycle treatment), which is acceptable for v1.1 throughput.

**Trigger paths:**

1. **Manual.** Reviewer issues Rollback from any surface: `studio rollback <bundle-id>` or MCP equivalent. This creates a `bundle_input` with:
   - `idea.body = "Rollback bundle <bundle-id>"` (auto-generated)
   - `parent_bundle_id = <bundle-id>` (the bundle being rolled back)
   - `related_bundle_ids = [<bundle-id>]`
   - `filed_by = <reviewer identity>`
   - `filed_via = <cli|mcp|github_issue>`
   The rollback bundle enters the normal bundle lifecycle (planning, review, approval, execution, verification).

2. **Automatic on QA verification failure.** Triggered when ALL of the following conditions are met:
   - The Verification Report outcome is `failed` or `partial`
   - The bundle's stakes are `Low` (from the approval matrix tier)
   - The Verification Plan's `rollback_plan.machine_executable` is `true`
   - The Verification Plan's `rollback_plan.auto_rollback_eligible` is `true`
   
   When all conditions are met, the orchestrator auto-creates the rollback bundle input (same as manual but with `filed_by = "system"` and `filed_via = "agent_generated"`). The rollback bundle is auto-approved (it's a rollback of a low-stakes bundle) and executes immediately.

**What the rollback executor (the rollback bundle's workers) actually runs:**

The rollback bundle's bundler reads the original bundle's Verification Plan, which includes a `rollback_plan`:
```yaml
rollback_plan:
  machine_executable: bool
  auto_rollback_eligible: bool
  steps:
    - description: str
      kind: git_revert | api_call | deploy_previous | manual
      spec: { ... }   # kind-specific parameters
```

The bundler translates the rollback plan into a task DAG. For a `git_revert` step, the worker runs `git revert <merge-commit>`. For a `deploy_previous` step, the worker re-deploys the previous known-good artifact. For `api_call` steps, the worker makes the specified API calls. The bundler is not bound by the plan; if the plan says "revert commit X" but commit X now has conflicts with later commits, the bundler proposes an alternative (e.g., a manual patch).

**How rollback is verified:**

The rollback bundle gets its own Verification Plan, produced by the QA agent during the rollback bundle's pre-execution review. The QA agent checks:
- The rolled-back state matches the pre-original-bundle state for the files the original bundle touched.
- Unrelated changes that landed after the original bundle are preserved (not accidentally reverted).
- The rolled-back product passes the same CI and smoke tests that the pre-original-bundle state passed.

**Terminal state after rollback:**

- **Successful rollback:** The original bundle stays in `COMPLETE` (it was shipped; the net is zero after rollback but the record is accurate). The rollback event is appended to the original bundle's `steering_events`. The rollback bundle completes with `outcome: shipped`.
- **Failed rollback:** The original bundle is annotated with `rollback_failed: true` in `outcome_json`. The reviewer is paged via the notification surface. The rollback bundle completes with `outcome: failed`. This is the worst case: bad code shipped, and the undo failed too. The reviewer must intervene manually.
- **Auto-rollback (verification-driven):** The original bundle transitions through `VERIFYING → IN_PROGRESS` (Transition 20) during rollback execution. When the rollback bundle completes, the original bundle transitions to `FAILED` (the original work failed verification, even though rollback repaired it). The original bundle's `outcome_json.verification.rollback_triggered` is `true` and `rollback_bundle_id` points to the rollback bundle. Terminal state: `FAILED` for the original, `COMPLETE` (or `FAILED`) for the rollback.

---

## Ratified open questions

The following open questions from the v1.1 spec were put to the PM and decided. These are recorded as resolved; they should be removed from the Open Questions section when this design is integrated.

### Pre-execution review ordering: confirmed

**Decision.** Pre-execution review tracks (adversarial critique, security review, QA verification planning) run before the approval matrix. Their outputs feed the matrix decision. Auto-ship is gated on their results. This ordering is ratified.

**Spec reference.** Open questions: "Pre-execution review ordering" — the sentence "(This ordering was inferred during consolidation; the prior conversation didn't make it explicit. Confirm.)" in Bundle lifecycle: planning and approval is now resolved. Remove the parenthetical.

**How this interacts with the state machine.** The flow is: `PROPOSED → IN_REVIEW` (bundler completes, review tracks dispatch) → review tracks run → `IN_REVIEW → APPROVED` or `IN_REVIEW → REJECTED` (approval matrix evaluates, consuming review track outputs). See Transition 2, 4, 5 in the state machine.

### Modification re-scoring: yes, always

**Decision.** When a bundle is modified via `/modify`, the bundler re-scores complexity and risk. If the new score changes the approval tier, the new tier applies. The prior score is preserved in the audit log for calibration.

**Spec reference.** Open questions: "Modification request re-scoring" — marked as "Not committed." Now committed: re-score always.

**Implementation detail.** The prior score is stored in `bundles.outcome_json.steering_events` as part of each modification event:
```json
{
  "action": "modify",
  "at": "<ISO8601>",
  "by": "<actor>",
  "note": "<instructions>",
  "score_delta": {
    "complexity": {"from": 3, "to": 5},
    "risk": {"from": 1, "to": 2},
    "tier": {"from": "approve-with-summary", "to": "full-review"}
  }
}
```

### Steering vocabulary: all four verbs ratified

**Decision.** Pause, Redirect, Abort, and Rollback are all ratified as specified in this document.

**Spec reference.** Open questions: "Mid-flight steering vocabulary acceptance" — flagged as "Open question: ratify, revise, or expand." Now ratified. The verbs were originally noted as "Claude-recommended after the reviewer explicitly said 'I honestly do not know the answer'" and "folded into the spec without being explicitly ratified." That status is now resolved.

### Default action for summary-tier timeouts: default-hold across the board

**Decision.** Regardless of risk cell, the default when the PM does not respond within the configured window is **hold** (require explicit response). This applies until the calibration loop has accumulated enough history to justify auto-approve on specific cells. The PM can change individual cells in `settings.json`.

**Spec reference.** Open questions: "Default action for summary-tier timeouts" — the previous default was "default-approve after 4 hours for low-risk cells, default-hold for moderate-risk." Now changed to "default-hold across the board."

**Implementation.** The `settings.json` field `approval.default_action` changes from `"approve"` (for low-risk) to `"hold"` for all summary cells. The per-cell override structure remains so the PM can selectively re-enable auto-approve later:
```json
{
  "approval": {
    "summary_tier_default_action": "hold",
    "default_action_overrides": {
      "low_risk_low_complexity": "hold",
      "low_risk_moderate_complexity": "hold",
      "moderate_risk_low_complexity": "hold"
    },
    "summary_timeout_hours": 4
  }
}
```

---

## Resolved items

Every gap, open question, and deferred item this document closes, referenced by the exact name used in the v1.1 spec:

**From Deferred items:**
- `Bundle-level input/output schema` — Resolved by: Bundle input schema and Bundle output schema sections.
- `Two-tier repo boundary` (specifically the `target:` field) — Resolved by: The `target:` field section (decision rule, boundary, mechanics, cross-target policy).
- `Modification during pause: executor's role in re-planning` — Resolved by: Redirect mechanics (re-planning flow ratified, snapshot definition, replay provenance).

**From Open questions and flagged decisions:**
- `Mid-flight steering vocabulary acceptance` — Ratified by PM: all four verbs accepted.
- `Pre-execution review ordering` — Ratified by PM: confirmed, review tracks before approval matrix.
- `Modification request re-scoring` — Ratified by PM: yes, always re-score.
- `Default action for summary-tier timeouts` — Ratified by PM: default-hold across the board.
- `Cooldown duration for the highest-risk tier` — Resolved by: 1 hour, with 24-hour carve-out for bundles flagged `irreversible` (defined in target field section, approval matrix cooldown spec).
- `Multi-surface action ordering and race resolution` — Resolved by: first-write-wins via SQLite serialization, conflict error with context (specified in Bundle lifecycle: planning and approval, Default actions/cooldown/race section in the main spec).
- `Two-tier repo target: field semantics` — Resolved by: The `target:` field section (complete specification).

---

## Remaining open

Items this document could not resolve, with specific blockers:

1. **`irreversible` flag formal schema slot.** The concept is introduced (bundles where rollback is not machine-executable get a 24-hour cooldown instead of 1 hour). The exact schema field in the bundle proposal, its population by the bundler, and its surfacing in the reviewer's UI are deferred. Blocker: needs a schema design pass to add the field to `bundle_output.proposal` and the approval matrix evaluator. Provisional: the field is a boolean `irreversible: bool` in the proposal, default `false`, set by the bundler based on the Verification Plan's `rollback_plan.machine_executable` flag.

2. **Abbreviated review threshold on Redirect.** When a bundle is redirected, the pre-execution review tracks examine "only the delta" from the prior review. The threshold for "delta is large enough to warrant full re-review" is not formally specified. Blocker: needs calibration data on how often abbreviated review misses issues. Provisional: review track agents self-escalate when the delta exceeds their confidence threshold. A formal threshold (e.g., "more than 30% of DAG nodes changed") is a future refinement.

3. **Auto-rollback eligibility for medium-stakes bundles.** Auto-rollback on QA failure is currently restricted to Low-stakes bundles. Expanding to medium-stakes is gated on empirical rollback reliability data. Blocker: no production data exists in v1.1. Provisional: Low-stakes only until rollback bundles demonstrate >95% success rate over at least 20 rollbacks.

4. **Parked bundle lifecycle.** The `PARKED` terminal state exists in the state machine but the workflow around it (how parked bundles are discovered in the review surface, whether they auto-expire, how they are resumed) is not specified. Blocker: needs a separate design pass on parked-bundle UX. Not blocking implementation of the state machine transition itself.

---

## New drift detected

No new contradictions or ambiguities were discovered in the existing spec while writing this document. The existing spec's Drift detected during consolidation section already covers the known supersessions. The four gaps this document closes were explicitly flagged as deferred or open; resolving them does not introduce drift.

One item of note, not drift but worth flagging for the integration pass:

**State machine sketch in Architecture section.** The ASCII diagram in the Architecture section (lines 177-189 in the current spec) shows 8 states and approximately 12 transitions. This document specifies 12 states and 25 transitions. The Architecture section's sketch should be replaced with a reference to the full specification in Bundle lifecycle: execution and integration during the integration pass. The sketch is not wrong (all depicted transitions remain legal) but it is incomplete. The SQLite schema comment for `bundles.state` must also be updated from the current `proposed|approved|in_progress|verifying|complete|failed|rejected` to the full 12-state enum.
