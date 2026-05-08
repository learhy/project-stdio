# Bundle Lifecycle, I/O Schema, and Repository Target Semantics

## Overview

This document specifies the bundle as an end-to-end entity: what goes in, what comes out, where the work lands, and every state the bundle passes through between filing and terminal outcome. It closes the largest remaining gap in the v1.1 agent orchestration spec, which has thorough coverage of internal mechanics (capability manifest, task DAG, executor, artifacts) and approval surfaces but leaves the seams between them unresolved. A bundle's inputs and outputs are not formally specified. The `target:` field is named but undefined. The mid-flight steering vocabulary is folded in but the executor's role in Redirect-driven re-planning is unspecified. Several smaller items (modification re-scoring, multi-surface action ordering, default-action behavior for summary-tier timeouts, cooldown duration, pre-execution review track ordering) are flagged as open questions whose resolution belongs here.

This design is opinionated where the spec allows it, provisional where calibration data is the only honest answer, and explicit about which prior decisions it builds on rather than re-litigates. It is intended to be folded into the v1.1 spec as expansions to the existing Bundle lifecycle sections, with cross-references back-filled in earlier sections. That folding is a separate pass.

**Scope.** Bundle-level input schema, bundle-level output schema, `target:` field semantics (three values, decision rule, control-plane vs. product boundary, new-repo flow end to end, existing-repo and control-plane flows, cross-target bundles), full bundle state machine with transition table and surface observability, mid-flight steering mechanics (Pause, Redirect, Abort, Rollback) at the executor level, pre-execution review track ordering and data flow into the approval matrix, modification request handling and re-scoring, default-action behavior and cooldown duration, multi-surface race resolution, and the post-execution verification handoff seam between QA planning and QA validation.

**Out of scope.** Schema versioning policy, multi-reviewer support, persistent-log Agent Activity Board, external notification relay, k8s deployment specifics, capability manifest review UX, hostname-based egress enforcement, bundle dependency relations. Bundle independence is assumed throughout; that assumption holds.

---

## Bundle inputs

A bundle's inputs are the contract between whoever filed the work and the bundler agent that plans it. The task-level I/O spec in the Task DAG schema is the model: each task declares its inputs (artifacts and params) and outputs (artifacts with required flags and success criteria). The bundle-level schema mirrors that structure at a higher level of abstraction.

### Schema

```yaml
bundle_input:
  idea:
    source: idea_forum | cli | mcp | github_issue | agent_generated
    body: "<free-text request>"
    title: "<optional one-line summary>"

  structured_params:
    target_hint: new-repo | existing-repo:<name> | control-plane | null
    priority_hint: low | normal | high | null
    deadline: <ISO8601 timestamp or null>
    requested_capabilities: [<capability name>, ...]

  parent_bundle_id: <ULID or null>

  related_bundle_ids: [<ULID>, ...]

  attachments:
    - name: <string>
      content_type: <mime-like>
      data_ref: <artifact descriptor or null>
      url: <string or null>

  metadata:
    filed_by: <identity string>
    filed_at: <ISO8601 timestamp>
    filed_via: idea_forum | cli | mcp | github_issue | agent_generated
```

### Field rationale

**`idea`** is the only required field. The `source` discriminator tells the bundler what kind of material it's working with: a one-line CLI request gets different treatment than a structured GitHub Issue with reproductions steps, and an agent-generated proposal (a follow-up bundle triggered by an Investigate decision on a capability request) carries its own framing. The `body` is free-text and deliberately unstructured; the bundler's job is to turn it into a structured proposal.

**`structured_params`** is optional and advisory. The three hints (target, priority, deadline) let the human steer without committing. The bundler may override any hint if the resulting proposal would be incoherent; override reasons are surfaced in the proposal's concerns section. `requested_capabilities` is an early signal: "I think this will need a new API key for SendGrid." The bundler treats these as suggestions, not grants; the capability request still goes through the normal approval flow.

**`parent_bundle_id`** captures lineage. When a bundle is spawned from an Investigate decision on a capability request (Capability Requests Board section), or when an agent proposes follow-up work after completing a bundle, the parent link preserves the through-line. It is not a scheduling dependency; it is provenance metadata. A bundle with a parent is otherwise independent.

**`related_bundle_ids`** is a looser reference: "this is like bundle X" or "this supersedes bundle Y." The bundler consults related bundles during planning for context, but does not block on them.

**`attachments`** let the filer provide supporting material: a screenshot, a log file, a link to an external document. Attachments with a `data_ref` are injected into the bundle's artifact namespace at planning time so the bundler and pre-execution review tracks can reference them. Attachments with only a `url` are fetched by the bundler and cached; the fetched content becomes a bundle-scoped artifact.

**`metadata`** records provenance. `filed_by` is an identity string; in v1.1 it is always the single human reviewer, but the field is typed as a string rather than an enum to avoid coupling to the identity model. `filed_via` records the surface for calibration (do CLI-filed ideas get better bundler proposals than MCP-filed ones?).

### Examples

A simple request filed via CLI:

```yaml
bundle_input:
  idea:
    source: cli
    body: "Add a health check endpoint to the api service"
  structured_params:
    target_hint: existing-repo:api
  metadata:
    filed_by: "dan"
    filed_at: "2026-05-08T14:30:00Z"
    filed_via: cli
```

An agent-generated follow-up:

```yaml
bundle_input:
  idea:
    source: agent_generated
    title: "Add secret-scanning to pre-commit hooks"
    body: "The security review track on bundle B0123 flagged that secret scanning..."
  parent_bundle_id: "01JQXYZ..."
  metadata:
    filed_by: "security-review-agent"
    filed_at: "2026-05-08T15:00:00Z"
    filed_via: agent_generated
```

### What the bundler does with inputs

On receiving a bundle input, the bundler:

1. Resolves `parent_bundle_id` and `related_bundle_ids` to load prior bundle artifacts from `memory/` for context.
2. Fetches and caches any URL-only attachments.
3. Reads relevant calibration data from `memory/calibration/` and prior decisions from `memory/decisions/`.
4. Drafts requirements, RFC, UX flow (if relevant), implementation plan, and verification plan.
5. Computes complexity and risk scores per the approval matrix factors.
6. Decomposes the work into a task DAG with capability manifests.
7. Determines the `target:` field (see below).
8. Populates the concerns section.

The input schema is the bundler's API contract. Anything not in the input was not provided by the filer; anything the bundler discovers during planning (a missing capability, an ambiguous requirement) becomes a concern in the proposal, not a silent assumption.

---

## Bundle outputs

A bundle's outputs are the structured record of what happened, preserved for the reviewer, for calibration, for post-mortem, and for future bundles that consult memory. They mirror the task-level output spec in the DAG schema but operate at the bundle level.

### Schema

```yaml
bundle_output:
  outcome:
    status: shipped | parked | killed | failed_verification | aborted
    rationale: "<human-readable explanation of the outcome>"

  product_artifacts:
    spawned_repos:
      - name: <repo name>
        url: <github url>
        registry_entry: <key in memory/products/registry.json>
    merged_prs:
      - repo: <repo name>
        pr_number: <int>
        pr_url: <github url>
        merge_commit: <sha>

  artifact_manifest:
    published_global_artifacts:
      - descriptor: {namespace: global, name: <string>, version: <string>, content_type: <string>}
        hash: <blaake3 hex>
    bundle_artifact_index: <artifact descriptor for the index of all bundle-scoped artifacts>

  verification:
    plan_ref: <artifact descriptor pointing to the Verification Plan>
    report_ref: <artifact descriptor pointing to the Verification Report>
    outcome: passed | failed | partial
    failed_criteria: [<criterion>, ...]
    rollback_triggered: true | false
    rollback_bundle_id: <ULID or null>

  calibration:
    axes:
      complexity:
        estimated: <int>
        actual: <float>
      risk:
        estimated: <int>
        actual: <float>
      code_surface:
        estimated_lines: <int>
        actual_lines: <int>
      agent_iterations:
        estimated: <int>
        actual: <int>
      wall_clock:
        estimated_minutes: <int>
        actual_minutes: <int>
      blast_radius:
        predicted: <string>
        realized: <string>
    divergence_threshold_exceeded: [<axis name>, ...]

  cost:
    llm_tokens:
      input_total: <int>
      output_total: <int>
      by_model: {<model>: {input: <int>, output: <int>}}
    worker_count: <int>
    worker_hours_total: <float>
    peak_ram_bytes: <int>
    peak_disk_bytes: <int>

  memory_pointers:
    decision_artifact: <path in memory/decisions/>
    post_mortem_artifact: <path in memory/post-mortems/ or null>
    calibration_append: <path in memory/calibration/>
    security_findings: [<path in memory/security-findings/>, ...]

  steering_events:
    pause_count: <int>
    redirect_count: <int>
    modification_count: <int>
    mid_flight_approvals: [<approval_decision summary>, ...]

  metadata:
    bundle_id: <ULID>
    completed_at: <ISO8601 timestamp>
    total_wall_clock_seconds: <int>
```

### Field rationale

**`outcome`** is the headline. Five terminal statuses, not the six in the bundle state machine (`rejected` is a pre-execution terminal state that exits before producing a full output record; rejected bundles get a lightweight outcome record with only `status: rejected` and `rationale`, stored in the `bundles` table row, not a full `bundle_output` artifact). `shipped` means the work was merged or the repo was created and verification passed. `parked` means the bundle was completed (code written, PRs opened) but deliberately not merged; the work is preserved on its feature branch for later. `killed` means the work was discarded; the killed-bundle archive in `memory/killed/` preserves the full proposal and execution record. `failed_verification` means execution completed but post-execution QA failed and rollback was either not possible or not configured. `aborted` means the reviewer killed the bundle mid-flight.

**`product_artifacts`** is the concrete deliverable record. `spawned_repos` lists every repo created by this bundle (usually zero or one; the cross-target section argues against creating multiple repos in a single bundle). `merged_prs` lists every PR that was merged, in the control-plane or in any product repo.

**`artifact_manifest`** provides discoverability. `published_global_artifacts` is the bundle's contribution to the global namespace; future bundles consult this to find persistent outputs. `bundle_artifact_index` is a single JSON artifact listing every bundle-scoped artifact produced during execution, so a reviewer or follow-up bundle can enumerate what's available without scanning `artifact_refs`.

**`verification`** captures the QA handoff. `plan_ref` and `report_ref` point into `memory/verification-plans/` and `memory/executions/<bundle-id>/verification-report.json` respectively. The `outcome` and `failed_criteria` fields summarize the result for quick inspection. `rollback_triggered` and `rollback_bundle_id` record whether verification failure initiated an automatic or manual rollback.

**`calibration`** is the feedback data the calibration loop consumes. Each axis records the bundler's estimate (from the proposal) and the actual (measured from execution). Axes whose divergence exceeds 50% are listed in `divergence_threshold_exceeded`, which triggers the post-mortem prompt. The calibration data is appended to `memory/calibration/scoring-outcomes.jsonl` by the orchestrator on bundle completion.

**`cost`** is the resource consumption record. v1.1 has no cost ceiling (token spend is flat-rate and compute is owned hardware), so cost is recorded for calibration rather than enforcement. `by_model` breaks down token consumption so the operator can reason about model-specific cost tradeoffs.

**`memory_pointers`** are the durable artifact references. Rather than embedding full decision text or post-mortem analysis in the output, the output carries pointers. The decision artifact is always written (even for auto-approved bundles; a one-line decision is still a decision). The post-mortem artifact exists only when divergence exceeded threshold.

**`steering_events`** records mid-flight interventions. A bundle that was paused three times and redirected twice is a calibration signal: the original plan was poor, or the requirements were unstable, or the reviewer was micromanaging. Either way, it's data.

**`metadata`** closes the lifecycle loop. `bundle_id` matches the id in the `bundles` table. `completed_at` is the timestamp of terminal transition. `total_wall_clock_seconds` is the clock time from `in_progress` to terminal state, including pause time; it's distinct from `calibration.wall_clock.actual_minutes` which measures active worker time.

### Relationship to calibration and post-mortem

The calibration loop (Bundle lifecycle: planning and approval) compares estimated vs. actual on every tracked axis. The `bundle_output.calibration` block is the actuals half of that comparison; the estimates are in the bundle proposal. The orchestrator appends the paired data to `memory/calibration/scoring-outcomes.jsonl` on bundle completion.

The post-mortem prompt (Threat model and trust assumptions) fires when any axis exceeds 50% divergence. The `divergence_threshold_exceeded` list is the trigger signal. The post-mortem prompt receives the full bundle output, the proposal, the verification report, and the worker reports as context. Its output is stored in `memory/post-mortems/<bundle-id>.md` and referenced from `bundle_output.memory_pointers.post_mortem_artifact`.

---

## The `target:` field

The `target:` field declares where the bundle's output should land. Three values are defined: `new-repo`, `existing-repo:<name>`, and `control-plane`. The field is set by the bundler during planning, informed by the optional `target_hint` in the bundle input. It is reviewable and overridable by the human during approval; the human's override takes precedence.

### Three values, when each applies, decision rule

**`new-repo`** applies when the bundle introduces a new product: a distinct deployable, ownable, versioned thing that does not belong inside an existing repo. The decision rule: if the bundle's primary deliverable is a new service, a new frontend, a new tool, or a new self-contained system, it gets `new-repo`. If the bundle's primary deliverable is a modification to an existing product (a new endpoint in the API service, a new page in the web frontend), it gets `existing-repo`. This is deliberately fuzzy at the boundary ("is a new microservice a modification to the platform or a new product?"), and the bundler escalates ambiguous cases to the reviewer as a concern rather than guessing.

**`existing-repo:<name>`** applies when the bundle modifies a product that already has a repo. The `<name>` is the repo slug as recorded in `memory/products/registry.json`. If the name does not exist in the registry, the bundler treats it as a concern (the reviewer asked for a modification to a repo that doesn't exist, or the repo was deleted, or the registry is stale).

**`control-plane`** applies when the bundle's work is entirely internal to the orchestrator's own repo: modifications to `AGENTS.md`, capability manifest, settings, agent prompts, orchestrator code, memory layout, templates, CI workflows, or documentation that lives alongside the orchestrator. Control-plane bundles are architecturally special because they modify the system that reviews, approves, and executes them; the approval matrix interaction section addresses this.

The `target_hint` in the bundle input is a hint, not a constraint. The bundler may override it. Override rationale appears in the proposal's concerns section. A hint of `null` means the filer had no opinion; the bundler decides.

**Who sets the field.** The bundler computes `target:` as part of planning. The human can override it during approval via any surface (`/modify target: control-plane`). If the human sets a target that contradicts the proposal's content (a `control-plane` target on a bundle that proposes to write product code), the bundler revises the proposal to match on the next planning pass.

### Control-plane vs. product content boundary

The two-tier repo architecture (Bundle lifecycle: execution and integration) establishes that control-plane and product repos are distinct, but the boundary between what belongs in each is not drawn. This section draws it.

**Control-plane content** is anything that constitutes the orchestration system itself: bundle proposals and RFCs, decision records, the Review Deck and its artifacts, memory directories, the capability manifest, agent prompt templates and system prompts, orchestrator source code and configuration (`settings.json`, `settings.local.json`), worker base-image Dockerfiles, repo templates (`templates/new-product-repo/`), GitHub Actions workflows and CI configuration for the control-plane repo, the MCP server implementation, and documentation about the orchestration system ("how to operate the orchestrator").

**Product content** is anything that ships as part of a product: application source code, product tests, product CI workflows, product Dockerfiles, product documentation (architecture, API reference, deploy instructions), product configuration, product data models and migrations, and product-specific agent memory (`AGENTS.md` at the product repo root).

**Ambiguous content.** Some content genuinely straddles the boundary. Agent prompt templates that customize behavior for a specific product ("the API service's developer agent should prefer async/await over threads") are product content because they describe product-specific behavior, but they live in the control-plane because that's where agent configuration is managed. The resolution: product-specific agent configuration lives under `memory/products/<product-slug>/agent-overrides.yaml` in the control-plane repo, not in the product repo. This keeps agent configuration centrally manageable while scoping overrides to specific products.

Repo templates (`templates/new-product-repo/`) are control-plane content because they define how products are born, but they instantiate into product repos at creation time. The template is control-plane; the instantiated copy is product. Modifications to the template affect future products, not existing ones, which is the desired behavior.

Cross-cutting concerns that touch both control-plane and product (a bundle that adds a capability and then uses it to build a product feature) are handled by the cross-target bundle policy below.

### New-repo flow end to end

Creating a new product repo is a multi-step flow gated by mandatory review. The sequence:

1. **Approval.** The bundle is approved with `target: new-repo`. Creating a new repo is a mandatory-review trigger (added to the mandatory-review triggers list in Bundle lifecycle: planning and approval), so this bundle always goes to full human review regardless of complexity and risk scores.

2. **Repo name resolution.** The bundler proposes a repo name (slug derived from the bundle title, configurable naming convention from `settings.json`). The reviewer can override during approval. The orchestrator checks GitHub for name collisions before creation; a collision escalates to the reviewer with suggested alternatives.

3. **Scaffolding.** The first worker task in the DAG checks out `templates/new-product-repo/` from the control-plane repo and instantiates it into a new directory. Template variables (product name, description, initial version, originating bundle id) are substituted. The scaffold includes: `README.md`, `docs/` (architecture overview, API reference placeholder, data model placeholder, key decisions log), `INSTALL.md`, `DEPLOY.md`, `AGENTS.md` (pre-populated with the product description and a pointer to the originating bundle), `LICENSE`, `.github/` (issue templates, PR template, CODEOWNERS with the reviewer as default owner, branch protection config), `CHANGELOG.md` with the initial entry auto-generated from the bundle's RFC, a working CI pipeline (lint, test, build), and a reproducible deploy mechanism (docker-compose for v1.1 local deployment).

4. **Repo creation.** The orchestrator calls the GitHub API (using the GitHub App installation token) to create the repo under the configured org, with the configured default visibility (private by default). The scaffold is pushed as the initial commit on `main`.

5. **Branch protection.** The orchestrator configures branch protection on `main`: require pull request reviews (1 approval, dismiss stale reviews), require status checks (the CI pipeline), require conversation resolution, and prohibit force pushes and deletions. The reviewer is the required reviewer; the GitHub App bot is excluded via the App's identity so that bot-authored PRs that are auto-merged (for auto-approved bundles modifying this product later) are not blocked. (Configurable; whether bot PRs should be excluded from branch protection or require a second factor is an operational decision for the reviewer.)

6. **Registry update.** The orchestrator appends an entry to `memory/products/registry.json`:

```json
{
  "product_slug": "api-service",
  "repo_name": "studio/api-service",
  "repo_url": "https://github.com/studio/api-service",
  "originating_bundle_id": "01JQXYZ...",
  "created_at": "2026-05-08T15:00:00Z",
  "status": "active"
}
```

7. **Product development.** Subsequent worker tasks in the same bundle develop the product code in the new repo, using the same per-worker-branch and DAG-order-merge mechanics specified in Bundle lifecycle: execution and integration.

8. **Verification.** The QA agent verifies the product in the new repo post-execution, per the standard verification handoff.

If the bundle is aborted after repo creation but before completion, the repo is left in place (it has the scaffold and whatever partial work was committed to feature branches). The repo's status in the registry is set to `abandoned`. A follow-up bundle can target the abandoned repo with `target: existing-repo:<name>` to continue the work.

### Existing-repo and control-plane flows

**`existing-repo:<name>`** follows the standard execution flow from Bundle lifecycle: execution and integration: workers operate on per-worker branches off a bundle base branch in the target repo, integration proceeds in DAG order, and the final bundle branch is merged to the target repo's main branch on successful verification. The orchestrator validates that the named repo exists in `memory/products/registry.json` before starting execution. If the repo exists but has status `abandoned`, the orchestrator surfaces a warning and proceeds; the reviewer may want to verify the repo's current state before work begins.

**`control-plane`** follows the same execution flow but with elevated caution. Control-plane bundles are always mandatory-review (already listed in the mandatory-review triggers). Additionally, the orchestrator takes a snapshot of the control-plane repo's state (a `control-plane-snapshot` artifact published to the global namespace) before the first worker task begins, so that a rollback has a clean baseline independent of git history. Control-plane bundles cannot auto-ship, regardless of score.

### Cross-target bundles (allow or reject)

A bundle that modifies both the control-plane and a product repo, or that modifies two product repos, is currently not supported. The schema has no mechanism for multi-target bundles: `target:` is a single value, not a list.

The argument for allowing cross-target bundles: a capability addition to the control-plane plus a product feature that uses it is naturally a single bundle; the reviewer thinks about them together. Splitting them into two bundles forces the reviewer to track a dependency ("approve the capability bundle, wait for it to ship, then approve the feature bundle") that the system has no primitive for.

The argument against: cross-target bundles complicate every lifecycle operation. Approval: which repo's mandatory-review triggers apply? Execution: workers operate in different repos; the integration step that merges across repos doesn't exist. Rollback: does rolling back the product change also roll back the capability addition? If so, what about other bundles that have started using the capability? If not, the bundle's safety story is asymmetric.

**Resolution: reject cross-target bundles for v1.1.** If a bundle's work genuinely spans control-plane and product, the bundler splits it into two bundles and notes the dependency in both proposals' related_bundle_ids. The reviewer sees the pair and can approve both in sequence. The system does not enforce the dependency (bundle independence is assumed), but the related_bundle_ids link is visible in the approval surface and the reviewer can sequence manually.

This is a real limitation. The most common case (capability addition paired with first use) is two bundles that should be approved together but can execute sequentially without coupling. The less common case (simultaneous changes to two product repos as part of a single feature) probably indicates that the product boundary was drawn wrong and one of the repos should be a subdirectory of the other, which the single-product-per-repo architecture already argues for.

An escape hatch: a `control-plane` bundle may modify `memory/products/<product-slug>/agent-overrides.yaml`, which is technically product-scoped content stored in the control-plane. This is the sanctioned way to make product-specific agent configuration changes without a cross-target bundle. The escape hatch is narrow by design.

### Approval matrix interaction

The `target:` field interacts with the approval matrix in two ways.

First, **repo creation is a mandatory-review trigger.** Added to the mandatory-review triggers list: `target: new-repo` forces full human review regardless of complexity and risk scores. The rationale: creating a repo is an irreversible namespace action (deleting a repo is not the same as reverting a commit; the URL is burned, the clone history is fragmented), and the reviewer should explicitly consent to the repo's existence, naming, and visibility.

Second, **control-plane modification is a mandatory-review trigger** (already listed). This section does not add new triggers but reinforces the existing one: control-plane bundles can never auto-ship.

Third, **existing-repo targets inherit the product repo's risk profile.** A bundle targeting `existing-repo:api` that touches the auth subdirectory gets the risk scoring appropriate to auth-touching work, as defined by the security-sensitive path patterns in `settings.json`. The target repo's sensitivity is encoded in those path patterns, not in a separate per-repo risk score.

---

## Bundle state machine

The architecture section sketches the state transitions. This section specifies the full state machine: every legal transition with its trigger, every illegal transition with the error model, what is persisted at each transition, and how external surfaces observe and trigger transitions. The mid-flight steering states (paused, redirecting) are included explicitly.

### States

A bundle exists in exactly one of the following states:

| State | Description |
|-------|-------------|
| `proposed` | Bundler has produced a proposal; awaiting pre-execution review and approval |
| `in_review` | Pre-execution review tracks are running; not yet at the approval matrix |
| `approved` | Bundle has passed review and been approved; awaiting execution start |
| `in_progress` | DAG executor is driving worker tasks |
| `paused` | Execution is halted mid-flight; state is preserved, workers are idle |
| `redirecting` | Paused bundle is being re-planned; new DAG being produced |
| `verifying` | All worker tasks complete; QA agent is running post-execution verification |
| `complete` | Terminal: bundle shipped successfully (all PRs merged, repos created, verification passed) |
| `parked` | Terminal: work completed but deliberately not merged; preserved for later |
| `failed` | Terminal: execution or verification failed; partial state preserved for forensics |
| `rejected` | Terminal: bundle was rejected during review; no execution occurred |
| `aborted` | Terminal: reviewer killed the bundle mid-flight; partial state preserved |

`parked` is a new state not in the architecture section's sketch. It is distinct from `complete` (the work wasn't shipped) and from `failed` (the work didn't fail; the reviewer chose not to merge it). It exists because the mid-flight steering vocabulary implies that a bundle can reach a point where the code is done but the decision is "not now." Parking preserves the work on its feature branch and closes the bundle cleanly.

`redirecting` is a transient state. A bundle is never observed in `redirecting` for more than the duration of a single planning pass (seconds to minutes). It exists so that the state machine can distinguish "paused and stable" from "paused and being re-planned," which matters because a second Pause or Abort during re-planning has different semantics than during stable pause.

### Transition table

Every legal transition, its trigger, the actor that initiates it, and what is persisted.

| From | To | Trigger | Actor | Persisted |
|------|----|---------|-------|-----------|
| (start) | `proposed` | Bundle input received | Filer (human or agent) | `bundles` row, `bundle_input` artifact |
| `proposed` | `in_review` | Bundler completes proposal | Bundler agent | Proposal artifact, DAG, manifest, concerns, pre-execution review track dispatch |
| `in_review` | `proposed` | Review track finds blocking issue, returns for revision | Review track agent | Review findings, modified proposal |
| `in_review` | `approved` | Approval matrix returns approve, or human approves | Orchestrator (auto) or Reviewer (manual) | `approval_decisions` row, `bundles.approved_at` |
| `in_review` | `rejected` | Approval matrix returns reject, or human rejects | Orchestrator (auto-reject not in v1.1) or Reviewer | `approval_decisions` row, lightweight outcome |
| `approved` | `in_progress` | Orchestrator starts execution | Orchestrator | Feature branch created, entry workers spawned |
| `approved` | `rejected` | Reviewer rejects after approval but before execution start | Reviewer | `approval_decisions` row (superseding prior approve) |
| `in_progress` | `paused` | Reviewer issues Pause | Reviewer | Pause event in audit log, in-flight worker allowed to finish current step |
| `in_progress` | `verifying` | All exit nodes reach terminal state | Orchestrator | Worker reports, merged bundle branch, verification dispatch |
| `in_progress` | `aborted` | Reviewer issues Abort | Reviewer | Worker cancellation, partial state preserved |
| `paused` | `in_progress` | Reviewer issues Resume | Reviewer | Resume event in audit log, scheduler re-ticked |
| `paused` | `redirecting` | Reviewer issues Redirect with new instructions | Reviewer | Redirect event in audit log, re-planning dispatch |
| `paused` | `aborted` | Reviewer issues Abort | Reviewer | Worker cancellation, partial state preserved |
| `redirecting` | `in_review` | Bundler completes re-plan | Bundler agent | New proposal, new DAG, baseline references to completed work |
| `redirecting` | `paused` | Reviewer issues Pause during re-planning | Reviewer | Re-planning halted; partial re-plan discarded |
| `redirecting` | `aborted` | Reviewer issues Abort during re-planning | Reviewer | Re-planning halted; workers killed; partial state preserved |
| `verifying` | `complete` | Verification passes, reviewer approves ship (or auto-ship criteria met) | QA agent + Orchestrator/Reviewer | Verification report, merge to target, `bundles.completed_at` |
| `verifying` | `parked` | Reviewer chooses to park rather than ship | Reviewer | Verification report, feature branch preserved, `bundles.completed_at` |
| `verifying` | `failed` | Verification fails and rollback is not configured or not possible | QA agent | Verification report, failure record |
| `verifying` | `in_progress` | Verification fails, rollback bundle spawned (rollback is a new bundle; this bundle enters `failed` after rollback completes) | QA agent + Orchestrator | Verification report, rollback bundle ref |
| `complete` | `in_progress` | Rollback triggered post-merge (rollback is a new bundle; this bundle stays `complete`) | Reviewer | Rollback bundle ref in `steering_events` |

### Illegal transitions and error model

Any transition not in the table is illegal. The orchestrator rejects illegal transitions with an error that includes the current state, the requested transition, and the reason it's not allowed. The error is returned to the requesting surface (MCP, GitHub Issues comment, CLI).

Examples of illegal transitions and their error messages:

- `approved → proposed` (cannot downgrade an approved bundle to proposed): "Bundle is approved; use /modify to revise before execution, or Pause during execution."
- `complete → failed` (terminal states are terminal): "Bundle is complete; transitions from terminal states are not allowed."
- `proposed → in_progress` (must pass through review): "Bundle has not been reviewed. Wait for pre-execution review and approval."
- `paused → verifying` (cannot skip resume): "Bundle is paused. Resume or Redirect first."
- `in_progress → approved` (cannot go backward in execution): "Bundle is executing. Pause first, then Redirect to re-plan."

### Surface observability

Every state transition is observable through all three surfaces.

**MCP.** The `studio://bundles/{id}` resource reflects the current state on every fetch. The `studio://bundles/pending` resource filters by state. State transitions do not push to MCP (MCP is pull-based for resources), but the Claude Desktop integration can poll on an interval.

**GitHub Issues.** The bundle's issue in the control-plane repo is updated on every state transition. The issue body reflects the current state and a timeline of transitions. Labels are updated to match: `state/proposed`, `state/in-review`, `state/approved`, `state/in-progress`, `state/paused`, `state/redirecting`, `state/verifying`, `state/complete`, `state/parked`, `state/failed`, `state/rejected`, `state/aborted`.

**CLI.** `studio show <bundle-id>` displays current state and transition history. `studio list --state <state>` filters by state.

**Transitions are recorded in `audit_log`** (event_type: bundle_state_transition, subject_type: bundle, subject_id: <bundle_id>, payload: {from_state, to_state, trigger, actor}). Both the state change and the triggering event are in the same SQLite transaction, so the audit log entry is committed atomically with the state change.

### Triggering transitions from surfaces

All three surfaces can trigger the reviewer-initiated transitions (approve, reject, pause, resume, redirect, abort, park). The orchestrator validates the transition against the current state and the requesting surface's authority. For v1.1, all surfaces have equal authority (the single-reviewer model means there is no surface-based authorization tier).

**MCP** triggers via tool calls: `approve_bundle`, `reject_bundle`, `pause_bundle`, `resume_bundle`, `request_modification` (for modify-and-re-plan during `in_review`), and the steering verbs mapped to the appropriate tools.

**GitHub Issues** triggers via comments: `/approve`, `/reject [reason]`, `/pause`, `/resume`, `/redirect [instructions]`, `/abort [reason]`, `/park [reason]`.

**CLI** triggers via commands: `studio approve <id>`, `studio reject <id>`, `studio pause <id>`, `studio resume <id>`, `studio redirect <id> "<instructions>"`, `studio abort <id>`, `studio park <id>`.

---

## Mid-flight steering mechanics

The steering vocabulary (Pause, Redirect, Abort, Rollback) was folded into the spec as Claude-recommended after the reviewer said "I honestly do not know the answer." This section treats them as accepted and specifies the executor's role in each. Rollback is addressed separately because it operates post-execution, not mid-flight.

### Pause

**Trigger.** Reviewer issues Pause via any surface.

**Executor behavior.** The orchestrator sends `worker.pause()` RPCs to all workers in state `running` or `ready` for this bundle. Each worker finishes its current step (the in-progress tool call, the in-progress test run, the in-progress file write) and then halts. The worker does not checkpoint mid-step; it completes the step it's on and then stops. This is a pragmatic compromise: true mid-step checkpointing would require workers to understand checkpoint semantics (serialize state, reconstitute on resume), which is far more coupling than the kill-all-on-crash policy accepts (per the DAG executor's rejection of mid-node checkpointing). The cost is that a worker in the middle of a 20-minute test suite will run that suite to completion before pausing; the reviewer sees "pausing..." in the surface until the worker acknowledges.

Workers in state `pending` are left `pending`. The scheduler is halted (no new nodes are dispatched from the ready set). The event pump continues to process events (worker completions, RPC replies) so that in-flight steps reach completion, but new dispatches are suppressed.

**What is persisted.** The pause event is written to `audit_log`. Each worker's state in `dag_nodes` is updated: `running → paused`. The `paused_at` timestamp is set on each paused node and on the bundle. Worker worktrees are left intact. The bundle's feature branch is not touched.

**What is logged.** `audit_log`: `{event_type: "bundle_paused", subject_type: "bundle", subject_id: <bundle_id>, payload: {paused_by: <actor>, paused_at: <timestamp>, surface: <mcp|github_issue|cli>}}`. For each worker, `node_state_history`: `{node_id: <id>, from_state: "running", to_state: "paused", at: <timestamp>, reason: "bundle_paused"}`.

**Resume semantics.** The reviewer issues Resume via any surface, optionally with notes. Notes are injected into the orchestrator's context for this bundle: they are prepended to the bundle's execution context as a `reviewer_notes` field, visible to workers on their next task spec refresh. The orchestrator re-ticks the scheduler: the ready set is recomputed from current node states, paused workers are transitioned `paused → running` and pick up where they left off (their worktrees are intact, their task specs are unchanged), and new nodes that became eligible during pause are dispatched normally.

If the reviewer provides no notes, resume is a simple unpause. If notes are provided ("I looked at the draft PR and the auth module is over-engineered; redirecting to simplify"), the notes appear in worker context but do not automatically change the DAG. If the reviewer wants to change the DAG, they use Redirect, not Pause-with-notes.

### Redirect

**Trigger.** Reviewer issues Redirect via any surface with new instructions. Redirect implies Pause (the bundle is paused first if not already paused), then re-plan.

**Re-planning flow.** The design ratifies the natural answer flagged in the deferred items: Redirect discards the current DAG and runs the planner on the current worktree state as a fresh bundle, with completed work as the baseline. The sequence:

1. **Pause.** If the bundle is not already `paused`, Pause is applied first (in-flight steps finish, scheduler halts). If the bundle is already `paused`, proceed directly.

2. **Snapshot current state.** The orchestrator produces a `bundle-state-snapshot` artifact containing:
   - The current worktree state (which commits are on which branches, which files are modified, the diff against the base branch).
   - The completed nodes from the prior DAG: their ids, their terminal states (`completed`, `failed`, `skipped`, `cancelled`), and references to their output artifacts.
   - The in-progress and pending nodes that will be discarded.
   - The prior proposal and DAG, for the audit trail.

3. **Transition to `redirecting`.** The bundle enters `redirecting` state. This is transient; the reviewer sees "redirecting..." in the surface.

4. **Dispatch re-planning.** The orchestrator spawns a new planning task (not a full bundle lifecycle; a narrow planning action within the existing bundle's identity). The planner agent receives:
   - The original bundle input (for context).
   - The reviewer's redirect instructions.
   - The `bundle-state-snapshot` artifact.
   - Calibration data and relevant memory (same as a new bundle).

5. **Produce new DAG.** The planner produces a new task DAG. Completed nodes from the prior DAG are referenced as baseline artifacts: if worker-3 completed successfully and its output is still valid under the new instructions, the new DAG carries a reference to worker-3's artifacts and does not re-execute the work. If the new instructions invalidate prior work, the new DAG includes replacement workers that write to the same or different artifact descriptors. The planner decides which completed work is reusable; the reviewer can override during re-approval.

6. **Re-enter review.** The new DAG goes through pre-execution review tracks (abbreviated: only the delta from the prior review is examined, not the full bundle) and the approval matrix. The bundle transitions `redirecting → in_review`.

7. **Resume execution.** On approval, the bundle transitions `in_review → approved → in_progress` (or directly `in_review → in_progress` if the approval-matrix tier doesn't require a separate approved step). The executor ingests the new DAG, the scheduler computes the ready set from the new DAG's entry nodes (completed work is not re-executed; new nodes that depend on completed work are dispatched normally), and execution proceeds.

**How completed work is referenced.** Both artifact descriptors and branch state. A completed worker's output artifacts are the primary reference: if worker-3 published `test-results` and the new DAG needs test results, it references the same artifact descriptor. The branch state (which commits are on which branches) is preserved so that the final integration merge has the complete history.

**How the new DAG's manifest relates to the old one.** The new DAG's capability manifests must be subsets of the bundle's original capability manifest (the bundle manifest was approved and has not changed). If the redirect instructions imply a scope increase beyond the original manifest, the planner must request additional capabilities, which go through the normal capability-request approval flow (escalating to the reviewer). Redirect does not silently expand the bundle's capability envelope.

**Audit trail.** The through-line is preserved via `parent_bundle_id` semantics in the audit log: the original proposal, the redirect event, the state snapshot, the new proposal, and the new DAG are all linked to the same bundle id. The `steering_events.redirect_count` in the bundle output records the full sequence. A bundle that was redirected three times has three state snapshots and three DAGs in the audit trail.

### Abort

**Trigger.** Reviewer issues Abort via any surface, from any non-terminal state. The bundle must be in `approved`, `in_progress`, `paused`, or `redirecting`. Abort from `proposed` or `in_review` is treated as Reject, not Abort.

**Worker cancellation propagation.** The orchestrator sends `worker.cancel(reason="bundle_aborted")` to all workers in state `running` or `paused` for this bundle. The cancellation follows the protocol from DAG executor Aggregator mechanics: 30-second grace period, then SIGTERM, then SIGKILL after another 10 seconds. Workers that complete cleanly during the grace period transition to `cancelled`, not `failed` (their work is preserved on their branch, but the bundle is dead).

Workers in state `pending` or `ready` are transitioned directly to `cancelled`.

**Draft PR closure.** If the bundle opened draft PRs (against the target repo's main branch), the orchestrator closes them with a comment: "Bundle aborted by reviewer. Feature branch `<branch>` is preserved for recovery." The PR is closed, not deleted; the branch remains.

**Worktree cleanup.** Worker worktrees are not deleted on Abort. They occupy disk space but preserve partial work for recovery. The periodic background sweep (Artifact Protocol, Lifecycle and garbage collection) collects worktrees for aborted bundles after a configurable retention period (default 30 days, same as failed bundles). The reviewer can force immediate cleanup via `studio cleanup <bundle-id>`.

**Terminal state.** The bundle transitions to `aborted`. The `bundles.completed_at` timestamp is set. A lightweight outcome is written: `{status: aborted, rationale: <reason from reviewer or "aborted by <actor>">}`.

**What's preserved.** Commits on feature branches are preserved (recoverable via git). Artifact refs in `artifact_refs` remain (the bundle's artifacts are not garbage collected until the aborted-bundle retention window expires). Capability grants that were made specifically for this bundle are revoked on Abort (the capability was for the bundle's work, which is now dead). The audit log preserves the full execution trace up to the abort point.

**What's not preserved.** In-flight RPC calls are abandoned. Workers that are killed via SIGKILL lose any uncommitted working state. The bundle's DAG and node states are frozen at their terminal states (`cancelled` or the pre-abort state for already-completed nodes).

### Rollback

Rollback is distinct from Abort because it operates post-merge or post-deploy, not mid-flight. A bundle that has reached `complete` (merged and verified) may later need to be rolled back.

**Is rollback a new bundle?** Yes. Rollback is a new bundle, not a special bundle kind, not a direct action. The rationale:

- Rollback touches the same repos, the same code paths, and the same deployment mechanisms as the original work. It needs capability grants, a task DAG, worker execution, and verification, same as any other change.
- Making rollback a new bundle means it gets the same review surface, the same audit trail, and the same calibration data as any other bundle. A rollback that fails is itself a failed bundle, with its own post-mortem.
- The alternative (a non-bundle direct action: the orchestrator reverts the merge commit and re-deploys) is simpler for the common case but fails for any nontrivial rollback (the revert commit has conflicts, the deploy has side effects, data was migrated and needs to be migrated back). A rollback-is-a-bundle model handles trivial and nontrivial rollbacks uniformly.

The rollback bundle's input carries `parent_bundle_id` pointing to the bundle being rolled back, and `related_bundle_ids` with the original bundle. The bundler reads the original bundle's Verification Plan (which includes the rollback plan) and the current repo state to produce the rollback DAG.

**What triggers rollback.** Two paths. First, **manual**: the reviewer issues Rollback from any surface (`studio rollback <bundle-id>`, or the MCP equivalent). This creates a rollback bundle input and enters the normal bundle lifecycle. Second, **automatic**: post-execution verification fails, the failure meets the auto-rollback criteria (stakes are Low, rollback is machine-executable per the Verification Plan, and the bundle's Verification Plan declared an auto-rollback eligibility), and the orchestrator spawns the rollback bundle automatically.

**Relationship to the Verification Plan's rollback plan.** The Verification Plan (produced by the QA agent during pre-execution review) includes a rollback plan: whether rollback is machine-executable, what steps it entails, and whether it's eligible for automatic triggering. The rollback bundle's bundler reads this plan as a starting point but is not bound by it; if the plan says "revert commit X" but commit X now has conflicts, the bundler proposes an alternative. The Verification Plan is a plan, not a contract.

**How rollback is verified.** The rollback bundle has its own Verification Plan, produced by the QA agent during the rollback bundle's pre-execution review. The plan verifies that the rolled-back state matches the pre-merge state (for the rolled-back change) while preserving unrelated changes that landed after the original bundle. This is a more demanding verification than "does the revert commit apply cleanly," because the rolled-back product must still work.

**Verification-driven rollback.** When the original bundle's post-execution verification fails and rollback is triggered (manually or automatically), the original bundle's state after rollback depends on the rollback outcome. If the rollback bundle completes successfully, the original bundle stays in `complete` (it was shipped, then un-shipped; the net is zero but the record is accurate) and the rollback event is appended to the original bundle's `steering_events`. If the rollback bundle fails, the original bundle is annotated with a `rollback_failed` flag and the reviewer is paged via the notification surface; this is the worst case (bad code shipped, and the undo failed too).

---

## Pre-execution review track integration with approval matrix

The v1.1 spec inferred that pre-execution review tracks run before the approval matrix and feed it. This section ratifies that ordering and specifies the data flow.

### Ordering

The sequence is:

```
Bundle proposed → Pre-execution review tracks run → Review findings stored → Approval matrix evaluates → Decision
```

This ordering is load-bearing. The approval matrix's decision logic depends on review track outputs: a bundle with a critical security finding cannot auto-ship regardless of its risk and complexity scores. If review tracks ran after the matrix, the matrix would make decisions on incomplete information, and the auto-ship gate would have to be re-evaluated after review tracks complete, which is effectively the same ordering with extra steps.

### Data flow

**What each track produces.** The General Adversarial Critique produces findings tagged `severity:{low|med|high}` and `status:{resolved|accepted-risk|unresolved}`. The Security Review produces findings in the same format plus a structured threat model section (included in the bundle body when the bundle touches auth, data handling, external surfaces, secrets, billing, or PII; omitted otherwise). The QA Verification Planning track produces a Verification Plan artifact and a rollback plan assessment.

**Where findings are stored.** Each track's findings are stored as bundle-scoped artifacts (descriptors: `bundle:adversarial-findings`, `bundle:security-findings`, `bundle:verification-plan`). They are also inlined into the bundle's proposal for the reviewer's direct inspection.

**How the approval matrix consumes them.** The approval matrix evaluator (a deterministic function in the orchestrator, not an LLM call) reads the bundle's complexity and risk scores, the review track findings, and the mandatory-review trigger list. It produces a tier decision and an auto-ship eligibility boolean.

### Matrix decision logic (pseudocode)

```python
def evaluate_approval_matrix(bundle, findings, triggers):
    # Mandatory-review triggers override the matrix entirely
    if any(trigger.matches(bundle) for trigger in triggers):
        return Tier.FULL_REVIEW, auto_ship=False, reason="mandatory review trigger"

    # Security findings gate auto-ship
    security = findings.get("security", [])
    has_critical = any(f.severity == "critical" for f in security)
    has_unresolved_medium_plus = any(
        f.severity in ("medium", "high", "critical") and f.status == "unresolved"
        for f in security
    )

    if has_critical:
        return Tier.FULL_REVIEW_COOLDOWN, auto_ship=False, reason="critical security finding"
    if has_unresolved_medium_plus:
        # Force full review; override whatever the scores say
        return Tier.FULL_REVIEW, auto_ship=False, reason=f"unresolved security findings"

    # Rollback plan gates auto-ship
    verification_plan = findings.get("verification_plan")
    has_viable_rollback = verification_plan and verification_plan.rollback_feasible
    if not has_viable_rollback:
        # Bump reversibility to 3 in stakes scoring
        bundle.risk_scores.reversibility = max(bundle.risk_scores.reversibility, 3)

    # Auth / billing / secrets / PII gate auto-ship
    touches_sensitive = any(tag in bundle.tags for tag in ("auth", "billing", "secrets", "pii"))
    if touches_sensitive:
        # Force full review regardless of scores
        return Tier.FULL_REVIEW, auto_ship=False, reason="touches sensitive surface"

    # Score-driven tier from the 3x3 matrix
    tier = matrix_lookup(bundle.complexity_score, bundle.risk_score)

    # Auto-ship is disabled for anything above auto or auto-notify
    if tier in (Tier.AUTO_APPROVE, Tier.AUTO_APPROVE_NOTIFY):
        can_auto_ship = (
            not has_unresolved_medium_plus
            and has_viable_rollback
            and not touches_sensitive
        )
    else:
        can_auto_ship = False

    return tier, auto_ship=can_auto_ship, reason="score-driven"
```

The matrix evaluator is deterministic and auditable. The scores (complexity and risk) are produced by the bundler and reviewed by the critique tracks; the evaluator does not re-score. The evaluator's only judgment is whether review findings or mandatory triggers override the score-driven tier. This keeps the machine's role narrow: it can escalate but never de-escalate.

### Storage of the matrix decision

The matrix decision (tier, auto_ship boolean, reason) is stored in the `bundles` table (`tier` column) and in `approval_decisions` (with `decision = tier` and `actor = "system"` for auto-decisions). The reviewer sees the decision on all surfaces and can override upward (escalate to a higher tier) but not downward. Downward overrides (reviewer decides an auto-approve bundle should actually be full review) are upward overrides in practice.

---

## Modification request handling and re-scoring

### When modification is available

Modification (`/modify [instructions]`) is available when the bundle is in `proposed`, `in_review`, or `approved` (pre-execution). During execution, Redirect is the equivalent verb (see mid-flight steering). The distinction is that Modify revises a proposal that hasn't started executing; Redirect revises a plan that is already in flight.

### Modification flow

1. Reviewer issues `/modify [instructions]` via any surface.
2. The bundle transitions `in_review → proposed` (or stays in `proposed` if already there).
3. The bundler revises the proposal based on the instructions: re-drafts requirements, RFC, implementation plan, and verification plan as needed; may re-decompose the task DAG.
4. The revised proposal re-enters pre-execution review. If the modification was narrow ("add a rate-limit to the new endpoint"), review tracks examine only the delta. If the modification was broad ("use Postgres instead of SQLite"), full re-review.
5. The revised proposal enters the approval matrix with potentially new scores.

### Re-scoring

**Yes, the bundler re-scores on modification.** The open question in the spec asks whether modification forces a re-score. The answer is yes. The argument against (bouncing between tiers confuses the surface) is real but addresses the wrong problem. If a modification meaningfully changes the bundle's risk profile, the approval tier should reflect the new risk profile, not the old one. A bundle that was modified from "add a health check endpoint" (complexity 1, risk 0) to "add a health check endpoint with authentication and rate limiting" (complexity 3, risk 2) should move from auto-approve to approve-with-summary. The reviewer should see that change explicitly.

The "bouncing between tiers" concern is a UI problem, not a scoring problem. The surface should show: "Tier: auto-approve (was: approve-with-summary)" or "Tier change: summary → auto-approve (risk dropped from 4 to 2 after removing the external integration)." The transition history makes the bounce visible and interpretable.

A corollary: the reviewer can see the score delta on each modification. If a bundle has been modified three times and each time the complexity score went up, that's a signal that the reviewer's instructions are expanding scope, and the surface should make it visible ("Complexity: 2 → 3 → 3 → 5 across three modifications").

### Modification count

The `bundle_output.steering_events.modification_count` records modifications. A bundle with more than three pre-execution modifications is surfaced in calibration as a signal of unstable requirements or poor initial bundling, regardless of execution outcome.

---

## Default actions, cooldown durations, and multi-surface race resolution

### Default action for summary-tier timeouts

The current spec states: "Default if reviewer doesn't respond in the configured window depends on the cell: low-risk cells default-approve; moderate-risk cells default-hold (require explicit response)." The open question is whether to default-hold across the board until trust is built.

**Resolution: default-approve for low-risk cells, default-hold for moderate-risk.** The conservative "default-hold everywhere until trust is built" is appealing but has a real cost: it eliminates the time-saving benefit of the summary tier. If every summary-tier bundle requires explicit action, the summary tier is just a shorter full-review tier, and the reviewer's attention budget doesn't benefit from the tier system at all.

The safer calibration: start with the spec's stated defaults (approve for low-risk, hold for moderate) and track default-approve outcomes as a calibration axis. If default-approved bundles have a higher failure rate than explicitly-approved bundles of the same stakes, the threshold for default-approve is wrong, and the remedy is adjusting the matrix, not default-holding everything. The calibration data answers the question better than a design-time intuition.

**Default-approve window: 4 hours** for low-risk summary-tier cells. This is the Review Deck v1 numerics interacting with the approval matrix: 4 hours is enough that the reviewer has had a chance to see the notification (email, mobile push via GitHub assignment) and intervene if they disagree, but not so long that it meaningfully delays low-risk work. Configurable in `settings.json` under `approval.default_approve_window_hours`.

### Cooldown duration for the highest-risk tier

The current spec says 1 hour. The open question asks whether 24 hours is more appropriate for irreversible changes.

**Resolution: 1 hour, with a 24-hour option for bundles marked irreversible.** The 1-hour cooldown serves the stated purpose (forcing function: look at it, walk away, come back) without imposing a calendar-day delay on high-stakes work that the reviewer has already thought about carefully. A 24-hour cooldown would mean a high-stakes bundle approved at 9 AM Monday can't start until 9 AM Tuesday, which is a real velocity cost for a solo founder.

The nuance: bundles that touch surfaces flagged as irreversible (production deploys, which don't exist in v1.1; data migrations that can't be rolled back; credential rotation; domain name changes) benefit from a longer cooldown. The `irreversible` flag is a new field on the bundle proposal, set by the bundler when the Verification Plan's rollback plan concludes "rollback is not machine-executable and manual recovery would require >1 hour of operator time." Bundles with the `irreversible` flag get a 24-hour cooldown rather than 1 hour, configurable in `settings.json` under `approval.irreversible_cooldown_hours`.

In v1.1, with no production and most changes being reversible (git revert, re-deploy to staging), the `irreversible` flag will be rare. The flag exists primarily as a design slot for when production becomes real.

**Override.** The reviewer can override the cooldown for genuine emergencies via any surface (`/force-approve` or MCP equivalent). An override writes an audit log entry with the reason. Overrides are tracked in calibration: a reviewer who overrides frequently is either operating in a high-urgency mode (which the system should learn from) or habitually bypassing the safety mechanism (which calibration should flag).

### Multi-surface action ordering and race resolution

The open question: what happens if approval-via-MCP races reject-via-Issues within the same minute?

**Resolution rule: first-write wins, second-write fails with a conflict error.** The mechanism is SQLite's serialized transaction model. When an approval decision arrives from any surface, the orchestrator opens a SQLite transaction, reads the current bundle state, validates the transition, writes the decision, and commits. Two surfaces racing within the same second are serialized by SQLite's write lock: one commits first, the other sees the updated state and rejects the transition.

Concrete example: the reviewer clicks Approve in MCP and simultaneously their phone (GitHub Issues notification) posts `/reject`. The MCP handler opens a transaction, sees `state = in_review`, validates `in_review → approved` as legal, writes the decision, commits. The GitHub Issues webhook arrives next (milliseconds later, but after the MCP transaction committed), opens a transaction, sees `state = approved`, attempts `approved → rejected`. That transition is legal in the state machine (approved bundles can be rejected before execution start), so the rejection supersedes the approval. The bundle lands in `rejected`. The audit trail shows approval at T, rejection at T+0.3s, and the surface shows "Rejected (superseded prior approval)."

If instead the rejection committed first (`in_review → rejected`), the approval attempt sees `state = rejected` and returns an error: "Bundle is rejected; cannot approve." The surface shows the rejection and the failed approval attempt.

The semantics exposed to the reviewer: **the last decision chronologically is authoritative, provided it's a legal transition from the state at the moment it executes.** This is natural: the reviewer changed their mind. The audit trail preserves the full sequence. The system does not attempt to merge or reconcile contradictory decisions; the most recent one wins, same as any state machine with a single mutator.

This is not a "race condition" in the traditional sense. SQLite serializes writes. The designer's question is what semantics to expose when the serial order produces a surprising outcome. The answer is: expose the serial order faithfully. The reviewer sees both decisions in the timeline and can issue a third if the outcome wasn't what they intended.

**What the surface shows on conflict.** If a decision arrives and finds the bundle in a state where that decision is no longer legal, the surface receives an error response: "Bundle <id> is in state <current>, cannot <action>. (It was <previous> when you loaded it; a decision from <other_surface> at <timestamp> changed it.)" The error includes enough context that the reviewer understands what happened without parsing the full audit trail.

---

## Post-execution verification handoff (QA dual-use seam)

The QA agent's dual use is specified in Bundle lifecycle: planning and approval: pre-execution, it produces a Verification Plan; post-execution, it runs the plan against the actual shipped artifact. The seam between the two uses needs explicit specification.

### Artifacts at the seam

**Pre-execution** produces the Verification Plan, stored as a bundle-scoped artifact: `{namespace: bundle, name: verification-plan, version: v1, content_type: application/json}`. The plan includes: acceptance criteria (observable, testable, tied to requirements), test surface (unit, integration, e2e, load, manual smoke with coverage targets), pre-merge gates (CI pass, coverage threshold, security findings resolved, manual smoke checklist), post-ship verification metrics (specific metrics, time windows, expected ranges), and a rollback plan (machine-executable boolean, steps, auto-rollback eligibility).

**Post-execution** receives: the Verification Plan artifact, the merged bundle branch, the worker reports, the CI run results, and access to the deployed artifact (the staging deploy on dev.learhy.net). It produces a Verification Report: `{namespace: bundle, name: verification-report, version: v1, content_type: application/json}`. The report includes: per-criterion pass/fail with evidence, aggregate outcome (passed, failed, partial), failed criteria with descriptions, coverage gaps (what the Verification Plan said to test but couldn't be tested), and a rollback recommendation (trigger, don't trigger, with reasoning).

### Verification failure handling

When the Verification Report's outcome is `failed` or `partial`:

1. **The bundle transitions `verifying → failed`** (not `complete`). The `bundles.outcome_json` is updated with `{status: failed_verification, rationale: <summary of failure>}`.

2. **Recall sequence.** The failed bundle is surfaced through all three approval surfaces. The GitHub Issue gets the `status:verification-failed` label. The bundle enters the Human Review Board's Needs Input column (as specified in the QA dual-use description).

3. **Auto-rollback eligibility.** If the Verification Plan declared auto-rollback eligibility AND stakes are Low AND rollback is machine-executable, the orchestrator spawns a rollback bundle automatically (per the Rollback mechanics section). The original bundle's state remains `failed`; the rollback bundle is a new bundle with `parent_bundle_id` pointing to the failed bundle.

4. **Manual rollback.** If auto-rollback criteria are not met, the reviewer decides: spawn a rollback bundle manually (`studio rollback <bundle-id>`), park the bundle (the work is done but broken; preserve for later debugging), or kill the bundle (manual revert plus cleanup). All three options are reviewer actions, not automatic.

5. **Verification-failed handling when auto-rollback is not configured.** The bundle stays in `failed`. The reviewer can Retry (re-trigger verification, which is idempotent), Override (mark the bundle `complete` despite verification failure, with an audit log entry explaining the override), Park, or Kill. Override is a deliberate "I accept the risk" decision and is tracked in calibration.

### The Verification Report as calibration data

The Verification Report is calibration data on how well the Verification Plan predicted reality. If the QA agent consistently produces plans that pass verification but the shipped product has bugs discovered later (through manual testing or production incidents, when production exists), the QA agent's plans are under-testing. If the QA agent consistently produces plans that fail verification on criteria that turned out to be irrelevant (the plan demanded load testing at 10k RPS but the product gets 10 RPS), the QA agent is over-testing. Both are calibration signals. The Verification Report includes a `plan_quality_self_assessment` field where the QA agent reflects on its own plan's accuracy, stored alongside the scoring calibration data in `memory/calibration/`.

---

## Open questions and flagged decisions

**Default-approve window of 4 hours.** The value was chosen to balance velocity with intervention opportunity, but it's a Review Deck v1 numeric interacting with a v1.1 approval matrix. The interaction was not formally analyzed in v1; track as calibration data and revisit after 30 days of live operation.

**1-hour cooldown with irreversible-carveout.** The distinction between "high risk" and "irreversible" is a new concept introduced by this design. The `irreversible` flag's definition ("rollback is not machine-executable and manual recovery would require >1 hour") is a first draft. It needs calibration against real bundles.

**Cross-target bundles rejected for v1.1.** The limitation is real and the two-bundle workaround is clunky. If the capability-plus-first-use pattern is common, a v1.2 design pass should revisit with a concrete proposal for multi-target DAGs and cross-repo integration steps.

**Abbreviated pre-execution review on Redirect.** The design says review tracks examine only the delta from the prior review on Redirect. This is a performance optimization, not a safety property. If review track agents routinely miss issues in the delta because the prior review's context is stale, full re-review is the fallback. The threshold for "delta is large enough to warrant full re-review" is not specified; the review track agents should self-escalate when the delta exceeds their confidence threshold.

**Verification-driven auto-rollback criteria.** Three conditions: stakes Low, rollback machine-executable, and auto-rollback declared in the Verification Plan. The "stakes Low" condition means auto-rollback never fires for medium or high-stakes bundles, which is conservative. If auto-rollback proves reliable in practice (the rollback bundle succeeds >95% of the time), expanding to medium-stakes bundles is a calibration-driven decision.

**Irreversible flag on bundles.** New field. Needs a slot in the bundle proposal schema and in the approval matrix evaluator. Lightweight addition; the design slot is the main deliverable.

**Parked bundle state.** New terminal state. The reviewer's workflow for parked bundles (how to find them, how to resume them, whether parked bundles count toward the stalled-bundle detector) is not specified. Parked bundles should probably not trigger the stalled-bundle detector (they're parked, not stalled), but they should surface in periodic digests ("3 bundles are parked; oldest is from 2026-04-15").

---

## Rejected alternatives

**Rollback as a non-bundle direct action.** Considered. The orchestrator would issue a `git revert` on the merge commit, push, and re-deploy, all without a bundle lifecycle. Rejected because nontrivial rollbacks (conflicts, data migrations, side effects in external systems) can't be expressed as a single revert commit, and a partial mechanism (use revert for simple rollbacks, escalate to a bundle for complex ones) introduces a classification step that's hard to get right. Rollback-is-a-bundle handles everything uniformly.

**Multi-target bundles with per-target sub-DAGs.** Considered. A bundle with `target: [control-plane, existing-repo:api]` would carry two DAGs, each targeting a different repo, with explicit cross-repo artifact dependencies. Rejected for v1.1 because the complexity (cross-repo integration steps, per-repo approval gates, rollback spanning repos) is disproportionate to the use case volume. The two-bundle workaround with `related_bundle_ids` covers the common case adequately.

**Default-hold for all summary-tier cells.** Considered. Rejected because it eliminates the time-saving benefit of the tier system. Calibration should answer whether default-approve is safe; design-time conservatism shouldn't preempt it.

**24-hour cooldown for all full-review-cooldown bundles.** Considered. Rejected because the 1-hour cooldown achieves the stated goal (forcing function) without imposing a calendar-day delay. The irreversible carve-out addresses the case where a longer cooldown is genuinely warranted.

**Redirect as in-place DAG modification rather than re-planning.** Considered: the reviewer provides new instructions, the planner patches the existing DAG (adds nodes, removes nodes, re-wires edges) rather than producing a new one. Rejected because DAG patching is harder to validate (does the patched DAG still satisfy the static validation rules?) and harder to audit (what exactly changed?) than producing a new DAG from the current state. A new DAG with explicit baseline references is cleaner at the cost of a full planning pass, which is fast relative to execution time.

**Modification without re-scoring.** Considered. Rejected because stale scores mislead the reviewer. A bundle modified to add authentication should not still show risk=0.

---

## Deferred items

**Parked bundle lifecycle.** How parked bundles are discovered, resumed, or cleaned up. The parked state exists in the state machine but the workflow around it (periodic digest surfacing, auto-cleanup after N days, resume-from-parked mechanics) is not specified.

**Multi-target bundles (cross-repo execution).** Deferred to v1.2. The single-target constraint holds for v1.1.

**Abbreviated review threshold on Redirect.** The conditions under which a Redirect's delta is small enough for abbreviated review vs. requiring full re-review are not specified. Review track agents self-escalating is the fallback; a formal threshold (e.g., "more than 30% of DAG nodes changed") is a future refinement.

**Verification-driven auto-rollback for medium-stakes bundles.** Currently restricted to Low stakes. If auto-rollback proves reliable, expanding the eligibility is a calibration-driven decision.

**`irreversible` flag formal schema slot.** The concept is introduced; the exact schema field, its interaction with the approval matrix evaluator, and its surfacing in the reviewer's UI are not specified at field level.

**Rollback bundle calibration.** Rollback bundles are bundles and get calibration data like any other. Whether rollback bundles should be tracked as a separate class for calibration purposes (do rollback bundles have systematically different complexity vs. actual profiles?) is a future question.

---

## Summary of resolutions

What got resolved in this design:

- **Bundle-level input schema.** Four required fields (idea, attachments, metadata) plus optional structured_params, parent_bundle_id, and related_bundle_ids. Mirrors task-level I/O.
- **Bundle-level output schema.** Thirteen field groups covering outcome, product artifacts, artifact manifest, verification, calibration (on all tracked axes), cost, memory pointers, steering events, and metadata. Mirrors task-level I/O.
- **`target:` field semantics.** Three values with explicit decision rules. Control-plane vs. product boundary drawn, with resolution for ambiguous content (agent overrides live in control-plane under `memory/products/<slug>/`). New-repo flow specified end to end (scaffolding, creation, branch protection, registry update). Cross-target bundles rejected for v1.1.
- **Full bundle state machine.** Twelve states, complete transition table (25 legal transitions), illegal-transition error model, and surface observability for every transition.
- **Pause executor mechanics.** Finish in-flight step, halt scheduler, preserve state. Resume with optional notes injected into orchestrator context.
- **Redirect re-planning flow.** Ratified the natural answer: discard current DAG, run planner on current worktree state as fresh bundle, completed work as baseline referenced via artifact descriptors and branch state.
- **Abort executor mechanics.** Worker cancellation propagation, draft PR closure, worktree preservation, capability grant revocation.
- **Rollback as a new bundle.** Manual and automatic triggers. Relationship to Verification Plan's rollback plan. Verification-driven rollback sequence.
- **Pre-execution review track ordering.** Ratified: review tracks run before the approval matrix and feed it. Data flow specified. Matrix decision logic in pseudocode.
- **Modification request re-scoring.** Yes, re-score. Surface should show score deltas.
- **Default action for summary-tier timeouts.** Default-approve for low-risk cells (4-hour window), default-hold for moderate-risk. Calibration tracks default-approve outcomes.
- **Cooldown duration.** 1 hour for full-review-cooldown tier. 24-hour carve-out for bundles flagged `irreversible` (new field). Reviewer override with audit log.
- **Multi-surface race resolution.** First-write wins, second-write fails with conflict error including context. SQLite serialization is the mechanism; faithful serial order is the semantic.
- **QA dual-use seam.** Verification Plan (pre-execution) and Verification Report (post-execution) as explicit artifacts with storage locations. Verification failure handling (recall sequence, auto-rollback eligibility, manual options, override tracking).

What's flagged as open:

- Default-approve window of 4 hours (needs calibration).
- 1-hour cooldown with irreversible carve-out (new concept, needs calibration).
- Abbreviated review threshold on Redirect (not specified; review agents self-escalate as fallback).
- Verification-driven auto-rollback criteria (stakes Low condition is conservative; calibration may expand).

What got newly deferred:

- Parked bundle lifecycle (discovery, resumption, cleanup).
- Multi-target bundles (cross-repo execution) to v1.2.
- `irreversible` flag formal schema slot.
- Rollback bundle calibration as a separate tracking class.
