> NOTE: Two corrections to this prompt before starting:
> CORRECTION 1 - Bundle 2.2 content addressing: This prompt says SHA-256. The spec says BLAKE3. Use BLAKE3.
> CORRECTION 2 - Bundle 2.5 irreversible flag: The cooldown duration (1h vs 24h) depends on an irreversible: bool field not yet in the spec or schema. When you reach that logic, stop and surface to PM before implementing.
> The spec has been significantly updated since this was written. Read specs/agent-orchestration-v1.1.md in full before starting any bundle. Spec beats prompt where they diverge.

---

# Phase 2: Self-Hosting — Build the Rest with the Kernel

## Context

The Phase 1 kernel is running. You can submit bundles, approve them, watch workers run, and see results in SQLite. The system works end-to-end in its narrowest form.

Phase 2 uses the kernel to build the rest of itself. That means: you will propose bundles through the kernel's CLI, the PM will approve them, workers will implement features, and the orchestrator will (progressively) manage its own development. This is the self-hosting bootstrap.

The attached v1.1 spec is the design document for everything you are building. Re-read it before starting this phase. Phase 1 should have changed your understanding of some parts; if the kernel diverged from the spec anywhere, that divergence is now the actual behavior, and you should treat it as the ground truth and note it.

You are working for a solo technical PM. The PM approves bundles but cannot read code. Your accountability mechanisms are the same as Phase 1: PM-level acceptance tests, clear commit history, and honest PR descriptions. The PM's primary interaction with this phase is through the review surfaces you are building — so the most important thing to get right early is the thing the PM looks at first.

The PM's model budget remains DeepSeek-v4-pro on Ollama Cloud. Design your worker tasks to fit within the context and reasoning budget of that model.

## What Phase 2 builds

Phase 2 implements the full Tier 1 and Tier 2 feature set from the spec. The order matters: some components unblock others. A dependency-ordered list follows, organized into bundles. You will submit these as actual bundles through the kernel.

### Bundle 2.1 — Full DAG Executor

Extend the executor from Phase 1 (linear DAGs only) to the full spec:

- Gate nodes: `artifact_property`, `rpc_query`, and `human_approval` predicate kinds. `human_approval` gates surface via the approval requests table; the PM approves or rejects them through the CLI (MCP comes later in this phase).
- Aggregator nodes: all four join modes (`all`, `any`, `quorum`, `first_success`), all three output strategies (`collect`, `first`, `reduce`). Built-in reducers: `majority_vote`, `concatenate`, `select_best_by`, `collect_all`.
- `on_success`, `on_failure`, `always`, and `on_property` edge conditions. The `on_property` expression sublanguage with the EBNF grammar from the spec.
- Dynamic DAG expansion: the graft handler, cycle checking, auto-approve-if-subset logic, `dag_expansions` table mechanics.
- The full `cap.request` RPC method (currently stubbed), which is the mechanism workers use to request expansion.
- Mermaid rendering of bundle DAGs: worker node rectangles, gate diamonds, aggregator hexagons, state-based node colors, capability annotation subtitles, expansion subgraph labels.

Acceptance tests: submit bundles with parallel branches; verify both workers run concurrently up to the semaphore cap. Submit a bundle with a `human_approval` gate; verify the orchestrator halts and waits. Submit a `first_success` aggregator; verify the losing worker is cancelled. Submit a bundle with an `on_property` edge; verify the condition evaluates correctly.

### Bundle 2.2 — Artifact Protocol

Implement the full artifact protocol from the Artifact Protocol section of the spec:

- RPC methods: `artifact.publish(descriptor, data)`, `artifact.fetch(descriptor)`, `artifact.list(filter)`. All capability-checked.
- Content addressing: SHA-256 of raw bytes for opaque content; canonical JSON serialization for structured content.
- `LocalFilesystemArtifactStore`: artifacts stored at `/var/lib/studio/artifacts/<bundle-id>/<hash>`. Metadata in `artifact_metadata` SQLite table.
- GC policy: bundle-scoped artifacts deleted when bundle reaches terminal state (with configurable forensic-retention window for failed bundles); task-scoped artifacts deleted when their producing node reaches terminal state; global artifacts retained until explicit deletion or expiry.
- The `artifact_refs` join table already exists from Phase 1 schema; populate it on `artifact.publish`.
- Notification mechanism: when a new artifact is published, the executor event pump is notified so downstream nodes waiting on that artifact can become ready.
- `secrets.fetch(name)` RPC: deliver a named secret from the orchestrator's secret store to the requesting worker. Validate against worker's manifest `secrets` grants. Log name (not value) to `memory/audit/credential-use.jsonl`. In Phase 2, secrets come from environment variables configured in `settings.json`; proper secret-store integration is a later refinement.

Acceptance tests: worker A publishes an artifact; worker B fetches it via `artifact.fetch`; verify the artifact bytes match. Verify GC deletes bundle-scoped artifacts after bundle completes. Verify `secrets.fetch` logs the credential name. Verify `secrets.fetch` is denied for a secret not in the worker's manifest.

### Bundle 2.3 — Bundler Agent

Implement the bundler agent worker class. This is the first real agent (not a stub) in the system. The bundler:

- Reads an inbound idea (free-text from the task spec's `objective` field).
- Reads memory: `memory/decisions/` for relevant prior decisions, `memory/killed/` for prior killed ideas, `memory/calibration/` for scoring correction factors.
- Reads `AGENTS.md` for system context.
- Produces, via the configured LLM (deepseek-v4-pro, thinking_mode: high): an RFC, a requirements summary, an implementation plan, a task DAG with capability manifests for each node, a complexity score (0-10) with factor breakdown, a risk score (0-10) with factor breakdown, and a concerns section.
- Publishes the bundle proposal as a structured artifact.
- Calls `worker.final_report` with the bundle proposal as structured output.

The bundler uses the Artifact Protocol to publish its outputs and the full task spec format (with DAG and manifests) specified in the v1.1 spec. Its output is what the PM reviews via the approval surfaces.

This is the first bundle where you will use the LLM inside a worker in a non-trivial way. Write the bundler's system prompt carefully. It should reference the spec's scoring factors, the mandatory-review triggers, the concerns-section requirement ("no concerns on a high-risk bundle is a calibration signal"), and the review deck TL;DR structure.

Acceptance tests: submit a free-text idea ("Add a health check endpoint to the API"). Verify the bundler produces a well-formed RFC, a non-empty concerns section, and a DAG with at least one node. Verify complexity and risk scores are populated with factor breakdowns. Verify the bundle proposal appears in `studio show <id>`.

### Bundle 2.4 — Pre-execution Review Tracks

Implement the three pre-execution review tracks as worker classes:

**Adversarial critique worker:** Reads the bundler's RFC, requirements, and implementation plan. Produces findings tagged `severity:{low|med|high}` and `status:{resolved|accepted-risk|unresolved}`. Writes findings to the bundle artifact and to `memory/security-findings/`.

**Security review worker:** Reads the bundle. Produces a threat model (for bundles touching auth, data, external surfaces, secrets, billing, or PII) and findings tagged `severity:{info|low|med|high|critical}`. Enforces hard rules: critical findings always require explicit human review even when resolved; high findings disable auto-ship; auth/billing/secrets/PII bundles never auto-ship. Writes to `memory/security-findings/`.

**QA verification planning worker:** Produces a Verification Plan: acceptance criteria, test surface, pre-merge gates, post-ship verification metrics, rollback plan. Enforces hard rules: no bundle reaches human review without a Verification Plan; bundles without a viable rollback auto-bump Reversibility to 3 in stakes scoring. Writes plan to `memory/verification-plans/`.

These three workers run as a parallel fan-out in the planning DAG, with an aggregator that collects their findings before the approval surface presents the bundle to the PM.

Acceptance tests: submit a bundle with a high-risk element (touches a secrets path). Verify the security review produces at least one finding. Verify a bundle without a rollback plan has Reversibility bumped to 3. Verify the QA agent produces a non-empty Verification Plan with acceptance criteria.

### Bundle 2.5 — Approval Matrix and Review Surface

Implement the full approval matrix from the Bundle lifecycle section:

- Complexity and risk scoring from the bundler's output.
- The 3x3 matrix mapping to four tiers: auto, auto-notify, summary, full-review, full-review-with-cooldown.
- Mandatory-review triggers from `settings.json`.
- Pre-execution track outputs feeding the matrix (unresolved security findings of medium+ disable auto-ship; no rollback plan blocks auto-ship; auth/billing/secrets/PII blocks auto-ship).
- Bundler self-escalation (may escalate, may not de-escalate).

Extend the `studio` CLI with review-deck output:

```
studio deck <bundle-id>    # print the full review deck: TL;DR, body, pre-execution findings
studio pending             # list all bundles waiting for PM action, with tier and acting-soon status
```

The review deck format follows the spec's Review Deck TL;DR and Body structure: Proposal, Recommendation + confidence %, counter-case, Cost, Predicted impact, Biggest risk, Stakes line. Then: Requirements summary, RFC summary, Implementation plan, Critique findings, Alternatives considered.

Acceptance tests: submit a bundle that scores Complexity 2, Risk 1. Verify it auto-approves without PM action. Submit a bundle that scores Complexity 8, Risk 7. Verify it enters full-review-with-cooldown. Verify `studio deck` renders a readable review. Verify the cooldown blocks approval for 60 minutes.

### Bundle 2.6 — Developer Worker (Real)

Replace the Phase 1 stub developer worker with a real implementation. The developer worker:

- Reads its task spec: objective, RFC excerpt, verification plan excerpt, `AGENTS.md`, capability manifest subset, model config.
- Creates a git worktree on a sub-branch of the bundle's feature branch.
- Invokes OpenCode against the configured model (kimi-k2.6 per the spec's model mapping) in headless mode, passing the task objective.
- Emits phase-tagged heartbeats: `thinking`, `tool-call`, `writing-code`, `running-tests`, `idle`.
- On completion, commits to the sub-branch, runs the tests specified in the Verification Plan's pre-merge gates, and calls `worker.final_report` with actual `files_changed`, `tests_run`, and outcome.
- If OpenCode gets stuck (exceeds 3 stuck-iterations or the configured timeout), calls `worker.request_human_input` (now implemented, not stubbed) to surface the blockage.

This bundle is the riskiest in Phase 2 because it depends on OpenCode and Ollama Cloud working reliably together. Submit it on a small, well-defined task so the first real run is observable. Suggested first task: "Add a `GET /health` endpoint that returns `{status: ok, version: <version>}` to the orchestrator's HTTP server."

Acceptance tests: submit the health-endpoint task. Verify the developer worker creates a sub-branch, commits code, runs tests, and reports completion. Verify `git log` on the feature branch shows the worker's commit. Verify the health endpoint actually works after the bundle ships.

### Bundle 2.7 — MCP Server

Implement the MCP server from the Surfaces section as a separate process communicating with the orchestrator over Unix domain socket.

Implement all tools, resources, and prompts from the spec:

Tools: `list_pending_bundles`, `get_bundle`, `approve_bundle`, `reject_bundle`, `request_modification`, `escalate_bundle`, `pause_bundle`, `resume_bundle`, `kill_worker`, `grant_capability`, `revoke_capability`.

Resources: `studio://bundles/pending`, `studio://bundles/{id}`, `studio://bundles/{id}/workers`, `studio://workers/active`, `studio://workers/{bundle_id}/{worker_id}/report`, `studio://capabilities/manifest`, `studio://capabilities/pending-requests`, `studio://memory/agents/{repo}`, `studio://calibration/recent`, `studio://decisions/recent`, `studio://system/status`.

Prompts: `review-pending`, `morning-digest`, `risk-audit`, `bundle-deep-dive`.

Approval actions require explicit tool-confirmation (MCP's built-in requirement). The server is managed by its own systemd unit (`studio-mcp.service`) with `Requires=studio-orchestrator.service`.

Acceptance tests: connect Claude Desktop to the MCP endpoint. Verify `list_pending_bundles` returns the current queue. Verify `approve_bundle` transitions a bundle to approved. Verify `studio://bundles/pending` resource renders the pending queue. Verify approval via MCP mirrors to a GitHub Issue comment (see Bundle 2.8 below — these two may need to be sequenced or acceptance test 2.7 can stub the GitHub side).

### Bundle 2.8 — GitHub Integration

Implement the GitHub App integration from the Identity model, Surfaces, and Bundle lifecycle sections.

- GitHub App registration (manual one-time setup by PM; document the steps clearly in `docs/github-app-setup.md`).
- Webhook receiver at `127.0.0.1:7810` (already in the orchestrator's HTTP server skeleton from Phase 1; wire up the handlers).
- Bundle proposal creates a GitHub Issue in the control-plane repo with structured template and tier label.
- Decisions expressed via issue comments (`/approve`, `/reject [reason]`, `/modify [instructions]`) are reflected in the orchestrator state machine.
- MCP actions mirror to GitHub Issue comments.
- GitHub Issue actions mirror to the MCP-side decision log.
- Worker assigns the reviewer on issues requiring attention.
- `studio-agents[bot]` as the bot identity; role-tagged commit author identity per the spec.

Acceptance tests: submit a bundle and verify a GitHub Issue is created. Post `/approve` as a comment. Verify the orchestrator transitions the bundle to approved. Verify the MCP resource `studio://bundles/pending` updates accordingly. Verify the issue gets the correct tier label.

### Bundle 2.9 — Post-execution QA and Calibration

Implement the QA agent's second job (post-execution validation) and the calibration loop.

**Post-execution QA:** After all workers complete, spawn a QA validation worker that reads the Verification Plan and runs it against the merged bundle branch. Produces a Verification Report. Failed criteria trigger auto-rollback (if rollback is machine-executable and stakes are Low) or `status:verification-failed` issue. Writes the Verification Report to `memory/verification-plans/`.

**Calibration loop:** After every bundle reaches a terminal state, record estimated vs. actual on all tracked axes: code surface, build cost, agent-iteration count, blast-radius, predicted impact. Write to `memory/calibration/scoring-outcomes.jsonl`. Post-mortem prompt fires when any axis diverges by >50%. Add a `studio calibration-report` CLI command that reads `memory/calibration/` and prints current correction factors.

Acceptance tests: complete a full bundle end-to-end (bundler -> review tracks -> PM approval -> developer worker -> QA validation -> complete). Verify the Verification Report is written. Verify calibration data is written to `memory/calibration/`. Run `studio calibration-report` and verify it prints at least the completed bundle's actuals.

## Build process and workflow

Each bundle above should be submitted through the kernel in dependency order. The bundler agent (Bundle 2.3) cannot run until it exists, so Bundle 2.3 requires a bootstrapping exception: submit it manually (you write the RFC and DAG spec directly), have the PM approve it via `studio approve`, and then the worker that runs is the one you build. After 2.3 is complete, subsequent bundles can go through the bundler.

This bootstrapping sequence is intentional and mirrors the self-hosting design. Document the seam explicitly in `AGENTS.md`.

At each bundle, follow the same delivery protocol as Phase 1: branch named `phase-2/<bundle-slug>`, PR with acceptance test output, deferred items list, spec divergences noted.

## What Phase 2 explicitly excludes

Do not build in Phase 2:

- Notification relay (Slack, Discord, SMS) — architectural hooks exist; wire them when needed.
- Egress proxy with hostname-based filtering — network isolation remains permissive until this is built; this is a Phase 3 item.
- Schema versioning and migration tooling beyond what Phase 2 needs.
- k8s deployment support.
- Multiple GitHub Apps.
- Performance or compliance review tracks.
- Multi-agent support per worker class.
- Persistent-log Agent Activity Board.

Add each to `DEFERRED.md` if you find yourself reaching for them.

## AGENTS.md maintenance

After each bundle, update `AGENTS.md` to reflect what's now in the system. Future agents (including future instances of you) will read this file before starting work. It should always describe the current state of the codebase accurately, including: what's implemented, what's deferred, where the key seams are, and the commands to run tests and start the system.

## Stop-and-confirm protocol

If at any point you discover that the spec is ambiguous in a way that would require an architectural decision, stop. Do not guess. Write a short note describing the ambiguity and your best read of the options, and surface it to the PM before writing code. An incorrect architectural decision embedded in Phase 2 is much harder to fix than a delayed start.
