# Agent Orchestration System: v1.1 Design

## Overview and scope

This document specifies v1.1 of the agent orchestration system. v1.1 covers three tightly coupled concerns: the security architecture (how the system constrains what agents can do), the capability model (how those constraints are declared, granted, and enforced), and the execution model (how bundles of work are planned, decomposed, run, and integrated). It builds on v1 of the Review Deck system, which defined the human-in-the-loop review surface for ship/no-ship decisions. v1 covered the cognitive bottleneck (the founder's attention against the volume of agent-prepared work). v1.1 covers everything that has to be true of the system below the Review Deck for it to safely produce the artifacts the deck reviews. The two specs compose: v1.1 is what runs; v1 is what the human sees.

Scope deliberately includes the parts of the surrounding system that intersect security, capability, and execution. The GitHub App identity model is in scope because it determines who can do what. The MCP, GitHub Issues, and CLI surfaces are in scope because they're the human side of capability grants and approval. The bundle approval flow with stakes scoring is in scope because it gates execution. The worker environment (compute substrate, base images, coding agent, model mapping) is in scope because it constrains what workers can plausibly do. The DAG executor (node lifecycle, scheduling, checkpointing, expansion mechanics) is in scope because it is the runtime that drives execution; it was the largest deferred item from the initial v1.1 consolidation and is now folded in.

Scope deliberately does not include re-specifying numerics that v1 already pinned down: the 75% confidence floor, the 8-hour stalled-bundle detector, the 48-hour low-stakes auto-ship window, the 5/10/21-day high-stakes escalation ladder, the 12-hour acting-soon label window, and similar values are defined in the Review Deck v1 spec and referenced here without re-derivation. If those numbers change, they change in v1.

The reader should assume the following baseline:

The deployment target is initially a single self-hosted box at dev.learhy.net, running on bare-metal Debian with 30 GB of RAM. Distribution to a Kubernetes cluster is a stated future target. The architecture is shaped so that k8s deployment is additive rather than invasive, achieved through interface boundaries rather than runtime conditionals.

The execution model is agent-driven. Wall-clock estimates, cost models, and review timeouts assume agents do the work. There is no human-built mode in v1.1, and the cost ceiling per bundle is none, because token spend is flat-rate and compute is owned hardware. Gating happens on complexity and risk, not on consumption.

The reviewer is a single solo technical founder. Multi-reviewer support is out of scope.

## Threat model and trust assumptions

The system runs LLM-generated and LLM-driven code. The primary threat is not adversarial humans but unconstrained agent behavior: an LLM proposes something destructive (intentionally hallucinated, or sycophantically following a malformed prompt), or a worker bypasses its own self-imposed limits because the model produced code that ignored a wrapped HTTP client and called something like `subprocess.run(["curl", ...])` directly. The capability model exists to make these paths impossible at the kernel level, not impolite at the application level. This was a load-bearing argument in the bubblewrap-vs-bind-mount decision: filesystem isolation alone leaves network and process namespaces unconstrained, so a worker's rogue subprocess is unbounded. Network namespaces close that door.

Within that primary threat, the trust assumptions are explicit:

**The orchestrator core process is the trust root**, by deliberate design. Capability-checking the orchestrator itself was considered and rejected. Splitting the orchestrator into a "brain" (state machine, decision logic) and "hands" (the thing that mutates filesystem, spawns processes, makes network calls) would have just relocated the trust root into the hands. The brain still has total power because it can ask the hands to do anything within the brain's policy, and the policy has to allow everything the orchestrator legitimately needs. The mitigation is to keep the orchestrator's privileged surface narrow and statically defined, log its actions heavily, and treat untrusted inputs (bundle proposals, webhook payloads, MCP messages) with maximum suspicion at the parsing boundary. On Kubernetes, this same orchestrator runs against a tightly scoped ServiceAccount; the SA's RBAC is the equivalent of the local-deployment systemd hardening, and should be reviewed with the same rigor as the orchestrator's privileged code paths.

**Workers are not trusted.** Every privileged action a worker can take is mediated through the orchestrator, either via the RPC protocol (which is itself capability-checked) or via OS-level enforcement (filesystem visibility, process namespace, network namespace, seccomp profile). The capability manifest is the source of truth for what each worker is allowed to do; the WorkerRunner translates the manifest into kernel-level enforcement; the orchestrator's RPC dispatcher checks every method call against the worker's grants.

**The bundler agent is trusted to plan honestly.** When it scores complexity and risk, when it surfaces concerns, when it proposes a task DAG, the system depends on it not gaming itself. There is no kernel-level defense against a bundler that lies. The mitigation is calibration: predicted-vs-actual outcomes are tracked across all dimensions (complexity, risk, code surface, build cost, ongoing cost, agent-iteration count, blast-radius-realized, predicted-impact-vs-observed), divergences greater than 50% on any axis trigger a post-mortem prompt, and systematic miscalibration surfaces as periodic digests for prompt or weight tuning. The "no concerns" output on a high-risk bundle is itself a calibration signal.

**The human reviewer is trusted absolutely.** Approval via MCP, GitHub Issues, or CLI all reach the same orchestrator state machine and are not differentially trusted. The MCP surface in particular requires explicit human gesture for any action (Claude Desktop can recommend, the human must click via MCP's tool-confirmation flow), so the LLM-mediated review surface doesn't become an additional attack vector.

**The host (dev.learhy.net) is trusted at the OS level.** Worker isolation runs inside that trust boundary. If the host is compromised, the system is compromised. Backups of `/memory` and the SQLite state file are an explicit operator responsibility called out in the ops checklist; v1.1 does not manage them automatically.

**Production does not exist** in v1.1. There is no production access policy because there is no production. All staging deploys land on dev.learhy.net itself. When production becomes real, it gets its own RFC and its own capability scopes; for now, the capability manifest simply has no `production-*` capabilities defined.

A key invariant: this trust model assumes a single tenant. "Distributable on k8s" in the v1.1 sense means customers deploy on their own clusters, single-tenant per install. Multi-tenant SaaS would introduce other tenants as an additional adversary class and make the operator a trust root in ways they aren't here. That's out of scope for v1.1.

## Identity model

A single GitHub App is the bot identity for the entire system in v1. Operationally it appears in audit trails as something like `studio-agents[bot]`, a Bot-type actor distinct from any human user. This matters because GitHub distinguishes Bot actors from User actors at the API level, and the distinction supports standard governance features: branch protection rules that treat bot PRs differently, CODEOWNERS that exclude or require human review for bot changes to critical paths, CI workflows scoped to author type. None of this is available if automation runs through a regular User account, where bot behavior is detectable only behaviorally, not categorically.

Role differentiation among agents (bundler, critique, developer, QA, etc.) is achieved within the single App via two mechanisms. First, commit author identity is set per-role at commit time: `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` are populated with a role-specific identity such as `bundler-agent@<domain>` or `critique-agent@<domain>`, while the App handles push authentication. Git's separation of author from committer makes this clean. Second, every comment and PR description posted by an agent includes a structured prefix like `[critique-agent]` so the role is visible without parsing commit metadata.

The alternatives considered and not adopted in v1.1:

Multiple GitHub Apps, one per role, would give finer-grained permission scoping (Critique-App needs only read; Developer-App needs write to product repos; QA-App needs read plus PR comments). This is the right answer once permission scoping becomes a real concern, especially when Developer agents start needing production-deploy credentials that other roles shouldn't have. It costs four sets of credentials and four installation flows, which is too much operational overhead for v1. v2 candidate.

App-plus-machine-user-accounts would combine the worst of bot-account opacity with App complexity. Not recommended.

App permissions for v1: contents, issues, pull requests, projects, and metadata at read/write; administration at write (required for repository creation in the new-product-repo flow); webhooks for reactive behavior. The App is installed at the personal GitHub org level, which simplifies the administration scope.

A note on supersedability: the role mapping (bundler, critique, etc. to author identities) lives in `settings.json` under `agents.identity.author_emails`, and the App ID lives there too. Migrating to multiple Apps in v2 means changing the schema to per-role App IDs and updating the App installation; nothing in the orchestrator's logic depends on the single-App assumption beyond what's expressed in this config.

## Architecture: topology and processes

The system runs as two long-running processes on the host plus ephemeral worker subprocesses. Both long-running processes are managed by systemd.

The orchestrator core is one process. It owns the bundle state machine, the worker pool manager, the capability enforcer, the audit logger, and the GitHub webhook receiver. These responsibilities all share state intensely; splitting them would add IPC overhead and consistency complexity for no benefit. The webhook receiver is folded in because it's just an HTTP handler that mutates state. Internally, the orchestrator core runs as async Python, exposing a webhook endpoint over loopback HTTP (to be reverse-proxied by Caddy with TLS termination) and a Unix domain socket for IPC with the MCP server.

The MCP server is a separate process. Its failure modes are independent: a bad client request, a memory leak in an MCP framework, or a deploy of a new MCP version should not kill in-flight bundles. The MCP server connects to the orchestrator over the Unix domain socket. systemd's `Requires=` ordering ensures the MCP server starts only after the orchestrator is ready (both use `Type=notify` with `sd_notify` so readiness is signaled when the process is actually accepting traffic, not just when the binary started). If the orchestrator dies, the MCP server dies with it, since it has no reason to exist alone.

Workers are subprocesses, spawned per task by the orchestrator core, isolated via bubblewrap (see worker isolation below). Each worker exits on completion (success, failure, or timeout). Worker output is captured via the bidirectional RPC channel; the orchestrator does not parse stdout for control flow.

The runtime topology:

```
dev.learhy.net
├── studio-orchestrator (long-running)
│   ├── HTTP server on 127.0.0.1:7810 for GitHub webhooks
│   ├── Unix socket at /run/studio/orchestrator.sock for MCP IPC
│   ├── SQLite at /var/lib/studio/state.db
│   └── spawns: worker subprocesses on demand
├── studio-mcp (long-running)
│   ├── HTTPS endpoint, proxied by Caddy from studio.learhy.net/mcp
│   └── connects to orchestrator via Unix socket
└── caddy
    ├── terminates TLS for the webhook endpoint
    └── proxies the MCP endpoint
```

**Implementation language: Python.** This was chosen against the usual instinct toward Go or Rust for a long-running orchestration process. The reasons: the orchestrator interacts heavily with LLM APIs and agent frameworks, all of which are Python-first; the state machine logic is not perf-critical at the throughput v1.1 will see (SQLite handles hundreds of bundles per day trivially); async Python (`asyncio` plus `aiosqlite`) gives the concurrency model needed without going multi-threaded; subprocess management ergonomics are good; iteration speed matters more than performance for a system whose design is still being tuned. The cost is heavier process model and care needed not to CPU-bottleneck the event loop. If performance ever becomes a real bottleneck (it won't in v1.1), the migration target would be Rust. That migration is premature.

**State persistence: a single SQLite file** at `/var/lib/studio/state.db`, WAL mode enabled. The orchestrator core is the single writer; the MCP server connects as a reader. WAL mode makes this clean. Atomic transactions across multi-table state changes (transitioning a bundle from proposed to approved while inserting an audit log entry while updating the capability log) are first-class. Backup is a file copy. No separate database service to operate.

The schema sketch (subject to refinement in implementation):

```sql
CREATE TABLE bundles (
  id TEXT PRIMARY KEY,             -- ULID
  repo TEXT NOT NULL,
  state TEXT NOT NULL,             -- proposed|in_review|approved|in_progress|paused|redirecting|verifying|complete|parked|failed|rejected|aborted
  tier TEXT NOT NULL,              -- auto|auto_notify|summary|full_review|full_review_cooldown
  complexity_score INTEGER,
  risk_score INTEGER,
  proposal_json TEXT NOT NULL,
  concerns_json TEXT,
  created_at INTEGER NOT NULL,
  approved_at INTEGER,
  approved_by TEXT,
  completed_at INTEGER,
  outcome_json TEXT
);

CREATE TABLE workers (
  id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  task_index INTEGER NOT NULL,
  state TEXT NOT NULL,             -- pending|running|paused|complete|failed|killed|connection_lost
  pid INTEGER,
  started_at INTEGER,
  last_heartbeat INTEGER,
  ended_at INTEGER,
  exit_reason TEXT,
  task_spec_json TEXT NOT NULL,
  report_json TEXT
);

CREATE TABLE capabilities (
  id TEXT PRIMARY KEY,
  scope_json TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  granted_by TEXT NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER,
  revoke_reason TEXT
);

CREATE TABLE capability_requests (
  id TEXT PRIMARY KEY,
  bundle_id TEXT REFERENCES bundles(id),
  worker_id TEXT REFERENCES workers(id),
  requested_scope_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  decided_at INTEGER,
  decided_by TEXT
);

CREATE TABLE approval_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  decision TEXT NOT NULL,
  surface TEXT NOT NULL,           -- mcp|github_issue|cli|auto
  actor TEXT NOT NULL,
  comment TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE capability_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT REFERENCES workers(id),
  bundle_id TEXT REFERENCES bundles(id),
  requested_op TEXT NOT NULL,
  result TEXT NOT NULL,
  matched_capability_id TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  subject_type TEXT,
  subject_id TEXT,
  payload_json TEXT,
  created_at INTEGER NOT NULL
);
```

Large fields are JSON because their internal schema will evolve. SQLite's JSON1 functions handle ad-hoc query needs. The `audit_log` table is the catch-all for cross-cutting timeline reconstruction; everything important also has its own typed table.

The DAG executor adds further tables (`dag_nodes`, `dag_edges`, `node_state_history`, `dag_expansions`, `approval_requests`, `artifact_refs`) covering DAG state, transition history, expansion provenance, the unified approval-request lifecycle, and the executor's view of artifact publication. Those are specified in the DAG executor section; the artifact metadata schema backing `artifact_refs` is specified in the Artifact Protocol section.

**Bundle state machine.** The full state machine is specified in Bundle lifecycle: execution and integration. The SQLite schema above enumerates all twelve valid values for `bundles.state`. The state machine enforces 25 legal transitions, enumerated in a transition table (each row: `from_state`, `trigger`, `to_state`, `actor`, `side_effects`). Transitions are guarded; any attempt to execute an illegal transition raises `IllegalTransitionError` with the current state, attempted transition, and reason. The error serializes as a JSON-RPC error with code `-32001`. Every transition writes an `audit_log` row and, when triggered by a human reviewer, an `approval_decisions` row. The twelve states are:

| State | Enum value | Terminal | Description |
|-------|-----------|----------|-------------|
| `PROPOSED` | `"proposed"` | no | Bundler produced a proposal; awaiting pre-execution review |
| `IN_REVIEW` | `"in_review"` | no | Pre-execution review tracks running |
| `APPROVED` | `"approved"` | no | Bundle passed review and approval; awaiting execution |
| `IN_PROGRESS` | `"in_progress"` | no | DAG executor driving worker tasks |
| `PAUSED` | `"paused"` | no | Execution halted; workers idle, state preserved |
| `REDIRECTING` | `"redirecting"` | no | Paused bundle being re-planned; transient |
| `VERIFYING` | `"verifying"` | no | All workers complete; QA agent running post-execution verification |
| `COMPLETE` | `"complete"` | yes | Shipped successfully |
| `PARKED` | `"parked"` | yes | Work completed but not merged; preserved |
| `FAILED` | `"failed"` | yes | Execution or verification failed; partial state preserved |
| `REJECTED` | `"rejected"` | yes | Rejected during review; no execution |
| `ABORTED` | `"aborted"` | yes | Reviewer killed bundle mid-flight; partial state preserved |

Terminal states (`complete`, `parked`, `failed`, `rejected`, `aborted`) permit no further transitions. The five non-terminal, non-transient states (`proposed`, `in_review`, `approved`, `in_progress`, `paused`) plus the transient `redirecting` and the `verifying` state form the active lifecycle. Mid-flight steering states (`paused`, `redirecting`) were added during the bundle lifecycle design pass.

**Crash recovery.** On orchestrator startup, the policy is **kill-all**: no attempt is made to resume in-flight workers. Reconstructing live worker connections and reattaching to running subprocesses is hard to get right and the failure modes are untestable at full coverage. The six-step reconciliation sequence is:

1. **Kill-all workers.** Scan `workers` for rows in state `running` or `paused`; mark each `failed` with `exit_reason = 'orchestrator_crash'`.
2. **Reconcile node states to worker states.** For each `dag_nodes` row in state `running`, if its worker is now `failed`, transition the node to `failed` with `failure_reason = 'worker_killed_on_crash'`.
3. **Apply retry policies.** For each node transitioned to `failed` in step 2, evaluate its `retry_policy`. If retries remain, transition to `pending` and let the scheduler re-consider.
4. **Replay unread approval decisions.** For each `approval_requests` row in state `pending`, check secondary surfaces (GitHub Issues comments, MCP-side decision log) for decisions posted while the orchestrator was down.
5. **Re-trigger bundle reconciliation.** Bundles in `verifying` re-trigger verification (idempotent by design). Bundles in `in_progress` re-tick: the scheduler computes the ready set from current node states.
6. **Open surfaces.** Webhook endpoint and MCP socket accept traffic.

The full reconciliation protocol with edge cases (worker completing during crash, aggregator cancellation in-flight) is specified in the DAG executor section under Checkpointing and crash recovery.

Phase 1 note: in Phase 1 (kernel-mode without bundler, MCP, or GitHub Issues), Step 1's paused-worker branch is a no-op (no Phase 1 code path produces `paused` workers), and Step 4 (replay unread approval decisions) is a no-op (the `approval_requests` table is unused). The implementing agent writes the reconciliation logic for all steps; the Phase 1 execution path never hits the paused or approval-replay branches.

## Worker runner abstraction

Workers don't run as plain processes. They run inside an isolation boundary, and the boundary is enforced by an interface called `WorkerRunner`. Two implementations are envisioned: `LocalBwrapWorkerRunner` for v1.1, and `K8sJobWorkerRunner` as a future implementation. Both produce a `WorkerHandle` that the rest of the orchestrator talks to identically.

The interface boundary matters because it lets capability descriptors stay runner-agnostic. Capabilities describe what the worker is allowed to do; the runner translates that into kernel-level enforcement appropriate for the substrate (bubblewrap flags locally, Pod spec plus NetworkPolicy on k8s). The orchestrator does not see substrate-specific concerns, and the capability manifest does not contain substrate-specific syntax. This is the seam that makes the k8s deployment additive: adding `K8sJobWorkerRunner` does not require touching the manifest schema, the orchestrator's bundle logic, or the RPC dispatcher.

**Local: `LocalBwrapWorkerRunner`.** Uses bubblewrap (the same isolation primitive Flatpak builds on) to give each worker its own PID namespace, mount namespace, network namespace, user namespace, and a seccomp filter that blocks dangerous syscall classes. The worker container runs as an unprivileged user mapped into a rootless namespace. The root filesystem is read-only with explicit writable mounts (worktree, scratch, language caches). No host network: by default, no network at all; specific egress is opt-in via a small host-side proxy that the worker's namespace can reach over a unix socket.

This was chosen over the lighter alternative (run as `studio` user with filesystem-level restrictions, with or without bind-mount-based working-directory chroot) because filesystem isolation alone leaves network and process namespaces wide open. A worker that ignores or bypasses its own wrapped HTTP client (for example, an LLM hallucinating a `curl` invocation) is unconstrained without network namespacing. Process namespacing also matters: without it, a worker running as the `studio` uid is in the same PID namespace as the orchestrator and can in principle signal it, ptrace it, or read `/proc` memory. Bubblewrap closes all of these for a per-worker startup overhead in single-digit milliseconds.

The full container alternative (Docker per worker, with hardened defaults) was earlier specified in the worker-environment discussion and is now superseded. See Rejected Alternatives.

**Future: `K8sJobWorkerRunner`.** Each worker is a Job or short-lived Pod. PID, mount, user namespaces are handled by the Pod runtime via `securityContext` (`runAsNonRoot: true`, `runAsUser`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`). Network namespacing becomes a `NetworkPolicy` selecting on a worker label, default-deny egress, allow-list specific destinations (the orchestrator service, an egress proxy for external APIs). Seccomp becomes `seccompProfile: RuntimeDefault` or a custom profile. Resource isolation becomes proper `resources.limits`. The capability manifest is translated to a Pod spec by this runner; the rest of the system is unchanged.

A few k8s-specific concerns are out of scope for the v1.1 implementation but are noted for when k8s becomes a deliverable: pod-eviction event watching for fast detection of cluster-driven worker termination; Helm chart with RBAC manifests, NetworkPolicies, PodSecurityStandards, and Secrets management; image signing (cosign) and SBOM publishing; supported deployment methods documentation (VM/bare-metal vs. k8s) including matrix of what's supported per substrate.

## Worker environment

**Compute substrate.** dev.learhy.net is the self-hosted runner: bare-metal Debian, 30 GB RAM, publicly reachable. The GitHub Actions self-hosted runner agent registers against the personal GitHub org. Ollama Cloud handles all LLM inference; the box runs orchestrator, MCP server, worker containers, builds, and tests, but holds no model weights locally. Backups for `/memory` and the SQLite state file are an explicit operator responsibility called out in the v1.1 ops checklist.

**Concurrency.** Four parallel workers maximum, enforced by an orchestrator semaphore. Real parallelism: Ollama Cloud handles inference scale-out, so there is no local GPU contention. Worker resource overhead (4 GB RAM × 4 workers = 16 GB) leaves comfortable headroom on the 30 GB box for orchestrator, host OS, build caches, and slack.

**Per-worker resource limits**, configurable per worker class via the capability manifest, with these defaults [PROVISIONAL: sized to the 30 GB dev box, need validation against real worker memory profiles]: 4 GB RAM, 2 CPU, 10 GB disk. There is no global wall-clock kill in v1.1; that policy was originally specified but replaced with heartbeat-based liveness plus learned p95 timeouts per worker class, because Ollama Cloud iteration latency is meaningfully slower than frontier-API iteration latency and a single global timeout proved hard to set right. [PROVISIONAL] First-run timeout defaults are 2 hours for small tasks, 4 hours for medium, 8 hours for large. These must survive first contact with real workloads before being ratified.

**Heartbeats** are emitted on every state transition, with a maximum interval of 60 minutes [PROVISIONAL: needs empirical validation against observed Ollama Cloud iteration latency; the 60-minute value may be too long for fast tasks and too short for models with multi-minute think phases]. Each heartbeat includes a `phase` field with values `starting`, `thinking`, `tool-call`, `writing-code`, `running-tests`, or `idle`. The `starting` value is the canonical first heartbeat emitted after worker spawn, before the worker begins meaningful work; it signals that the worker process is alive and the RPC connection is established. All other values describe the worker's current activity, so that "slow but alive" is distinguishable from "wedged." A worker that crosses 2x its expected timeout surfaces as a capability-board entry suggesting model upgrade or task decomposition, rather than being auto-killed.

**Heartbeat-driven worker state updates.** On receiving a `worker.heartbeat` notification, the orchestrator always updates `workers.last_heartbeat` to the current timestamp. On the first heartbeat received from a worker in `pending` state, the orchestrator additionally transitions `workers.state` from `pending → running`. This is the mechanism by which a newly-spawned worker signals that it is alive and has established its RPC connection. Subsequent heartbeats from a worker already in `running`, `paused`, or `connection_lost` state update only `last_heartbeat`. A heartbeat from a worker in a terminal state (`complete`, `failed`, `killed`) is logged as a warning and ignored for state-update purposes.

**Filesystem layout.** The bundle's feature branch is checked out into the runner workspace. Each worker gets its own git worktree on a sub-branch of the feature branch, which is what the per-worker-tree-per-worker-branch decision means in concrete terms. Workers read the entire repo but write only within their assigned subdirectory; this is enforced by orchestrator review of commits, not by filesystem permissions, because tying it to file permissions is too brittle. Persistent caches mount from the host: language package caches (`~/.npm`, `~/.cache/pip`, `~/go/pkg`, `~/.cargo`), Docker layer cache, build artifacts where safe.

**Base images.** Layered Dockerfiles maintained in the control-plane repo, built and stored locally on the box (no registry needed for v1):

- `studio-agent-base`: git, gh, jq, ripgrep, fd, curl, openssl, bash 5+, build-essential, OpenCode CLI, Ollama client. The universal baseline.
- `studio-agent-backend`: base plus Python 3.12 with uv, Node LTS with pnpm, Go, Rust, common DB clients.
- `studio-agent-frontend`: base plus Node LTS with pnpm, Playwright, headless browser.
- `studio-agent-infra`: base plus Docker CLI plus docker-compose. No cloud CLIs in v1, since all staging is on-box.
- `studio-agent-docs`: base plus markdown tooling and diagram generators.

The orchestrator selects the appropriate image per worker assignment. Capability grants update both the relevant Dockerfile and the manifest entry; the next worker spawn picks up the new capability automatically.

**Coding agent.** OpenCode is the sole coding agent in v1, running as the inner loop inside every worker container. It was chosen for four reasons. First, it's model-agnostic by design, treating provider choice as a first-class config dimension. The other candidates considered (Claude Code, Codex, OpenClaw, Hermes Agent) all have an ancestral home in a specific frontier provider and treat alternative models as side paths; for a system built around DeepSeek and Kimi via Ollama Cloud, that mismatch would compound. Second, it reads `AGENTS.md` natively, which is the cross-tool-portable durable-memory file we standardized on. Third, it supports headless and programmatic invocation cleanly via CLI and config file. Fourth, it's open source and forkable, which matters because the orchestrator will eventually need hooks the upstream doesn't provide (for example, emitting phase-tagged heartbeat events from inside the inner loop without brittle stdout parsing).

The architecture supports per-worker-class agent overrides; the schema slot exists in `settings.json`. Multi-agent support is explicitly deferred to v1.2; for v1, only OpenCode is wired up.

**Model mapping.** Configurable in `settings.json` under a `models` block. Defaults:

```jsonc
{
  "agents": {
    "default": "opencode",
    "by_worker_class": {}
  },
  "models": {
    "default": "deepseek-v4-pro:cloud",
    "by_worker_class": {
      "bundler":     { "model": "deepseek-v4-pro:cloud",   "thinking_mode": "high" },
      "planner":     { "model": "deepseek-v4-pro:cloud",   "thinking_mode": "high" },
      "critique":    { "model": "deepseek-v4-pro:cloud",   "thinking_mode": "max"  },
      "developer":   { "model": "kimi-k2.6:cloud" },
      "lightweight": { "model": "deepseek-v4-flash:cloud", "thinking_mode": "non-think" }
    }
  },
  "ollama_cloud": {
    "base_url": "https://ollama.com/api",
    "rate_limits": "learn-empirically",
    "unreachability_policy": {
      "health_check_interval_seconds": 30,        // [PROVISIONAL]
      "grace_window_minutes": 5,                   // [PROVISIONAL]
      "on_grace_expiry": "fail-with-retry"
    }
  }
}
```

The reasoning behind the choices: deepseek-v4-pro for bundler, planner, and critique because it's the strongest reasoning model in the available catalog with a 1M context window (which matters for roles that ingest large RFC plus memory excerpts); critique uses Max thinking mode because deeper reasoning has the highest payoff per token in that role. kimi-k2.6 for developer because it's purpose-built for long-horizon coding across Rust, Go, Python, frontend, and DevOps domains, with native multimodal support and 256K context (sufficient for tasks scoped to a single subdirectory). deepseek-v4-flash for lightweight tasks (linter fixes, doc tweaks, commit messages) because it's faster and cheaper.

**Rate limits** are learned empirically. Workers emit `rate-limit-observed` signals (with `retry-after` header where present) into `worker-report.json`. The orchestrator aggregates these into `memory/capabilities/rate-limit-observations.jsonl` and adapts spawn rate when patterns emerge. No hard-coded ceilings in v1.

**Ollama Cloud unreachability.** Every 30 seconds [PROVISIONAL], the orchestrator runs a cheap health check against a known endpoint. On failure: pause new worker spawns, mark in-flight workers as `paused-external-dependency`, allow a 5-minute grace window [PROVISIONAL] for transient blips. On grace expiry: fail in-flight workers gracefully with auto-retry on the same task once reachability returns. System status surfaces in the orchestrator dashboard and CLI. Clean failure semantics, no zombie workers consuming runner slots while waiting.

**Network egress** from the worker container is mediated by the host-side egress proxy. Hostname-based grants (rather than CIDRs) are first-class in the manifest, and the proxy does the L7 lookup. The host's own firewall is operator-maintained and not duplicated inside containers.

**Secrets.** GitHub Actions secrets for v1, scoped per repo, mounted into worker containers as environment variables only when the worker class declares the capability. An audit log entry records every secret name (not value) accessed per worker per task, in `memory/audit/credential-use.jsonl`. The longer-term intent is to migrate from env-var delivery to RPC-fetched short-lived credentials (`secrets.fetch(name)` over the worker RPC). The `secrets.fetch` RPC method and its audit trail are fully specified in the Artifact Protocol section. The `env` and `file` delivery mechanisms remain as fallbacks for legacy tools.

## Worker RPC protocol

Workers communicate with the orchestrator over a bidirectional RPC channel. Bidirectionality is non-negotiable: the unidirectional alternative (workers print structured JSON to stdout, orchestrator parses) was rejected explicitly because future use cases (mid-task context injection, prepare-for-handoff coordination, pause/resume signaling) need orchestrator-to-worker calls and locking that out now would be a costly backtrack.

**Protocol.** JSON-RPC 2.0, with length-prefixed framing (a 4-byte big-endian length, then a JSON payload) over a duplex byte stream. JSON-RPC 2.0 was chosen because it has well-defined semantics for both calls and notifications, supports both directions natively, has a standard error model, and has libraries in every language. The framing is intentionally transport-agnostic: locally the byte stream is a Unix domain socket; on Kubernetes it becomes a WebSocket connection over TLS, or upgrades to gRPC if JSON-RPC's lack of streaming primitives proves painful in practice. The method surface and dispatcher do not change with transport.

**Authentication.** Locally, the orchestrator generates a 256-bit token per worker, passes it via the `STUDIO_WORKER_TOKEN` environment variable (private to the worker's process), and the worker presents it as the first message. The orchestrator validates and binds the connection to a worker ID. The token is single-use; presenting it on a second connection fails. On Kubernetes, the worker pod gets a token mounted as a projected ServiceAccount token (k8s issues short-lived audience-bound tokens natively via the TokenRequest API), and the orchestrator validates via TokenReview. This is much stronger than env-var tokens because tokens are short-lived, audience-scoped, and revoked when the pod terminates.

**Method namespacing.** Methods are organized by family: `worker.*`, `cap.*`, `artifact.*`, `secrets.*`, etc. This lets new method families be added without name collisions.

**Call vs. notification classification.** JSON-RPC 2.0 distinguishes calls (which carry an `id` and require a response) from notifications (no `id`, no response). Worker-to-orchestrator notifications: `worker.heartbeat`, `worker.log`, `worker.progress_report`. All other worker-to-orchestrator methods are calls. All orchestrator-to-worker methods are calls (they require acknowledgment).

**Worker-to-orchestrator methods:**

**`worker.heartbeat`** — Notification only. No response expected.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.heartbeat",
  "params": {
    "phase": "starting" | "thinking" | "tool-call" | "writing-code" | "running-tests" | "idle",
    "progress": "<human-readable summary>",
    "current_step": "<string or null>",
    "estimated_completion_seconds": <int or null>
  }
}
```

**`worker.log`** — Notification only. No response expected.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.log",
  "params": {
    "level": "debug" | "info" | "warn" | "error",
    "message": "<string>",
    "structured_data": { ... } | null
  }
}
```

**`cap.request`** — Synchronous call. Blocks the worker until human or auto decision.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "cap.request",
  "params": {
    "scope_json": { ... },            // capability manifest fragment
    "rationale": "<why this capability is needed>",
    "urgency": "blocking" | "degrading" | "friction"
  },
  "id": <int>
}

Response (granted):
{
  "jsonrpc": "2.0",
  "result": {
    "granted": true,
    "capability_id": "<ulid>",
    "expires_at": <unix_timestamp or null>
  },
  "id": <int>
}

Response (denied):
{
  "jsonrpc": "2.0",
  "result": {
    "granted": false,
    "denied_reason": "<human-readable explanation>"
  },
  "id": <int>
}
```

**`cap.check`** — Fast path for already-granted capabilities. Returns synchronously.

`op_descriptor` format: `<category>.<operation>[:<resource>]` where category is one of `filesystem`, `network`, `process`, `secrets`, `rpc`, `resources`. The resource segment is optional and category-dependent.

Examples:
- `filesystem.write:/work/src/main.py` — check write permission for a specific path
- `filesystem.read:/work/src/config` — check read permission for a path
- `network.egress:api.github.com:443` — check egress to a host:port
- `process.exec:/usr/bin/git` — check binary execution permission
- `rpc.method:artifact.publish` — check RPC method invocation permission
- `rpc.artifact_access.read:bundle:test-results:*` — check artifact read pattern

The dispatcher parses the category prefix, extracts the operation and resource, and dispatches to the appropriate category-specific capability check against the worker's manifest.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "cap.check",
  "params": {
    "op_descriptor": "<category>.<operation>[:<resource>]"
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "allowed": true | false,
    "capability_id": "<ulid or null>"
  },
  "id": <int>
}
```

**`worker.progress_report`** — Notification only. Structured progress update.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.progress_report",
  "params": {
    "stage": "<string>",
    "percent": 0-100,
    "message": "<human-readable string>"
  }
}
```

**`artifact.request`** — Superseded by `artifact.publish` and `artifact.fetch`. Retained as a protocol-reserved stub that returns `-32000 method_not_implemented`. See Artifact Protocol.

**`worker.request_human_input`** — Escape hatch; surfaces via the approval channel.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.request_human_input",
  "params": {
    "question": "<string>",
    "context": "<string>",
    "options": ["<string>", ...] | null
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "response": "<human-provided string>",
    "responded_at": <unix_timestamp>,
    "responded_by": "<identity string>"
  },
  "id": <int>
}
```

In v1.1 this method returns `-32000 method_not_implemented`. It is protocol-reserved for a future version where the human-input path is built.

**`worker.final_report`** — Terminal call before worker exit. Initiates worker teardown.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.final_report",
  "params": {
    "outcome": "success" | "failure" | "paused" | "timeout",
    "files_changed": ["<path>", ...],
    "tests_run": <int>,
    "tests_passed": <int>,
    "tests_failed": <int>,
    "artifacts_produced": [<descriptor>, ...],
    "errors": ["<string>", ...],
    "summary": "<human-readable string>"
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "acknowledged": true
  },
  "id": <int>
}
```

**Orchestrator-to-worker methods:**

**`worker.pause`** — Request worker to checkpoint and stop. Worker must acknowledge.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.pause",
  "params": {
    "reason": "<string>"
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "acknowledged": true,
    "current_phase": "<phase string>"
  },
  "id": <int>
}
```

**`worker.resume`** — Request worker to continue from paused state.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.resume",
  "params": {
    "note": "<string or null>"
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "acknowledged": true,
    "note": "<string or null>"
  },
  "id": <int>
}
```

**`worker.cancel`** — Request worker to clean up and exit.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.cancel",
  "params": {
    "reason": "<string>",
    "grace_seconds": 30
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "acknowledged": true
  },
  "id": <int>
}
```

After the grace period the worker receives SIGTERM, then SIGKILL after 10 additional seconds.

**`worker.query_status`** — Request worker's self-reported state.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.query_status",
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "phase": "<string>",
    "current_step": "<string or null>",
    "progress_percent": 0-100,
    "uptime_seconds": <int>
  },
  "id": <int>
}
```

**`worker.inject_context`** — Push new information to a worker mid-task.

```
Request:
{
  "jsonrpc": "2.0",
  "method": "worker.inject_context",
  "params": {
    "data": { ... },
    "reason": "<string>"
  },
  "id": <int>
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "acknowledged": true
  },
  "id": <int>
}
```

**`worker.prepare_handoff`** — Superseded by `artifact.publish`. Retained as a protocol-reserved stub that returns `-32000 method_not_implemented`. See Artifact Protocol.

**Connection-loss semantics.** If the worker's connection drops, the orchestrator marks the worker `connection_lost` and gives it a grace period to reconnect (workers can reuse their token within the grace window). After the grace period, the worker process is killed. This handles transient hiccups without making lost-connection equal lost-work for short interruptions. On Kubernetes, the orchestrator additionally watches pod events from the API server so it learns about evictions promptly rather than waiting for connection timeout.

**RPC-level error codes.** These apply across all methods in addition to method-specific errors:

| Code | Name | Meaning |
|------|------|---------|
| `-32000` | `method_not_implemented` | Method is protocol-reserved but not yet implemented |
| `-32001` | `capability_denied` | Worker's manifest does not grant this method |
| `-32011` | `worker_not_found` | Worker ID from connection binding does not match a known worker |
| `-32012` | `token_expired` | One-time token already used or grace period expired |
| `-32013` | `bundle_not_active` | Worker's bundle is in a terminal state; method rejected |
| `-32600` | `invalid_request` | JSON-RPC message is not a valid Request object |
| `-32601` | `method_not_found` | Method name does not exist |
| `-32602` | `invalid_params` | Required params missing or malformed |
| `-32603` | `internal_error` | Unexpected orchestrator-side failure |

**RPC dispatcher capability check.** Every method call (both directions) is validated against the worker's manifest. For worker-to-orchestrator calls: the worker's `rpc.methods` grant must include the called method name or a wildcard pattern covering it (`"worker.*"` matches `worker.heartbeat`). For orchestrator-to-worker calls: the worker's manifest is checked at connection setup to determine which methods the orchestrator may call on this worker; a worker that does not grant `worker.pause` cannot be paused. Capability check failures return `-32001 capability_denied`.

Methods like `worker.request_human_input` are protocol-reserved in v1 even though their implementation is stubbed (they return "not implemented"). This avoids a protocol version bump when those features are actually built. `artifact.request` and `worker.prepare_handoff` were previously protocol-reserved stubs but are now superseded by the fully specified `artifact.publish`, `artifact.fetch`, and `artifact.list` methods; see Artifact Protocol.

The full RPC method dispatcher is itself capability-checked: every method call is validated against the worker's manifest before being served. This is what closes the loop on "workers can't escalate via RPC." A worker can only call methods its manifest grants, and the manifest is a subset of the bundle manifest, which the human approved.

## Capability manifest schema

The manifest is the artifact that says, for a given bundle or task, exactly what the worker is allowed to do. It is the input to the human approval flow, the input to the WorkerRunner (which translates it into bwrap flags or a Pod spec), and the reference the orchestrator's RPC dispatcher checks against on every method call. The schema is designed to be human-readable enough for review, machine-precise enough for enforcement, and composable so that task grants can be checked against bundle grants as a subset relation.

Top-level structure:

```yaml
capability_manifest:
  schema_version: "1.0"
  subject:
    kind: task | bundle
    id: <stable identifier>
  grants:
    filesystem: { ... }
    network: { ... }
    process: { ... }
    secrets: { ... }
    rpc: { ... }
    resources: { ... }
  metadata:
    rationale: "<human-written justification>"
    requested_by: <task-or-planner-id>
    expires_at: <timestamp or null>
```

Six grant categories matched to how reviewers actually think about risk and how WorkerRunner implementations dispatch enforcement.

**Filesystem grants.**

```yaml
filesystem:
  reads:
    - path: <absolute path within worker's view>
      recursive: true | false
  writes:
    - path: <absolute path within worker's view>
      recursive: true | false
      create: true | false
  working_tree:
    branch: <branch ref>
    base: <commit sha or branch ref>
    write_scope: full | path_restricted
    restricted_paths: [...]
```

Paths are within the worker's view, not the host's: a worker sees `/work/src/...`, not `/var/lib/studio/workers/abc123/src/...`. This decouples the manifest from runner-specific layout. The working tree is its own first-class category because almost every coding task touches it, and treating it specially lets the schema express "you can edit source code" cleanly without enumerating files. There is no "read everything" or "write everything" option; even the most permissive grant is bounded.

**Network grants.**

```yaml
network:
  egress:
    - destination: <hostname or CIDR>
      ports: [<port>, ...]
      protocol: tcp | udp | http | https
      rationale: "<why this is needed>"
  ingress:
    enabled: false   # default; workers do not accept inbound
  dns:
    enabled: true | false
    resolvers: [...]
```

Default-deny: an empty `egress` list means no network. Hostname-based grants are first-class (the WorkerRunner translates them to enforcement, typically via the host-side egress proxy because pure netfilter cannot do hostname matching). Per-destination rationale is required, because the human reviewer should be able to ask "why does this task need to reach api.github.com?" and answer it from the manifest alone. HTTP and HTTPS are listed as separate protocols so the schema can be extended later with method or path constraints without breaking compatibility. Ingress is disabled by default and rarely enabled, since workers connect out to the orchestrator rather than accepting inbound.

**Process grants.**

```yaml
process:
  exec:
    - binary: <absolute path>
      args_pattern: <regex or null>
      rationale: "<why>"
  spawn_subtasks:
    enabled: true | false
    max_depth: <int>
    max_count: <int>
```

Exec is an allowlist. Workers can only invoke listed binaries. `args_pattern` is optional but supported; for high-risk binaries (`git`) it can constrain args (allow `git log` and `git diff` but not `git push`), while for low-risk binaries the field can be null. `binary` requires an absolute path rather than a basename, because basename resolution would require trusting `PATH`; this has minor ergonomic cost but makes the manifest unambiguous and enforceable.

`spawn_subtasks` is the schema-level expression of bounded dynamic expansion. A task without this grant cannot request sub-task spawning. The bundle's `expansion_policy` (in the task DAG schema) governs the global budget; this section governs per-task participation.

**Secrets grants.**

```yaml
secrets:
  - name: <secret identifier>
    purpose: <enum: github_auth | llm_api | registry_auth | custom>
    delivery: env | file | rpc
    rationale: "<why>"
```

Secrets are named, never inlined; the manifest references a secret by name, and the orchestrator resolves the value at worker spawn time from its own secret store. The manifest never contains plaintext. Delivery mechanism is declared: `env` for legacy tools that read environment variables, `file` for tools that read credentials from disk, `rpc` for tools that ask the orchestrator over RPC. The `rpc` option is best for dynamic short-lived credentials (the worker calls `secrets.fetch(name)`, the orchestrator audits the fetch, and the secret only lives in worker memory for the duration of the operation). The `secrets.fetch` RPC method, its capability binding, and its audit trail are fully specified in the Artifact Protocol section. `purpose` is enumerated so a reviewer can see "this task gets a `github_auth` secret" and immediately understand the implication; `custom` is an escape hatch and should be rare.

**RPC grants.**

```yaml
rpc:
  methods:
    - <method namespace, e.g., "artifact.*">
    - <specific method, e.g., "cap.request">
  artifact_access:
    reads:
      - <artifact descriptor pattern>
    writes:
      - <artifact descriptor pattern>
```

RPC method access is itself a capability. The orchestrator's RPC dispatcher checks the worker's manifest before serving any method. Wildcards are scoped to namespace level (`artifact.*` is allowed; `*` is not), which prevents accidental over-grant. Artifact access is its own sub-category because reading and writing artifacts is the canonical inter-worker communication channel; pattern matching allows things like "read any artifact tagged `test-results-*`" without enumerating each one.

The exact artifact descriptor format is shared with the task DAG schema: `{namespace: bundle|global|task, name: <string>, version: <spec or null>, content_type: <mime-like>}`. Bundle-scoped artifacts die with the bundle. Global artifacts persist across bundles and are subject to additional capability checks. Task-scoped artifacts are task-internal.

**Resources grants.**

```yaml
resources:
  cpu_limit: <millicores>
  memory_limit: <bytes>
  disk_limit: <bytes>
  wall_time_limit: <seconds>
  llm_token_budget:
    input_tokens: <int>
    output_tokens: <int>
    by_model: { ... }
```

Resource limits are part of the manifest because they affect blast radius (a worker that runs for 24 hours has bigger blast radius than one that runs for 5 minutes) and should be reviewed alongside other capabilities. LLM token budget is a first-class resource and is enforced by the orchestrator at `llm.*` RPC time. Wall-time is enforced by the runner (a wrapping timeout locally; `activeDeadlineSeconds` on a k8s Job).

**Composition rules.** The bundle has its own manifest; tasks within it have task manifests. Three rules are enforced.

**Rule 1: Task grants must be a subset of bundle grants.** A task cannot request more than its bundle was approved for. Subset checking is per-category and algorithmic.

**Rule 2: The bundle is the human-approval unit.** Reviewers approve bundle manifests. Task manifests within an approved bundle do not need separate approval.

**Rule 3: Expansion requests carry their own manifest.** When a worker requests sub-task spawning, the request includes the proposed task manifest. If it's a subset of the bundle manifest, auto-approve. If not subset, escalate to human.

**Subset-checking algorithm, per category:**

```python
def is_subset(task_manifest: CapabilityManifest, bundle_manifest: CapabilityManifest) -> tuple[bool, str]:
    """Returns (is_subset, failure_reason)."""
    if not filesystem_is_subset(task_manifest.filesystem, bundle_manifest.filesystem):
        return (False, "filesystem grant exceeds bundle scope")
    if not network_is_subset(task_manifest.network, bundle_manifest.network):
        return (False, "network grant exceeds bundle scope")
    if not process_is_subset(task_manifest.process, bundle_manifest.process):
        return (False, "process grant exceeds bundle scope")
    if not secrets_is_subset(task_manifest.secrets, bundle_manifest.secrets):
        return (False, "secrets grant exceeds bundle scope")
    if not rpc_is_subset(task_manifest.rpc, bundle_manifest.rpc):
        return (False, "rpc grant exceeds bundle scope")
    if not resources_is_subset(task_manifest.resources, bundle_manifest.resources):
        return (False, "resources grant exceeds bundle scope")
    return (True, "")
```

**Filesystem subset rules:**
- For each entry in task `reads`: there must exist a bundle `reads` entry where `task.path` starts with `bundle.path` (or is equal), and if `task.recursive = true`, `bundle.recursive` must also be `true`.
- For each entry in task `writes`: same path-containment check as reads, using bundle `writes` entries.
- Task `working_tree.write_scope` must be `path_restricted` if bundle's is `path_restricted`; task `restricted_paths` must be a subset of bundle `restricted_paths`.

**Network subset rules:**
- For each entry in task `egress`: there must exist a bundle `egress` entry where `task.destination` is contained in `bundle.destination` (hostname exact match or CIDR containment), `task.ports` is a subset of `bundle.ports`, and `task.protocol` equals `bundle.protocol` or `bundle.protocol` is less restrictive (e.g., bundle allows `tcp`, task asks for `http`; `tcp` subsumes `http` due to protocol layering: `tcp` > `http` > `https`).
- Task `ingress.enabled` cannot be `true` if bundle `ingress.enabled` is `false`.
- Task `dns.enabled` cannot be `true` if bundle `dns.enabled` is `false`.

Protocol subsumption order: `tcp` subsumes `udp`, `http`, and `https`; `http` subsumes `https`. The rationale is transport-layer grants are broader than application-layer grants.

**Process subset rules:**
- For each entry in task `exec`: there must exist a bundle `exec` entry with the same `binary` absolute path, and task `args_pattern` (if present) must match only a subset of what bundle `args_pattern` matches. Pattern subset-ness is determined by regex intersection: if both patterns are present, the task pattern must be syntactically more restrictive (the regex compiler can statically compare character classes and quantifiers; if static comparison is inconclusive, the check fails-safe by rejecting).
- Task `spawn_subtasks.enabled` cannot be `true` if bundle `spawn_subtasks.enabled` is `false`.
- Task `max_depth <= bundle.max_depth` and `max_count <= bundle.max_count`.

**Secrets subset rules:**
- For each entry in task `secrets`: there must exist a bundle `secrets` entry with the same `name`. Task `purpose` must equal bundle `purpose` or bundle `purpose` is `custom` (which subsumes all purposes).
- Task `delivery` must be compatible with bundle `delivery`: `rpc` is the most restrictive, `env` and `file` are equivalent. A bundle granting `delivery: env` also allows task `delivery: rpc`.

**RPC subset rules:**
- For each method pattern in task `rpc.methods`: there must exist a bundle method pattern that covers it. Coverage rule: `"artifact.publish"` is covered by `"artifact.*"` or `"artifact.publish"`; `"artifact.*"` is covered by itself (not a broader wildcard, since `"*"` is disallowed at manifest validation).
- For each pattern in task `rpc.artifact_access.reads`: there must exist a bundle reads pattern that covers it. Coverage rule: all four descriptor fields are matched via the same glob algorithm specified in Artifact Protocol, Capability pattern matching; for the pattern to cover another, each field's glob must match a superset of strings. Example: `name: "test-*"` covers `name: "test-results-*"`; `name: "**"` covers everything. Same for writes.

**Resources subset rules:**
- `task.cpu_limit <= bundle.cpu_limit`
- `task.memory_limit <= bundle.memory_limit`
- `task.disk_limit <= bundle.disk_limit`
- `task.wall_time_limit <= bundle.wall_time_limit`
- `task.llm_token_budget.input_tokens <= bundle.llm_token_budget.input_tokens`
- `task.llm_token_budget.output_tokens <= bundle.llm_token_budget.output_tokens`
- For each `(model, budget)` in task `by_model`: there must exist a bundle `by_model` entry with the same model and `task_budget <= bundle_budget` for both input and output tokens.

Implementation note: the subset checker is a pure function of two typed manifest objects. It performs no I/O, issues no RPCs, and has no side effects. It is called at task dispatch time, at expansion validation time, and during DAG schema validation. A checked result may be cached per `(task_manifest_hash, bundle_manifest_hash)` pair.

The schema is purely additive: there is no way to say "allow everything except X." This keeps subset-checking trivial and the threat model clean. Common patterns like "read the whole working tree except `.env`" must be expressed by listing allowed paths rather than excluded paths.

The manifest is YAML/JSON, validated by JSON Schema, runtime-loaded into a typed model (probably pydantic). It lives alongside the bundle proposal, gets reviewed by humans, and gets stored in the audit log. Schema versioning policy (forward-compatible additions, deprecation cycles, migration rules) is deferred.

## Task DAG schema

The task DAG is what the planner produces, the human approves as part of the bundle, the DAG executor consumes, and the audit log preserves. It interlocks with the capability manifest (each task references one), with the roll-our-own DAG executor (the schema is what the executor reads), and with the star topology decision (edges in the DAG are scheduling dependencies, not data flow; data flows via capability-mediated artifact reads and writes).

The most important framing decision: **edges are scheduling, not data flow.** Many DAG systems (Airflow, Dagster) couple them: an edge A → B means "B consumes A's output." In this system, scheduling and data are separate concerns. Scheduling dependencies are explicit edges. Data dependencies are expressed via artifact reads and writes in capability manifests. They usually coincide but not always: sometimes you want a scheduling edge without data ("don't run deploy until tests pass, even though deploy doesn't read test output"); sometimes you want a data edge without scheduling ("read whatever's in `latest_config`, which may have been written long before this bundle started"). Keeping them separate makes both clearer and makes the executor's job simpler, since it only has to think about scheduling.

Top-level structure:

```yaml
task_dag:
  schema_version: "1.0"
  bundle_id: <stable identifier>
  bundle_manifest_ref: <pointer to capability manifest>

  nodes:
    - id: <stable task id within bundle>
      kind: worker | gate | aggregator
      ...

  edges:
    - from: <node id>
      to: <node id>
      condition: <edge condition spec>

  entry_nodes: [<node id>, ...]
  exit_nodes: [<node id>, ...]

  expansion_policy:
    allow_dynamic_expansion: true | false
    max_total_nodes: <int>
    max_depth: <int>

  metadata:
    created_by: <planner identifier>
    created_at: <timestamp>
    rationale: "<bundle-level justification>"
```

**Three node kinds**, deliberately rather than one. The architectural clarity of separate kinds is worth the schema complexity, and easy to revisit if it doesn't pay off.

`worker` nodes are the common case. A sandboxed worker is spawned, runs to completion, produces artifacts, exits.

```yaml
- id: implement_auth_module
  kind: worker
  task_manifest_ref: <pointer to capability manifest>
  spec:
    objective: "<natural-language description for the worker>"
    inputs:
      artifacts:
        - name: <local-name>
          ref: <artifact descriptor>
      params: { ... }
    outputs:
      artifacts:
        - name: <local-name>
          ref: <artifact descriptor>
          required: true | false
    success_criteria:
      - kind: artifact_exists | rpc_signal | exit_code
        ...
    retry_policy:
      max_attempts: <int>
      backoff: <spec>
```

`gate` nodes are decision points without a worker. The orchestrator evaluates them directly. Three flavors:

```yaml
- id: tests_passed_gate
  kind: gate
  spec:
    predicate:
      kind: artifact_property | rpc_query | human_approval
      ...
```

`artifact_property` evaluates a property of an existing artifact ("does test-results.json show all green?"). `rpc_query` queries some external state (a CI system, a service health check), subject to its own capability rules. `human_approval` pauses the DAG and waits for explicit human signoff: this is the architectural interrupt point. Gates are a separate kind because they don't need worker overhead (no sandbox), and human-approval gates in particular are where the executor's checkpointing semantics matter most.

A clarification folded in from the DAG executor design: gate nodes do carry a task manifest reference for the `artifact_property` and `rpc_query` predicate kinds, because those predicates need network or artifact-read grants to execute. For `human_approval` gates the field is optional and defaults to no grants. The earlier "no manifest" framing was a simplification; the executor needs the manifest to capability-check the predicate's actions.

`aggregator` nodes join parallel branches. Most DAG systems handle this implicitly (a node with multiple incoming edges joins them), but join semantics aren't always "wait for all," so the schema makes it explicit:

```yaml
- id: merge_test_results
  kind: aggregator
  spec:
    join: all | any | quorum | first_success
    quorum_count: <int>   # if join=quorum
    output_strategy: collect | first | reduce
    reducer: <reference to a registered reducer>
```

`join: all` is the default. `any` and `first_success` are for the "race three approaches, take whichever finishes first" pattern. `quorum` is for "spawn five workers, majority opinion wins." Making aggregators explicit is a divergence from convention but is worth it because the semantics are visible in the schema rather than buried in executor behavior, reviewers can see "this bundle uses majority voting" without reading code, and the executor implementation is simpler (incoming edges to non-aggregator nodes always have `all` semantics; aggregators are the only place where it varies). The reducer registry (named reducers for `output_strategy: reduce`, with built-in implementations for `majority_vote`, `concatenate`, `select_best_by`, and `collect_all`) is specified in the DAG executor section under Reducer registry. Custom reducers are deferred (see Deferred items).

**Edges and edge conditions.**

```yaml
edges:
  - from: <node id>
    to: <node id>
    condition:
      kind: always | on_success | on_failure | on_property
      property: <expression>
```

`always` means the edge fires regardless of source outcome (cleanup nodes). `on_success` is the default. `on_failure` is for error-handling branches. `on_property` evaluates an expression against the source node's outputs to decide whether the edge fires. The expression sublanguage is intentionally restricted: field access on source node outputs, comparison operators, boolean combinators. No function calls, no loops. Anything more complex should be a gate node, not an edge condition. The formal grammar (EBNF), evaluation semantics, and sandboxing rules are fully specified in the DAG executor section under The `on_property` expression sublanguage.

The schema deliberately has no loops. "Retry until X" is expressed via `retry_policy`, not loops; "iterate over a list" is expressed via dynamic expansion (worker spawns one sub-task per item), not loops. This is a real expressiveness limitation; workflow engines that support loops (Temporal, Step Functions) get something this system doesn't. The reasoning is that loops make static analysis dramatically harder and the human reviewer should see exactly the structure that will execute. Acceptable cost; flagged as a deliberate constraint.

**Dynamic expansion.** When a worker requests sub-task spawning, it submits a partial DAG fragment to be grafted into the running DAG:

```yaml
expansion_request:
  parent_node: <node id of requesting worker>
  graft_point: <node id where new subgraph attaches>
  fragment:
    nodes: [...]
    edges: [...]
  rationale: "<why this expansion is needed>"
```

The orchestrator validates: every node in the fragment has a task manifest that's a subset of the bundle manifest; the graft introduces no cycles and references only existing nodes; `expansion_policy.max_total_nodes` and `max_depth` are not exceeded. If checks pass and `allow_dynamic_expansion` is true, graft auto-approves. Otherwise, escalate to human.

**Validation rules** that the schema validator runs before human approval and before executor ingestion:

1. DAG-ness: no cycles, every node reachable from `entry_nodes`, every node reaches an `exit_node`.
2. Reference integrity: every node id referenced by an edge exists; every manifest reference resolves; every artifact reference is well-formed.
3. Manifest subset: every task manifest is a subset of the bundle manifest.
4. Artifact flow: every artifact a task reads is either external (provided as bundle input) or written by some predecessor in the DAG. No reads from the future.
5. Aggregator placement and edge homogeneity: aggregator nodes have ≥2 incoming edges. Non-aggregator nodes' incoming edges must be homogeneous, either all in {`on_success`, `on_property`} or all in {`on_failure`, `always`}. Mixing success-conditional and failure-conditional edges into a non-aggregator node is an error, because the join semantics would be ambiguous about "predecessor failed but I had an `on_success` edge from them." Success-set non-aggregator nodes become ready when all predecessor edges fire (all predecessors completed with satisfying property); if any predecessor skips or fails, the node is skipped. Failure-set non-aggregator nodes become ready when at least one predecessor edge fires. This refinement of the original "all `on_success` or all `always`" rule was made during the DAG executor design and folded back into schema validation here.
6. Expansion policy coherence: if `allow_dynamic_expansion` is false, no node has the `spawn_subtasks` capability granted.

Rule 6 is a nice cross-validation: the DAG schema and the capability manifest constrain each other.

**Storage and identity.** A bundle's full state is bundle manifest plus task DAG plus bundle metadata, stored as YAML in the audit log, loaded into a typed runtime representation by the orchestrator. The bundle id is content-addressed (hash of the canonical-serialized bundle), giving free deduplication of identical bundles, tamper-evidence, and straightforward audit references. When dynamic expansion happens, the bundle id stays the same (it identifies the planning-time bundle); each expansion produces a separate content-addressed expansion record. The audit log captures both the original bundle and the sequence of expansions, in order.

DAG visualization rendering rules (mermaid output, including how gates, aggregators, capability annotations, and grafted nodes are rendered) are specified in the DAG executor section.

## DAG executor

The executor is the component that reads an approved task DAG, drives it to terminal state by spawning workers, evaluating gates, and running aggregators, and writes the outcome back to the bundle. It sits between the bundle state machine (which decides whether to enter execution) and the WorkerRunner (which knows how to spawn an isolated process with a capability set). Everything interesting about scheduling, dependency tracking, failure propagation, and checkpointing lives here.

### Design framing

Two framing decisions from earlier sections constrain the executor's shape more than any others and are worth restating in this context.

**Edges are scheduling, not data flow.** The executor is a scheduling machine. It decides when nodes become eligible to run, based on the completion state of their predecessors and the conditions on their incoming edges. It does not route data between nodes. Data flows through the artifact layer: a worker node declares the artifacts it reads (in its task manifest) and writes (in its DAG node spec); the executor ensures that scheduling edges mean a predecessor has had the chance to publish before a successor tries to fetch, but it doesn't plumb outputs to inputs. This keeps the executor narrow and makes scheduling correctness independent of artifact correctness.

**Checkpointing is node-boundary, and the checkpoint is already in SQLite.** The pattern borrowed from LangGraph is that state is persisted at node boundaries and resumes pick up from the last persisted boundary. In a system with a separate checkpointer and an in-memory graph state, that's a nontrivial engineering concern. In this system, every node state transition is already a SQLite transaction, the DAG structure and node states are already tables, and there is no separate in-memory "graph state" distinct from what SQLite holds. The checkpoint *is* the last committed transaction. Resume is "re-read SQLite and reconcile with reality." This collapses most of the checkpointing design into a schema design and a reconciliation protocol.

A third framing decision, new in the executor design: **the executor is event-driven at the edges and synchronous at the core.** Worker completions, RPC replies, capability decisions, and cancellation requests arrive as events; the executor enqueues them into a single work queue that is drained by a single async task. Each drain tick takes one event, updates SQLite in one transaction, computes the new ready set, and spawns whatever is newly eligible. Serializing the tick eliminates most of the concurrency hazards that a multi-reader-multi-writer scheduler would introduce, at the cost of some latency that is vanishingly small compared to worker runtime. This is the orchestrator's analogue of a game-engine tick: one mutator, many observers.

### Executor topology

The executor is a module inside the orchestrator core process, not a separate process. It has no network surface of its own; it's invoked from the bundle state machine and it invokes the WorkerRunner, RPC dispatcher, and capability enforcer. The orchestrator's existing async event loop hosts it.

Logically it decomposes into five cooperating pieces:

The **scheduler** computes the ready set from the current DAG state. On every tick, it identifies nodes whose predecessor-condition is satisfied and whose concurrency budget is available, and emits spawn requests.

The **node executor** dispatches a ready node to the right backend: worker nodes to the WorkerRunner, gate nodes to the gate evaluator, aggregator nodes to the reducer registry. It is responsible for transitioning the node's state in SQLite and handling the backend's response.

The **event pump** is the single async task that drains the event queue. It deserializes the event, applies it to SQLite in a transaction, asks the scheduler for the newly-ready set, and invokes the node executor for each ready node. It is the only mutator of executor state.

The **graft handler** processes dynamic expansion requests. It validates the requested fragment against the bundle manifest and DAG structural constraints, applies the graft in a SQLite transaction, and emits a tick to the event pump so the newly-added nodes are considered for scheduling.

The **reconciler** runs only on orchestrator startup. It reads SQLite, compares to the (empty) live state, and performs the kill-all-workers-then-resume-DAG sequence described below.

These pieces share the orchestrator's SQLite connection pool. They do not share in-memory mutable state outside of the event queue; anything persistent is in SQLite.

### Node lifecycle

Each DAG node progresses through a state machine. The state machine is uniform across node kinds at the top level (the same set of states applies), but the transitions and side-effects differ by kind.

The states:

`pending` means the node exists in the DAG but its predecessor condition is not yet satisfied. `ready` means predecessors are satisfied and the node is eligible to run, but the scheduler hasn't dispatched it yet (typically because concurrency is full). `running` means the node has been dispatched to its backend. `completed` means it terminated successfully and its outputs (if any) are visible to downstream edges. `failed` means it terminated unsuccessfully; whether this propagates downstream depends on outgoing edge conditions. `skipped` means the node will not run; a node is skipped when every incoming edge's condition has become unreachable (all predecessors failed and the node has no `on_failure` or `always` edges) or when an ancestor was skipped with the same consequence. Skipped is distinct from failed so downstream `on_failure` edges don't fire spuriously. `cancelled` means the node was explicitly terminated before natural completion, typically because the bundle was aborted or because aggregator siblings made its completion unnecessary (for `first_success` or satisfied `quorum` cases).

The `pending → ready` transition is deterministic: it fires as soon as all incoming edges' conditions evaluate to a terminal answer (fired or not-fired) and at least one has fired. A node with no incoming edges (an entry node) is born `ready`.

The `ready → running` transition is scheduled. It is gated on the bundle's per-kind concurrency budget: workers are capped per bundle and globally; gates and aggregators are not. When dispatched, the node's `running_at` timestamp is written and a kind-specific side-effect begins. For worker nodes, the WorkerRunner spawns the isolated process, the capability manifest is materialized into bwrap flags or a Pod spec, the worker connects back via RPC, and the task spec is delivered. For gate nodes, the gate evaluator fires the predicate; for `artifact_property` and similar synchronous predicates, the node may transition to `completed` or `failed` in the same tick; for `rpc_query`, the query is issued and the node waits on the reply event; for `human_approval`, the node posts an approval request to the approval surface and waits for a decision event. For aggregator nodes, the reducer registry resolves the named reducer (or applies the built-in join semantics), collects the outputs of the ready predecessors, and produces the aggregator's own output; this is synchronous for built-in joins, and custom reducers are not supported in v1.1 (see Reducer registry).

The `running → completed` and `running → failed` transitions are triggered by events from the backend. For worker nodes, the event is the worker's `worker.final_report` RPC call (success or failure depending on the reported outcome) or a runner-side signal (process exited, timeout exceeded, cancellation complete). For gate nodes, the event is the predicate's resolution. For aggregator nodes, the event is the reducer's completion.

The `running → cancelled` transition is triggered by an explicit cancel event, either from bundle-level Abort or from aggregator sibling cancellation. Cancel sends the worker a `worker.cancel` RPC with a grace period, then SIGTERM, then SIGKILL. Gates and aggregators, being in-process, simply drop pending work.

Every state transition writes an `audit_log` entry and a `node_state_history` row (see schema below). Transitions are atomic within a SQLite transaction that also updates the dependent state (artifact publication records, heartbeat reset, etc.).

### Edge semantics

An edge has a `from` node, a `to` node, and a condition. The condition determines whether the edge *fires* when the source node reaches a terminal state. Firing an edge is the act that can make the destination node eligible to transition from `pending` to `ready`.

`always` fires whenever the source reaches any terminal state (`completed`, `failed`, or `skipped`). It is the right condition for cleanup steps and unconditional handoffs. `on_success` fires only when the source reaches `completed`; it is the default for newly-drawn edges and the vast majority of dependency relationships. `on_failure` fires only when the source reaches `failed`; it is the condition for error-handling branches like a retry node, a notification node, a rollback node, or a human-input escape hatch. `on_property` fires when the source reaches `completed` *and* an expression over the source's output evaluates to true.

When the source transitions to `skipped` or `cancelled`, no edge with an `on_success`, `on_failure`, or `on_property` condition fires; only `always` edges fire. This is the mechanism by which skipped-ness propagates: a node whose only incoming edges are `on_success` or `on_failure` and whose predecessors all skipped is itself marked skipped.

The `pending → ready` eligibility rule is: a node becomes ready when at least one incoming edge has fired *and* no incoming edge is still in indeterminate state (predecessor is `pending`, `ready`, or `running`). For aggregator nodes, the rule is modified by the aggregator's join semantics (see below); for non-aggregator nodes, it is the homogeneity-based rule from the schema validation refinement.

Edges are evaluated lazily: the `pending → ready` check is performed only on the tick that sees a predecessor transition, not on every tick for every node. Ready-set maintenance is incremental.

### Ready-set scheduling

The scheduler selects from the ready set on every tick, subject to two concurrency budgets.

The **global worker semaphore** caps total concurrent workers across all in-flight bundles. In v1.1 this is 4 (sized to the 30 GB box). It is enforced by the orchestrator, not the executor; the executor simply asks the orchestrator to dispatch a worker and may have the request queued if the semaphore is full. Queued-but-not-yet-dispatched worker nodes remain in `ready` state.

The **per-bundle concurrency budget** caps concurrent workers within a single bundle. In v1.1 this is not separately configured (the global budget is the only cap), but the executor tracks it as a distinct concept to make per-bundle fairness straightforward to add later. [PROVISIONAL] The default formula is `max(2, global_budget // active_bundles)`, reconsidered in v1.2. An implementing agent should hardcode this formula and not treat it as a ratified constraint.

Gate and aggregator nodes do not consume the worker budget. They run in-process in the orchestrator and their cost is bounded by reducer and predicate execution, which v1.1 keeps cheap. If in-process aggregators ever become expensive (a worker-spawning reducer was considered and rejected, but a future variant might re-raise the question), they would consume budget the same way worker nodes do, because they would effectively become worker nodes.

Scheduling policy within the ready set is FIFO by `ready_at` timestamp for v1.1. There is no priority, no critical-path optimization, no SJF. Reasoning: bundles are small, DAGs are small (hundreds of nodes at most), and scheduling latency is negligible compared to node runtime. A more sophisticated policy is trivial to drop in later because the scheduler's input is just the ordered ready-set table.

Starvation is not possible with FIFO plus no priority. A node in the ready set will eventually dispatch as long as the global semaphore has any throughput at all. An Aborted bundle cancels its nodes explicitly, so they leave the ready set; they do not wait.

### Gate node mechanics

Three predicate kinds, each with concrete execution semantics.

**`artifact_property`** evaluates a boolean expression over an artifact's properties. The executor fetches the artifact via the artifact layer (capability-checked through the gate node's task manifest), parses the expression against the artifact's declared schema, and returns true or false. The expression sublanguage is the same one used for `on_property` edges; see below.

**`rpc_query`** issues an RPC to a service whose endpoint and method are declared in the gate spec. The RPC requires its own capability grant in the gate's task manifest. The RPC returns a boolean. Timeout defaults to 30 seconds [PROVISIONAL: chosen as a reasonable ceiling for internal service calls; should be tuned against observed p95 latency of target services] and is configurable per gate. Transient RPC failures are currently not distinguished from predicate-false responses (both produce gate failure, subject to retry policy); a future refinement to classify error types is flagged.

**`human_approval`** is the architectural interrupt point. On dispatch, the executor creates an approval request in the `approval_requests` table and surfaces it through all three review surfaces (MCP tool output, GitHub Issues comment, CLI listing). The gate transitions to `running` and stays there until a decision event arrives. An `approve` decision completes the gate. A `reject` decision fails it. A `modify` decision on a gate is not supported; `modify` applies to bundle proposals. Mid-flight modification is the job of `pause → redirect → resume`, not of gate decision semantics.

Gate nodes are the only nodes that can legitimately sit in `running` state for long wall-clock periods without consuming worker budget or emitting heartbeats. The stalled-bundle detector (8 hours, from v1) is computed over a bundle's whole `in_progress` duration, not per-node, so a long human-approval gate correctly triggers the `acting-soon` label rather than stall detection. A long `rpc_query` gate is suspicious and surfaces through a separate `gate_wait_time_excess` signal when it exceeds 3× the gate's declared timeout.

### Aggregator mechanics

Four join modes from the schema, with concrete semantics:

**`all`** requires every incoming edge to have fired (not necessarily with success; skipped predecessors don't fire an `on_success` edge and so delay the aggregator until the skip propagates through the other edges too). This is the default and is what a non-aggregator node also uses implicitly.

**`any`** fires as soon as any incoming edge has fired. Remaining predecessors continue to run; their outputs are collected if they complete before the aggregator's output is produced, or discarded. Workers that continue running after an `any` aggregator has fired are a source of waste, which is why `first_success` exists as a separate mode.

**`first_success`** fires as soon as any incoming edge of kind `on_success` has fired. On firing, the executor cancels all still-running sibling predecessors via the cancellation protocol below. This is the intended mode for "race N approaches, take whichever finishes first" patterns, where the value is in not paying for the slower approaches once a fast one succeeds.

**`quorum`** fires when the number of fired incoming edges meets or exceeds `quorum_count`. On firing, still-running siblings can optionally be cancelled depending on a `cancel_remaining_on_quorum` flag (default `true`). The `any` and `first_success` modes are degenerate cases of quorum with `quorum_count = 1`, retained as separate modes because the common case is worth a readable name.

Output strategy is orthogonal to join. **`collect`** returns the list of all fired predecessors' outputs in DAG-order. **`first`** returns the first-fired predecessor's output, paired with `any` or `first_success`. **`reduce`** invokes a named reducer from the reducer registry with the fired predecessors' outputs.

Not all combinations are legal: `(all, first)` would be ambiguous about which output to take and is rejected by schema validation. `(any, collect)` is legal and returns a list that may grow after the aggregator fires; the executor snapshots at fire time and ignores later arrivals.

**Aggregator cancellation protocol.** When an aggregator transitions from `pending` to `ready` via `first_success` or via a quorum-with-cancel-remaining satisfied condition, the executor identifies still-running sibling predecessors and cancels them. The protocol enumerates the aggregator's incoming edges whose sources are in state `ready` or `running`, then issues cancellation events. For worker nodes this means `worker.cancel` RPC with a 30-second grace period [PROVISIONAL], then SIGTERM, then SIGKILL after another 10 seconds [PROVISIONAL]. Both durations need validation against observed worker step-completion times. For gate nodes with `rpc_query`, the RPC is abandoned and the node transitioned directly to `cancelled`. For gate nodes with `human_approval` still pending, the approval request is withdrawn (marked `expired`) and the node transitioned to `cancelled`.

The grace period exists so that a worker that has nearly finished can commit its work before being killed; the work may still be useful even if the aggregator no longer needs it (for memory, for calibration data, for audit). Workers that exit cleanly during the grace period transition to `completed`, not `cancelled`, and their outputs are captured in the audit trail. The aggregator has already fired and does not re-evaluate based on the late completion.

Cancelled nodes transition to `cancelled`, not `failed`. Downstream edges with `on_failure` conditions do not fire on cancellation, only on failure. This is the right default because cancellation is an executor decision, not a judgment on the work; a downstream failure handler should not run just because the executor decided the sibling was no longer needed. Downstream `always` edges do fire on cancellation.

### The `on_property` expression sublanguage

The expression sublanguage for `on_property` edge conditions and for gate `artifact_property` predicates is deliberately restricted. It supports field access on source node outputs, comparison operators, and boolean combinators. No function calls, no loops, no assignment, no ternary, no lambda, no arithmetic beyond what comparisons need implicitly. The rationale is that `on_property` is a scheduling predicate, not a general-purpose expression evaluator; anything that wants general computation should be a gate node invoking a reducer or a worker.

Grammar in EBNF:

```ebnf
expression    = or_expr ;
or_expr       = and_expr , { "||" , and_expr } ;
and_expr      = not_expr , { "&&" , not_expr } ;
not_expr      = [ "!" ] , compare ;
compare       = primary , [ compare_op , primary ] ;
compare_op    = "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "matches" ;
primary       = literal | path | "(" , expression , ")" ;
path          = identifier , { path_step } ;
path_step     = "." , identifier | "[" , string_literal , "]" ;
identifier    = letter , { letter | digit | "_" } ;
literal       = number | string_literal | bool | null | list ;
bool          = "true" | "false" ;
null          = "null" ;
list          = "[" , [ literal , { "," , literal } ] , "]" ;
string_literal = '"' , { char } , '"' ;
number        = integer | float ;
```

Semantics. The root context is the source node's output, a JSON-ish structure with the shape `{outputs: {...}, artifacts: [...], exit_code: int, report: {...}}`. Path expressions like `outputs.tests_passed` or `report["coverage"]` resolve against this context. `==` and `!=` compare values of the same type; comparing mismatched types is a type error and the expression evaluates to false with a logged warning. `<`, `<=`, `>`, `>=` apply to numbers only; mismatches are type errors. `in` tests membership: `"integration" in outputs.test_suites_run`. `matches` tests a regex: `report.error_message matches "timeout.*"`. `&&`, `||`, `!` are standard short-circuit boolean combinators.

Evaluation is sandboxed: no I/O, no clock access, no randomness, no reference to state outside the source node's output. Evaluation cost is bounded (the source output is already in memory and the expression is parsed once), and evaluation errors fail-closed (the edge does not fire) with a warning logged.

Implementation uses a small recursive-descent parser producing an AST and a walker that evaluates the AST against the context. Parser is roughly 150 lines of Python; evaluator is roughly 100. No third-party expression-language dependency, because the grammar is smaller than the dependency footprint of any general expression library and a tight custom implementation is much easier to audit for sandbox escape than a general library where the author is defending against many use cases.

### Reducer registry

Aggregator nodes with `output_strategy: reduce` reference a named reducer. The registry is a static Python module in the orchestrator: `orchestrator.executor.reducers`. Reducers are plain functions with a fixed signature, registered by decorator.

v1.1 ships with the following named reducers:

**`majority_vote`** takes a list of node outputs, extracts a designated field (default `outputs.answer`), and returns the modal value. Ties resolved by first-arrival order. Used with `quorum` join for the "spawn five, majority wins" pattern.

**`concatenate`** takes a list of node outputs, extracts a designated field (default `outputs.content`), and returns the concatenation. Requires all extracted values to be strings or lists.

**`select_best_by`** takes a list of node outputs, extracts a designated numeric field, and returns the node output with the maximum (or minimum) value of that field. Paired with judge-score patterns.

**`collect_all`** returns the raw list of outputs. Equivalent to `output_strategy: collect`; included in the reducer registry so it can be referenced uniformly.

Reducers receive the list of outputs and a parameter dict declared in the aggregator's node spec. They do not have capability context, do not issue RPCs, do not touch the artifact layer directly; they operate on data already materialized into the executor by the aggregator's collection step.

Custom reducers (bundle-supplied code) are out of scope for v1.1. If a bundle needs reduction logic beyond the built-in set, the right answer is to make the aggregator's output a placeholder and add a successor worker node with the custom logic; this gives the logic proper capability bounds, proper auditability, and proper timeouts. A "worker-spawning reducer" abstraction was considered and rejected because it collapses two clean concepts (aggregator as join, worker as task) into one muddy one.

### Executor state schema

The executor adds the following tables to the SQLite schema specified in the architecture section:

```sql
CREATE TABLE dag_nodes (
  id TEXT PRIMARY KEY,              -- bundle_id + node_id, colon-joined
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  node_id TEXT NOT NULL,            -- stable id within bundle
  kind TEXT NOT NULL,               -- worker|gate|aggregator
  spec_json TEXT NOT NULL,          -- the node spec from the DAG
  task_manifest_id TEXT,            -- references capabilities if kind=worker or gate
  state TEXT NOT NULL,              -- pending|ready|running|completed|failed|skipped|cancelled
  worker_id TEXT REFERENCES workers(id),
  ready_at INTEGER,
  started_at INTEGER,
  ended_at INTEGER,
  output_json TEXT,                 -- node output, available once completed
  failure_reason TEXT,
  UNIQUE(bundle_id, node_id)
);

CREATE INDEX idx_dag_nodes_bundle_state ON dag_nodes(bundle_id, state);

CREATE TABLE dag_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  condition_kind TEXT NOT NULL,     -- always|on_success|on_failure|on_property
  condition_expr TEXT,              -- if kind=on_property
  fired INTEGER DEFAULT 0,          -- 0|1
  fired_at INTEGER,
  UNIQUE(bundle_id, from_node_id, to_node_id)
);

CREATE INDEX idx_dag_edges_to ON dag_edges(bundle_id, to_node_id);
CREATE INDEX idx_dag_edges_from ON dag_edges(bundle_id, from_node_id);

CREATE TABLE node_state_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL REFERENCES dag_nodes(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  at INTEGER NOT NULL,
  reason TEXT,
  event_id INTEGER                  -- pointer to the triggering event in audit_log
);

CREATE TABLE dag_expansions (
  id TEXT PRIMARY KEY,              -- ULID
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  parent_node_id TEXT NOT NULL,
  graft_point_node_id TEXT NOT NULL,
  fragment_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL,              -- pending|approved|rejected|applied|failed
  requested_at INTEGER NOT NULL,
  decided_at INTEGER,
  decided_by TEXT,
  applied_at INTEGER
);

CREATE TABLE approval_requests (
  id TEXT PRIMARY KEY,              -- ULID
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  kind TEXT NOT NULL,               -- gate_human_approval|expansion|capability_grant|bundle
  subject_id TEXT NOT NULL,         -- node_id, expansion_id, capability_request_id, bundle_id
  context_json TEXT NOT NULL,       -- what the reviewer sees
  state TEXT NOT NULL,              -- pending|decided|expired
  decision TEXT,                    -- approve|reject|modify
  decided_at INTEGER,
  decided_by TEXT,
  decided_surface TEXT,             -- mcp|github_issue|cli
  created_at INTEGER NOT NULL
);

CREATE TABLE artifact_refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id TEXT NOT NULL REFERENCES bundles(id),
  producer_node_id TEXT NOT NULL REFERENCES dag_nodes(id),
  descriptor_json TEXT NOT NULL,    -- {namespace, name, version, content_type}
  published_at INTEGER NOT NULL
);

CREATE INDEX idx_artifact_refs_descriptor ON artifact_refs(bundle_id, descriptor_json);
```

The `dag_nodes` and `dag_edges` tables hold the planning-time DAG plus every grafted expansion, merged into a single table per bundle. Distinguishing planned-vs-grafted nodes is done via the `dag_expansions` table, which holds the provenance; the nodes themselves don't carry a flag because the executor shouldn't treat them differently at runtime.

The `node_state_history` table is append-only and indexed; it is the primary source for executor-level forensics. The top-level `audit_log` table still receives high-level events (bundle started, bundle completed, expansion granted), but per-node state transitions go to `node_state_history` to keep `audit_log` scanable.

The `approval_requests` table generalizes over gate human-approval, dynamic expansion approval, and capability-grant approval, all of which have the same lifecycle: created, surfaced, decided. Bundle-level approval is also written here for uniformity, though the bundle state machine reads it through a different path.

The `artifact_refs` table is the executor's view of the artifact layer. The artifact layer owns the storage and content addressing; `artifact_refs` is the join table letting the executor answer "has any predecessor published this artifact yet?" without a round-trip to the artifact store. This is the minimum interface the executor needs from the artifact layer; the full artifact protocol is specified in the Artifact Protocol section.

### Checkpointing and crash recovery

Because every node transition is a SQLite transaction, the effective checkpoint boundary is every commit. There is no separate checkpoint mechanism. A crash mid-tick loses no state that was committed; everything after the last commit is re-derived on restart.

Crash recovery proceeds as follows, performed by the reconciler on orchestrator startup, before the HTTP webhook endpoint and the MCP socket open:

1. **Kill-all workers.** The `workers` table is scanned for rows in state `running`, `paused`, or `pending-start`. Each is marked `failed` with `exit_reason = 'orchestrator_crash'`. This is unchanged from the architecture-section description.
2. **Reconcile node states to worker states.** For each `dag_nodes` row in state `running`, find its `worker_id`. If the worker is now `failed` (from step 1), transition the node to `failed` with `failure_reason = 'worker_killed_on_crash'`. If the worker was a gate whose `rpc_query` was in flight, transition the node to `failed` with `failure_reason = 'rpc_in_flight_on_crash'`. If the node was a gate with a pending human approval, leave it in `running`: the approval request is still live in `approval_requests`, and a reviewer decision will complete it normally.
3. **Apply retry policies.** For each node transitioned to `failed` in step 2, evaluate its `retry_policy`. If retries remain, transition to `pending`, reset the fired state of incoming edges that were firing on completion (none, since the node didn't complete), and let the scheduler re-consider it. If no retries remain, let the failure propagate via outgoing `on_failure` edges.
4. **Replay unread approval decisions.** For each `approval_requests` row in state `pending`, check the secondary surfaces (GitHub Issues comments, MCP-side decision log) for decisions posted while the orchestrator was down. Apply any found.
5. **Re-trigger bundle-level reconciliation.** Bundles in state `verifying` re-trigger verification, as specified earlier. Bundles in state `in_progress` are re-ticked: the scheduler computes the ready set from current node states and dispatches as usual.
6. **Open surfaces.** Webhook endpoint and MCP socket accept traffic.

This reconciliation is idempotent: running it twice on the same SQLite state produces the same result, because the transitions it makes are from states that wouldn't have existed had the reconciler already run. A crash during reconciliation is safe to recover from by re-running reconciliation.

One subtle case: a node that was `running` on a worker that was successfully completing its `worker.final_report` RPC at the moment of the crash. The RPC's request may have been received and processed (node transitioned to `completed`) but the worker process was killed before it could cleanly exit. On restart, the worker appears `running` in the workers table but the node is already `completed`. Step 1 marks the worker `failed`, which is a lie (the worker actually succeeded), but the node is already in the correct state. The worker-level lie is tolerable: it's noise in the audit trail but causes no incorrect downstream behavior. An alternative (mark the worker `completed` if its node is `completed`) has the reverse failure mode: if the node transition didn't commit, we'd mark a dead worker successful. The current rule fails in the direction of pessimism, which is the right default for a recovery routine.

### Dynamic expansion mechanics

Dynamic expansion is how workers request sub-task spawning mid-execution. The Task DAG schema defines the request format and the validation rules that must pass for auto-approval. This section specifies the mechanics.

A worker issues a dedicated `expansion.request` RPC method (or `cap.request` for the manifest portion) with a fragment (nodes plus edges plus rationale). The graft handler receives the request and proceeds:

1. **Parse and validate structurally.** Fragment must be a well-formed partial DAG; every node has an id unique within the bundle (not already in `dag_nodes`); every edge references either a node in the fragment or an existing node in `dag_nodes`; no cycles are introduced by grafting at `graft_point`. Cycle check runs against the merged DAG, not against the pre-graft DAG. The check is a topological sort, O(V+E), fast at the scales v1.1 sees.
2. **Validate per the schema rules.** Every task manifest in the fragment is a subset of the bundle manifest. `expansion_policy.max_total_nodes` is not exceeded by the merged DAG. `expansion_policy.max_depth` is not exceeded (depth measured from any entry node to the deepest fragment node).
3. **Decide auto-approve or escalate.** If `allow_dynamic_expansion` is true and every fragment manifest is a subset of the bundle manifest, auto-approve. Otherwise, create an `approval_requests` row with kind `expansion` and surface to the reviewer; the graft waits on decision.
4. **Apply.** In a single SQLite transaction: insert the fragment's nodes into `dag_nodes` with state `pending`; insert the fragment's edges into `dag_edges`; mark the expansion record `applied`; write `audit_log` entries for the expansion; for each grafted node whose inputs declare artifacts produced by already-running predecessors, increment those artifacts' `ref_count` in `artifact_metadata` (this mirrors the ref_count increment done at normal node dispatch, specified in the Artifact Protocol section). Commit.
5. **Tick the scheduler.** The newly-added nodes are considered for readiness in the next tick.

The transaction-boundary choice is that the graft is all-or-nothing. Partial grafts (some nodes inserted, some not) are never observed. If the transaction fails (uniqueness violation caught mid-insert, etc.), the expansion record stays in `pending` and the worker's RPC returns a failure that it can surface or retry.

A subtle case: what if the worker that requested the expansion has already completed or failed by the time the human decides on the expansion? The design is that the expansion stands alone: it's a modification to the DAG, not a dependent action on the requester. If the requester has completed, the grafted subgraph attaches normally at `graft_point` and runs when its predecessors are satisfied. If the requester has failed, the grafted subgraph may or may not be what was wanted anymore; the reviewer sees the requester's failure in the approval context and decides. This is consistent with the orchestrator-as-spawn-authority property: the request is a proposal, not a direct worker action.

### Failure handling and retry policy

A node's `retry_policy` in the DAG schema is `{max_attempts, backoff}`. The executor's retry evaluator runs on every `running → failed` transition.

If the node has retries remaining, the executor transitions the node to `pending`, records the retry in `node_state_history`, and schedules re-eligibility after the backoff delay. The node's incoming edges are not re-fired (they already fired when the predecessors completed, and they remain fired); only the node itself re-enters the lifecycle. For worker nodes, retry means a fresh WorkerRunner spawn; the worktree is reset to the predecessor state (discarding any partial commits the previous attempt made) before the new worker starts. For gate nodes, retry re-invokes the predicate. Aggregator retry is generally not meaningful and `retry_policy` on aggregator nodes is rejected at schema validation.

Backoff is one of: `immediate`, `fixed(seconds)`, `exponential(base, factor, max)`. No jitter by default, because the single-box deployment doesn't benefit from it; jitter can be opted in per node.

If the node has no retries remaining, the failure is terminal for that node. Outgoing `on_failure` edges fire. Outgoing `on_success` and `on_property` edges do not fire. The node's state becomes `failed`.

Bundle-level failure is derived from node states: a bundle is `failed` when any exit node is `failed` and no other path to an exit node remains unexplored, or when every exit node is in a terminal state and at least one is `failed`. This rule handles the common case (single exit node fails, bundle fails) and the multi-exit case (some exits succeed, some fail: bundle still fails because the user asked for all exits to succeed). A more permissive rule (bundle succeeds if any exit succeeds) is conceivable but has no obvious use case in this system and is not supported.

When a bundle is marked `failed` due to node failure, the executor's in-flight nodes in the same bundle are not automatically cancelled. The rationale is that in-flight work may still produce useful artifacts for a manual recovery attempt, and the reviewer may want to see what partial state the bundle reached. Cancellation requires an explicit Abort from the reviewer. A future refinement (soft-abort on fatal failure) is in deferred items.

### Mermaid rendering

The executor emits a mermaid rendering of a bundle's DAG on demand for inclusion in RFCs, GitHub Issue comments, and MCP resources. The rendering rules:

Worker nodes render as rectangles. Gate nodes as diamonds. Aggregator nodes as hexagons. Node colors vary by state: `pending` is white; `ready` light yellow; `running` light blue; `completed` light green; `failed` light red; `skipped` grey; `cancelled` grey with dashed border (mermaid supports this via `stroke-dasharray`).

Edge rendering varies by condition: `on_success` is a solid arrow (the default); `on_failure` is a dashed arrow with red color; `always` is a thick solid arrow; `on_property` is a solid arrow with a condition label on the edge.

Worker nodes carry a subtitle showing a compressed summary of the task manifest, e.g., `[fs:rw:/src, net:api.github.com, exec:git,pytest]`. This helps reviewers spot unusual grants at a glance. The full manifest remains elsewhere; the annotation is a pointer.

Grafted nodes are drawn with a doubled border to distinguish them from planning-time nodes. The `dag_expansions.rationale` appears as a mermaid subgraph label containing the grafted nodes. Entry nodes are preceded by a filled circle; exit nodes are followed by a ringed circle, per convention.

The mermaid is generated by a pure function of the DAG state; no side effects, no I/O. It is cheap enough to re-render on every MCP resource fetch and every GitHub comment update.

### Testing strategy

The executor must be testable without live LLM workers. Three layers.

A **MockWorkerRunner** implements the WorkerRunner interface, accepts task specs, and returns scripted outcomes per test scenario. The mock supports success, failure, timeout, crash-mid-run, slow-heartbeat, and RPC-injection test cases. It also supports explicit step-through mode for interactive debugging of scheduler behavior.

**Property-based tests on the scheduler.** Given a random well-formed DAG and a random sequence of backend events (completions, failures, cancellations), the scheduler should always reach a terminal state where every node is in a terminal state (`completed`, `failed`, `skipped`, `cancelled`), no two nodes' states contradict (e.g., a `skipped` node with a `completed` successor via `on_success`), and the total number of nodes dispatched is within the concurrency bound at every moment. These invariants are cheap to check and catch whole classes of bugs.

**Replay tests.** The `node_state_history` table is a trace of an execution. Given a trace, a test can assert that the observed transitions are consistent with the DAG and the event order. Real bundle traces from dev.learhy.net can be replayed against the executor code to verify that refactors don't change behavior.

**Migration tests for schema changes.** When `dag_nodes` or `dag_edges` schemas evolve, migrations are tested by loading a pre-migration database, running the migration, and asserting post-migration invariants hold.

No real Ollama Cloud calls in tests. The test suite runs on CI without network access.

## Artifact Protocol

The artifact protocol is the system that stores, addresses, retrieves, and garbage-collects the data workers produce and consume. It sits between the DAG executor (which needs to know when an artifact becomes available so it can unblock successors) and the persistence layer (which owns SQLite for hot state and `memory/` for durable bytes). It also absorbs the secrets fetch protocol, because secrets are a degenerate kind of artifact: named, capability-checked, RPC-fetched, never persisted in worker state. Every part of v1.1 that references artifact descriptors, the `rpc.artifact_access` manifest fields, inter-worker data flow, or the `artifact_refs` join table is a client of the protocol specified here.

Scope includes the three artifact RPC methods (`publish`, `fetch`, `list`), the content-addressing scheme, the storage layer and its substrate-agnostic interface, the artifact metadata schema, the lifecycle and garbage collection policy, the `secrets.fetch` RPC, capability pattern matching at fetch time, crash recovery for in-flight publishes, and integration with the DAG executor's notification channel and validation rules. Scope explicitly does not include distributed artifact stores (multi-orchestrator HA is a separate deferred item), cross-bundle artifact sharing semantics, external artifact registries, streaming for very large artifacts (deferred, with explicit gating criteria), or multi-tenant concerns.

### Design framing

Five load-bearing decisions shape the artifact protocol, in addition to the constraints imported from earlier sections.

The star topology (from Bundle lifecycle: execution and integration) means all artifact movement is mediated by the orchestrator. Workers do not ship bytes to each other. Per-worker filesystem isolation (same section) means workers do not share disk; artifacts are the canonical inter-worker data channel. Capability-mediated access (from Capability manifest schema) means every read and write is checked against the worker's manifest before the orchestrator serves it. The orchestrator is the trust root (from Threat model and trust assumptions); workers are not trusted, and integrity comes from orchestrator mediation, not from workers cooperating. The executor's `artifact_refs` table (from DAG executor) is the join table the executor queries to answer "has a predecessor published this artifact?" and it is not redesigned here.

The new decisions:

**The artifact layer is a module inside the orchestrator core process, not a separate service.** It exposes an `ArtifactStore` interface, analogous to `WorkerRunner`, so the storage substrate is swappable without touching manifests, RPC, or executor logic. The rationale is that artifact operations are capability-checked (they share the dispatcher), they share the orchestrator's SQLite connection for metadata, and they emit events into the executor's event pump. Splitting the artifact layer into its own process would turn all three of those couplings into network calls, which is complexity without benefit at v1.1 scale. The interface boundary, not the process boundary, is what makes the k8s migration additive.

**The descriptor is the user-facing handle; the content hash is an internal implementation detail.** The hash is not a field in the descriptor. Descriptors are declared by planners and workers before bytes exist (a worker declares what it will produce in its DAG node spec). The hash is assigned by the orchestrator at publish time, after the bytes arrive. This means the descriptor's version field is a semantic label (like `"v1"`, `"latest"`, `"draft"`), not a content-derived identity. Re-publishing to the same descriptor overwrites the hash mapping. This is intentional: `"latest"` semantics require mutability. For immutable pointers, the version should be a value that will not be reused (a ULID, a monotonically increasing integer, or omitted in favor of fetching by hash directly). Embedding the hash in the descriptor was considered and rejected because it would require a two-phase declaration (declare intent, publish bytes, update descriptor), breaking the property that a reviewer sees the complete artifact topology in the DAG before execution.

**Artifact bytes live on disk under `memory/artifacts/`; metadata lives in SQLite.** This matches the persistence layering from the Persistence and audit section: bytes are durable and forensics-grade (survive SQLite corruption, operator-inspectable with standard tools); metadata is queryable and participates in atomic SQLite transactions with executor state. An inline threshold of 4 KB bridges the two: artifacts up to and including 4096 bytes are stored as BLOBs in the metadata table, giving them SQLite's transactional atomicity and single-file backup simplicity. Everything larger goes to disk. The threshold is configurable in `settings.json` under `artifacts.inline_threshold_bytes`.

**Garbage collection is reference counting plus time-based expiry, not mark-and-sweep.** The star topology means the orchestrator owns every artifact reference. Reference counts are always accurate because all increments and decrements happen inside the same SQLite transactions that transition node state. Mark-and-sweep addresses distributed systems where reference counts can drift across nodes; that failure mode does not exist here. Time-based expiry handles the case where an artifact has zero active consumers but should be retained for forensic value, and the complementary case where a global artifact with no declared consumers should eventually be collected.

**`artifact.publish` notifies the executor asynchronously via the event queue, not synchronously within the publish call.** When a publish completes, the artifact layer enqueues a `new_artifact` event into the executor's event queue. The executor picks it up on the next tick, queries `artifact_refs`, and unblocks any successors whose input dependencies are now satisfied. This preserves the executor's single-mutator property (the event pump is the sole mutator of executor state, as specified in the DAG executor section) and means the artifact layer does not need to know about node readiness, edge semantics, or scheduling internals. The cost is one tick of latency between publish and unblock, which is vanishingly small compared to worker runtime. If the event is lost (orchestrator crash before the executor processes it), the reconciler's ready-set recomputation on restart picks up the published artifact from `artifact_refs` and unblocks successors.

### Descriptor semantics and identity

The descriptor shape is fixed by the capability manifest schema: `{namespace: bundle|global|task, name: <string>, version: <string or null>, content_type: <mime-like string>}`. This section specifies resolution semantics.

**Namespace scoping.** `bundle` artifacts are scoped to the producing worker's bundle. Only workers within that bundle can read them, subject to their read grants. They become eligible for garbage collection when the bundle terminates, as described in the lifecycle section below. `task` artifacts are scoped to the producing worker's own task. Only that worker can read them. They die when the worker terminates, subject to a short retention window for retry and debugging. `global` artifacts persist across bundles. Any worker with a read grant can fetch them. They are subject to the global storage cap and are collected only under cap pressure or explicit deletion. Writing to the global namespace requires an explicit `namespace: global` entry in the worker's write patterns; a wildcard namespace pattern (`"*"`) does not grant global write permission. This is a deliberate escalation gate: the human reviewer must explicitly approve global artifact production.

**Name** is a free-form string, typically kebab-case or dot-separated. Names should be descriptive (`test-results`, `coverage-report`, `lint-output`). No structural enforcement beyond what the pattern matching rules impose.

**Version** is a human-meaningful label. Conventions: `"latest"` for a mutable rolling pointer, `"v1"` or `"v2"` for intended-to-be-stable snapshots, and `null` or omitted when versioning is not meaningful. The artifact layer does not enforce immutability on non-`"latest"` versions; re-publishing to `"v1"` overwrites. Immutability is a convention in v1.1. A future design pass may add an immutability flag; see open questions.

**Content type** is a MIME-like string. Examples: `application/json`, `text/plain`, `application/octet-stream`, `application/vnd.studio.worker-report+json`. The artifact layer does not parse content; the content type is metadata for consumers. The `vnd.studio.*` prefix is reserved for system-defined types.

**Identity rule.** Two descriptors refer to the same artifact if and only if all four fields match exactly (after pattern resolution). The hash is not part of identity; it is a property of the artifact bytes, assigned at publish time. This is the opposite of a content-addressed store where the hash is the identity. The reasoning is stated in Design framing.

### Content addressing

**Hash function: BLAKE3.** Three reasons drove the choice. First, performance: BLAKE3 is 5 to 10 times faster than SHA-256 on modern x86-64 CPUs, using AVX2 and AVX-512 SIMD with a portable C fallback on other architectures. At artifact sizes up to 100 MB, the difference is user-visible during publish and fetch. Second, tree hashing: BLAKE3's Merkle tree structure means the hash can be verified incrementally. This is not used in v1.1 (the whole blob is hashed at once), but the capability exists for future streaming verification without a protocol change. Third, cryptographic strength: BLAKE3 is derived from BLAKE2, which was a SHA-3 finalist and has received substantially more cryptanalysis than SHA-256's alternatives. For this system's threat model (integrity against disk corruption and operator error, not adversarial hash collision), BLAKE3 is more than sufficient. The system has no regulatory requirement for FIPS-certified algorithms, which is the only context in which SHA-256 would be preferable. SHA-256 was considered and rejected on performance grounds.

The output is 256 bits (32 bytes), encoded as lowercase hex in the metadata table and in RPC responses (64 characters).

**What is hashed.** The raw artifact bytes exactly as provided by the worker in the `artifact.publish` call. No envelope, no framing, no metadata prepended. The bytes are hashed as-is. For structured content types, the worker is responsible for producing canonical serialization before publishing; the artifact layer neither validates nor transforms the content.

**Hash assignment.** The orchestrator computes the hash at publish time, after receiving the full artifact data from the worker. It is stored in the `artifact_metadata.hash` column and returned to the publishing worker in the RPC response.

**Verification on fetch.** When a worker calls `artifact.fetch`, the orchestrator resolves the descriptor to the stored metadata, retrieves the bytes (from the inline BLOB if present, from disk otherwise), re-computes BLAKE3 over the retrieved bytes, and compares the result to the stored hash. On mismatch the fetch returns a `verification_failed` error and the orchestrator logs an ERROR-level audit event with the descriptor, stored hash, and computed hash. On match the bytes are returned. This means every fetch re-verifies integrity. The cost is one BLAKE3 computation per fetch; at BLAKE3 throughput (multiple GB/s on modern CPUs) and v1.1 artifact sizes, this is negligible.

**Relationship to the version field.** The hash changes on every publish, even to the same descriptor. The version field is set by the worker or planner and not updated by the artifact layer. Version is a semantic pointer; hash is a content pointer. A consumer that wants to verify it received the right bytes should compare the returned hash, not the version string.

### RPC method specifications

All three methods are worker-to-orchestrator calls over the bidirectional JSON-RPC 2.0 channel specified in the Worker RPC protocol section. All are capability-checked by the orchestrator's RPC dispatcher before the handler executes.

**artifact.publish**

```
Request:
{
  "jsonrpc": "2.0",
  "method": "artifact.publish",
  "params": {
    "descriptor": {
      "namespace": "bundle",
      "name": "test-results",
      "version": "v1",
      "content_type": "application/json"
    },
    "data": "<base64-encoded bytes>"
  },
  "id": 1
}

Response (success):
{
  "jsonrpc": "2.0",
  "result": {
    "published": true,
    "hash": "a1b2c3d4e5f67890...",
    "size_bytes": 12345
  },
  "id": 1
}
```

Error codes. `-32001 capability_denied`: the worker's manifest `rpc.artifact_access.writes` has no pattern matching this descriptor. The response includes the failing descriptor and the list of granted write patterns. `-32002 artifact_too_large`: data size exceeds the per-artifact limit of 100 MB. `-32003 storage_full`: per-bundle or global storage cap exceeded; publish rejected even though this artifact individually is within limits. `-32004 invalid_descriptor`: descriptor fails structural validation (unrecognized namespace value, empty name, malformed content type). `-32005 namespace_violation`: worker attempting to publish to a namespace it cannot access, such as another bundle's namespace or the global namespace without explicit grant. `-32602 invalid_params`: the `data` field is missing, not valid base64, or otherwise malformed.

Side effects of a successful publish, executed in order:
1. Compute BLAKE3 over the raw bytes.
2. If `len(data) <= inline_threshold`, store bytes as a BLOB in the metadata row. Otherwise, write bytes to `memory/artifacts/hashes/<hash[0:2]>/<hash>` where `<hash[0:2]>` is the first two hex characters of the hash.
3. INSERT or UPDATE (UPSERT) the `artifact_metadata` row keyed on `(namespace, name, version)`.
4. INSERT a row into `artifact_refs` with the bundle id, producer node id, descriptor JSON, and current timestamp.
5. Enqueue a `new_artifact` event into the executor's event queue.
6. Write an `audit_log` entry: `{event_type: "artifact_published", subject_type: "worker", subject_id: <worker_id>, payload_json: {descriptor, hash, size_bytes}}`.

Steps 3, 4, and 6 are inside a single SQLite transaction. Step 2 for on-disk artifacts is outside the transaction (disk writes cannot participate); the crash recovery section addresses the resulting non-atomic boundary. Step 5 is an in-memory operation.

The capability check on publish proceeds as follows. The concrete descriptor is extracted from the request. The worker's manifest `rpc.artifact_access.writes` patterns are loaded. The descriptor is matched against each pattern using the algorithm in the capability pattern matching subsection below. If no pattern matches, `capability_denied` is returned. Two additional namespace checks are applied beyond pattern matching: if the descriptor namespace is `global`, at least one matching pattern must explicitly list `namespace: global` (a pattern with `namespace: "*"` does not suffice); if the descriptor namespace is `bundle`, the orchestrator verifies that it resolves to the calling worker's own bundle (a worker cannot publish into another bundle's namespace even with a matching pattern).

**artifact.fetch**

```
Request:
{
  "jsonrpc": "2.0",
  "method": "artifact.fetch",
  "params": {
    "descriptor": {
      "namespace": "bundle",
      "name": "test-results",
      "version": "v1",
      "content_type": "application/json"
    }
  },
  "id": 2
}

Response (success):
{
  "jsonrpc": "2.0",
  "result": {
    "data": "<base64-encoded bytes>",
    "hash": "a1b2c3d4e5f67890...",
    "size_bytes": 12345
  },
  "id": 2
}
```

Error codes. `-32001 capability_denied`: the worker's manifest `rpc.artifact_access.reads` has no pattern matching this descriptor. `-32006 artifact_not_found`: no artifact with this descriptor has ever been published. `-32007 artifact_gc_d`: the artifact existed but has been garbage collected; the response includes the `gc_d_at` timestamp and the reason (`bundle_terminated`, `retention_expired`, `explicit_delete`). `-32008 verification_failed`: the stored bytes' BLAKE3 hash does not match the stored hash; this is an integrity violation and a system-level alarm, not a worker-level error.

The capability check on fetch matches the concrete descriptor against the worker's `rpc.artifact_access.reads` patterns. The same matching algorithm applies, without the additional namespace restrictions that writes carry (reads are less dangerous).

Fetch resolution algorithm:
1. Query `artifact_metadata` WHERE namespace = descriptor.namespace AND name = descriptor.name AND version = descriptor.version.
2. If no row: return `artifact_not_found`.
3. If a row exists and `gc_d_at` is not null: return `artifact_gc_d` with the timestamp and reason from the metadata row.
4. Read bytes: from `inline_data` if non-NULL, from the disk path otherwise.
5. Compute BLAKE3 over the retrieved bytes. Compare to `artifact_metadata.hash`.
6. On match: return bytes, hash, and size.
7. On mismatch: return `verification_failed` and log the ERROR audit event.

**artifact.list**

```
Request:
{
  "jsonrpc": "2.0",
  "method": "artifact.list",
  "params": {
    "namespace": "bundle",
    "name_pattern": "test-*"
  },
  "id": 3
}

Response (success):
{
  "jsonrpc": "2.0",
  "result": {
    "artifacts": [
      {
        "descriptor": {
          "namespace": "bundle",
          "name": "test-results",
          "version": "v1",
          "content_type": "application/json"
        },
        "hash": "a1b2c3d4e5f67890...",
        "size_bytes": 12345,
        "published_at": 1715030400
      }
    ]
  },
  "id": 3
}
```

Parameters. `namespace` is optional; it defaults to the calling worker's bundle namespace. `name_pattern` is optional; if omitted, all artifacts in the namespace are returned (subject to the filtering rule below).

Error codes. `-32001 capability_denied`: the worker's RPC methods grant does not include `artifact.list` (this is checked via the RPC methods grant, not via `artifact_access.reads`, since listing is a metadata operation). `-32004 invalid_descriptor`: `name_pattern` is a malformed glob expression.

The response includes only artifacts whose descriptors match at least one of the worker's `rpc.artifact_access.reads` patterns. A worker cannot use `artifact.list` to discover artifacts it lacks permission to fetch. This is a security property: listing must not become a side channel for capability enumeration.

The `artifact.list` RPC method grant is separate from `artifact_access.reads`. A worker may be able to fetch individual artifacts (granted via reads patterns) but not list them all (no `artifact.list` in `rpc.methods`), or vice versa. In practice, most worker manifests will grant both or neither.

### Capability pattern matching

The `rpc.artifact_access.reads` and `writes` fields in the capability manifest hold lists of **descriptor patterns**. A pattern is a partial descriptor where each field supports glob-style wildcards:

```yaml
rpc:
  artifact_access:
    reads:
      - namespace: bundle
        name: "test-results-*"
        version: "*"
        content_type: "application/json"
      - namespace: global
        name: "**"
        version: "*"
        content_type: "*"
```

The `**` wildcard means "any characters, including path separators" (analogous to globstar). A single `*` means "any characters within a single name segment." The distinction exists so hierarchical artifact naming conventions can be adopted later without changing the pattern language. In v1.1, names are flat strings and both wildcards behave identically.

Matching algorithm for a pattern P against a concrete descriptor D:
1. `namespace_match`: P.namespace equals D.namespace, or P.namespace equals `"*"`.
2. `name_match`: glob(P.name, D.name). Case-sensitive.
3. `version_match`: glob(P.version, D.version). If P.version is omitted or null, treat as `"*"`.
4. `content_type_match`: glob(P.content_type, D.content_type). If P.content_type is omitted or null, treat as `"*"`.
5. The pattern matches if and only if all four fields match.

Glob syntax. `*` matches any sequence of characters within a single path segment. `**` matches any sequence of characters including path separators. `?` matches exactly one character. `[abc]` is a character class; `[!abc]` a negated class. No brace expansion, no extglob. The syntax is intentionally a subset of standard shell globs to keep the implementation auditable.

Implementation is a single Python function `glob_match(pattern: str, value: str) -> bool`, roughly 40 lines, that compiles the pattern to a regex and caches the compiled form. It does not need a third-party library. The function is small enough to audit for ReDoS (catastrophic backtracking in user-supplied patterns is a risk with naive regex compilation from globs; the implementation should use a non-backtracking strategy or bound match time).

Namespace write restrictions, stated here for completeness: a worker can write to `namespace: bundle` only for its own bundle; a worker can write to `namespace: task` only for its own task; a worker can write to `namespace: global` only if its write patterns explicitly include `namespace: global`. These are enforced by the `artifact.publish` handler, not by the pattern matcher. The pattern matcher answers only "does this descriptor match this pattern." The handler layers the namespace rules on top.

### Notification mechanism

When `artifact.publish` succeeds, the artifact layer constructs a `new_artifact` event and enqueues it into the executor's event queue:

```json
{
  "event_type": "new_artifact",
  "descriptor_json": "{\"namespace\":\"bundle\",\"name\":\"test-results\",\"version\":\"v1\",\"content_type\":\"application/json\"}",
  "published_at": 1715030400,
  "producer_node_id": "bundle-abc:node-3"
}
```

The executor's event pump receives this on the next tick. Processing:
1. Query `artifact_refs` for rows matching the descriptor JSON in the event.
2. For each matching row, find the successor nodes of `producer_node_id` in `dag_edges`.
3. For each successor, re-evaluate the `pending` to `ready` eligibility: the successor may become ready if this artifact was its last unsatisfied input dependency.
4. Transition ready nodes and dispatch as usual.

The artifact layer does not query `dag_edges`, does not know about node readiness, and does not mutate executor state. It only emits an event. The executor's existing machinery (the event pump, the ready-set scheduler) handles the rest. The single-mutator property is preserved.

The notification is fire-and-forget from the artifact layer's perspective. If the event queue is full or the pump is backed up, the publish still succeeds (the artifact is stored and committed). The event waits in the queue. This is safe because the reconciler (run on orchestrator startup) recomputes the ready set from the current DAG state and `artifact_refs`; missed or delayed notifications do not cause permanent stalls. The event queue is sized comfortably for the artifact volume v1.1 will see (hundreds of artifacts per bundle, not thousands per second).

### Storage layer

**Physical layout on the local filesystem:**

```
memory/
  artifacts/
    hashes/
      00/
        a1b2c3d4e5f67890...
      01/
        f3e4d5c6b7a80912...
      ...
      ff/
        0a1b2c3d4e5f67890...
```

Sharding by the first two hex characters of the BLAKE3 hash creates 256 top-level directories. The shard key is derived from the hash, which is uniformly distributed, so shards fill evenly. A flat directory was considered and rejected because even modest artifact counts (tens of thousands) degrade common filesystem operations when stored in a single directory.

File names are the full 64-character lowercase hex hash. No file extension. The `artifact_metadata.content_type` column is the authoritative source for how to interpret the bytes.

**Inline threshold: 4096 bytes** [PROVISIONAL: should be revisited after observing actual artifact size distributions in the first few dozen bundles; configurable in `settings.json` under `artifacts.inline_threshold_bytes`]. The value was chosen for three reasons. First, 4 KB matches a typical OS page size, aligning with I/O patterns. Second, the most common small artifacts (JSON test results, status blobs, YAML config fragments) are typically in the hundreds to low thousands of bytes, well within 4 KB. Third, inline artifacts benefit from SQLite's atomicity: a crash during publish either commits the full metadata row with the BLOB or commits nothing. There is no orphan window for inline artifacts, unlike on-disk artifacts where the file write and the metadata commit are not atomic.

**Per-artifact size limit: 100 MB.** This is larger than any reasonable single artifact in v1.1 (worker reports, test outputs, code patches, configuration bundles, small binary assets) and small enough to keep garbage collection practical. It is also small enough that base64 encoding in JSON-RPC is tolerable (roughly 150 MB on the wire for 100 MB of data; see the streaming decision and deferred items). If a bundle needs to pass data larger than 100 MB between workers, it should use the git worktree, which workers share via the orchestrator-mediated branch merge specified in Bundle lifecycle: execution and integration, not via the artifact layer. The artifact layer is for structured data, not bulk file transfer.

**Per-bundle storage cap: 1 GB.** Summed across all artifacts in that bundle's namespace. This is a soft cap: when a bundle exceeds its cap, the artifact layer runs a sweep of that namespace to collect eligible artifacts before rejecting the publish. If GC cannot free enough space (all artifacts are still referenced or within their retention window), the publish is rejected with `storage_full`. In practice, a bundle producing 1 GB of artifacts is producing an unusually large volume of structured output.

**Global storage cap: 50 GB** [PROVISIONAL: sized to the 30 GB dev box; should be re-evaluated when actual artifact accumulation patterns are known] (configurable in `settings.json` under `artifacts.global_storage_cap_bytes`). Summed across all global artifacts. Same soft-cap semantics: the GC sweep runs first; if that fails, publishes are rejected. The default of 50 GB is sized to leave comfortable headroom on the 30 GB box; the actual disk has more space than RAM, but 50 GB ensures the artifact store never becomes the dominant disk consumer.

**The ArtifactStore interface.** The abstract interface that lets `LocalFilesystemArtifactStore` (v1.1) and a future `S3ArtifactStore` (k8s) plug in without changes to manifests, RPC handlers, or executor logic:

```python
class ArtifactStore(ABC):
    @abstractmethod
    async def put(self, descriptor: ArtifactDescriptor, data: bytes) -> str:
        """Store artifact bytes. Returns BLAKE3 hex hash. Raises on failure."""
        ...

    @abstractmethod
    async def get(self, descriptor: ArtifactDescriptor) -> Optional[bytes]:
        """Retrieve artifact bytes by descriptor. Returns None if not found."""
        ...

    @abstractmethod
    async def get_by_hash(self, hash: str) -> Optional[bytes]:
        """Retrieve artifact bytes by hash. Returns None if not found."""
        ...

    @abstractmethod
    async def delete(self, descriptor: ArtifactDescriptor) -> bool:
        """Delete artifact by descriptor. Returns True if deleted."""
        ...

    @abstractmethod
    async def delete_by_hash(self, hash: str) -> bool:
        """Delete artifact by hash. Returns True if deleted."""
        ...

    @abstractmethod
    async def exists(self, descriptor: ArtifactDescriptor) -> bool:
        """Check whether an artifact with this descriptor exists."""
        ...

    @abstractmethod
    async def list(self, namespace: str,
                   name_pattern: Optional[str] = None) -> List[ArtifactMetadata]:
        """List artifact metadata in a namespace, optionally filtered by glob."""
        ...

    @abstractmethod
    async def get_metadata(self, descriptor: ArtifactDescriptor
                          ) -> Optional[ArtifactMetadata]:
        """Get metadata for an artifact."""
        ...

    @abstractmethod
    async def total_size(self, namespace: str) -> int:
        """Total bytes stored in a namespace. Used for cap enforcement."""
        ...

    @abstractmethod
    async def sweep_orphans(self) -> int:
        """Remove on-disk artifact files with no metadata row.
        Returns count removed."""
        ...
```

**LocalFilesystemArtifactStore.** `put`: if `len(data) <= inline_threshold`, UPDATE `artifact_metadata` with `inline_data = data`. Otherwise, write to `self.root / "hashes" / hash[0:2] / hash`, then UPDATE or INSERT the metadata row with `inline_data = NULL`. `get`: resolve descriptor to metadata; if `inline_data` is non-NULL return it; otherwise read from the disk path, verify the hash, and return the bytes. `get_by_hash`: query metadata by hash, same retrieval logic. `delete`: remove the metadata row and, if on disk, unlink the file. `list`: SELECT from `artifact_metadata` filtered by namespace and optional name pattern (translated from glob to SQL LIKE). `sweep_orphans`: list all files under `hashes/`, cross-reference with `artifact_metadata.hash`, unlink files with no matching metadata row.

A future `S3ArtifactStore` implements the same interface. `put` writes to an S3 bucket with key `artifacts/<hash[0:2]>/<hash>`. `get` issues an S3 GetObject. `sweep_orphans` lists S3 objects and cross-references. The orchestrator's artifact layer code does not change; only the concrete store class changes at startup based on configuration.

### Metadata schema

The artifact layer adds one table to the SQLite schema:

```sql
CREATE TABLE artifact_metadata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL CHECK(namespace IN ('bundle', 'global', 'task')),
  name TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  content_type TEXT NOT NULL,
  hash TEXT NOT NULL,                -- BLAKE3 lowercase hex, 64 chars
  size_bytes INTEGER NOT NULL,
  inline_data BLOB,                  -- NULL if stored on disk
  producer_node_id TEXT,             -- dag_nodes.id or NULL
  producer_worker_id TEXT,           -- workers.id or NULL
  bundle_id TEXT,                    -- bundles.id, NULL for global artifacts
  task_id TEXT,                      -- worker-scoped task identifier
  ref_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  published_at INTEGER NOT NULL,
  expires_at INTEGER,                -- NULL for permanent
  gc_eligible_at INTEGER,            -- computed by GC policy
  gc_d_at INTEGER,                   -- NULL if still alive
  UNIQUE(namespace, name, version)
);

CREATE INDEX idx_artifact_metadata_hash ON artifact_metadata(hash);
CREATE INDEX idx_artifact_metadata_bundle ON artifact_metadata(bundle_id);
CREATE INDEX idx_artifact_metadata_ns_name ON artifact_metadata(namespace, name);
CREATE INDEX idx_artifact_metadata_gc ON artifact_metadata(gc_eligible_at)
    WHERE gc_eligible_at IS NOT NULL AND gc_d_at IS NULL;
```

Field notes. `version` defaults to the empty string rather than NULL so the UNIQUE constraint works consistently; a missing version in the descriptor is normalized to `""`. `producer_node_id` and `producer_worker_id` are nullable because artifacts injected from outside the DAG (bundle inputs provided by the planner) have no producer node, and artifacts produced by the system itself (verification reports generated by the QA agent post-execution) have a producer node but not necessarily a worker. `bundle_id` is the owning bundle; for `namespace: global` it is NULL. `task_id` is the producing task for provenance. `ref_count` is the number of active consumers (workers currently in `ready` or `running` state that declare this artifact as an input). `gc_eligible_at` is set when the artifact becomes logically eligible for collection; it may be NULL for global artifacts that are not time-expiring. `gc_d_at` is set when the artifact is physically deleted; it acts as the tombstone that distinguishes "never existed" from "was collected" in fetch error responses.

**Relationship with the executor's `artifact_refs`.** The two tables serve different purposes. `artifact_metadata` is the artifact layer's source of truth: it owns the storage, the hash, the GC lifecycle, and the full provenance. `artifact_refs` (specified in the DAG executor section) is the executor's join table: it answers the single question "has a predecessor of the current node published an artifact matching this descriptor?" The executor queries `artifact_refs` by `bundle_id` and `descriptor_json`; it never reads `artifact_metadata`. The artifact layer reads and writes `artifact_metadata` and inserts into `artifact_refs`; it never queries `artifact_refs`. Both inserts happen in the same SQLite transaction, so a published artifact is visible to the executor atomically.

The two tables carry redundant data (descriptor fields appear in both). This is acceptable because `artifact_refs` is a read-optimized join table with a narrow, fixed query pattern and no GC concerns. Denormalization keeps the executor's queries simple and independent of the artifact layer's schema evolution.

For artifacts declared as DAG inputs but produced outside the DAG (bundle inputs provided by the planner at planning time), an `artifact_metadata` row exists but there may be no corresponding `artifact_refs` row, since `artifact_refs` only tracks artifacts produced by DAG nodes. The executor handles this case by checking `artifact_metadata` as a fallback when `artifact_refs` has no match. This fallback is limited to entry node inputs; all other artifacts must flow through the DAG and will have `artifact_refs` entries.

### Lifecycle and garbage collection

**When artifacts become eligible for collection:**

Task-scoped artifacts become eligible when the producing task's worker terminates (clean exit, failure, or kill). `gc_eligible_at` is set to `worker_ended_at + task_retention_seconds`, with a default retention of 86400 seconds (24 hours) [PROVISIONAL: calibrated to an assumed operator debug cadence; configurable]. The 24-hour window allows the worker's own retry attempts and brief forensic inspection.

Bundle-scoped artifacts become eligible when the bundle reaches a terminal state (`complete`, `failed`, `rejected`). `gc_eligible_at` is set to `bundle_ended_at + bundle_retention_seconds`. The default retention is 604800 seconds (7 days) [PROVISIONAL] for `complete` and `rejected` bundles, and 2592000 seconds (30 days) [PROVISIONAL] for `failed` bundles. Both are configurable in `settings.json`. The extended retention for failed bundles preserves forensic artifacts for post-mortem analysis without requiring operator intervention.

Global artifacts do not become eligible by time. They persist until explicitly deleted or until the global storage cap triggers LRU eviction. A global artifact with `ref_count = 0` and no `gc_eligible_at` is still alive; it is collected only under storage pressure.

**Collection condition.** An artifact is collected when `gc_eligible_at IS NOT NULL AND gc_eligible_at <= now() AND gc_d_at IS NULL AND ref_count = 0`. For global artifacts under cap pressure, the condition is relaxed to also consider `ref_count = 0` global artifacts without `gc_eligible_at`, ordered by `published_at` ascending (oldest first). This is the only case where a non-expired artifact is collected.

**Reference count maintenance.** `ref_count` is incremented when a worker node is dispatched: the executor reads the node's `inputs.artifacts` list from the DAG node spec and increments the ref_count for each declared input artifact in the same SQLite transaction that transitions the node from `ready` to `running`. It is decremented when the worker completes (any terminal state): the executor decrements the ref_count for all input artifacts in the same transaction that transitions the node to `completed`, `failed`, `skipped`, or `cancelled`. If a node is skipped before dispatch, its ref_count was never incremented, so no decrement is needed.

On orchestrator crash recovery, the reconciler walks `dag_nodes` in state `running` (which become `failed` per the kill-all policy) and decrements the ref_count of their input artifacts as part of the reconciliation transaction. This guarantees ref_count consistency across crashes: every increment that was committed is paired with exactly one decrement, either from normal node completion or from crash reconciliation.

**When GC runs.** Four triggers. First, on every bundle terminal state transition: the artifact layer sweeps that bundle's namespace, sets `gc_eligible_at` for all artifacts based on the bundle terminal state and retention policy, then collects any that are already past their window. Second, on every worker terminal state transition: the artifact layer sweeps that task's namespace and collects expired task-scoped artifacts. Third, a periodic background sweep runs hourly (configurable) across all namespaces: collects eligible artifacts, sweeps orphaned bytes, and enforces the global storage cap. Fourth, on `artifact.publish` when caps are near: before accepting a publish, the artifact layer checks whether the target namespace would exceed its cap and runs an immediate sweep to free space if so.

**Retention overrides.** The reviewer can pin a bundle via the MCP surface or CLI (`studio bundle retain <bundle-id> <duration>`), which extends retention for that bundle's artifacts. Pinned artifacts have their `gc_eligible_at` set to the pin expiry. Pins are recorded in `approval_decisions` with `decision = 'retain'`.

**Orphaned byte cleanup.** Files under `memory/artifacts/hashes/` with no corresponding `artifact_metadata` row are orphans. They arise when the orchestrator crashes between writing a file and committing its metadata row, or when a metadata row is deleted but the file unlink fails (disk full, permission change). The `sweep_orphans` method runs during the periodic background sweep, not on the critical path of publish or fetch.

**Behavior when a referenced artifact has been collected.** `artifact.fetch` returns error `-32007 artifact_gc_d` with the `gc_d_at` timestamp and the reason. The worker treats this as a fatal input error and surfaces it in `worker.final_report` with a structured error containing the missing descriptor. The executor distinguishes `artifact_gc_d` from `artifact_not_found` in its error classification. `artifact_not_found` signals a malformed DAG (a node declared an input that no predecessor promised to produce). `artifact_gc_d` signals a system-level timing issue (the retention window was too short for the bundle's execution duration) and surfaces differently to the reviewer.

### The secrets.fetch RPC

The Worker environment section states the intent to migrate from env-var secret delivery to RPC-fetched short-lived credentials. This subsection specifies that protocol. Secrets are a degenerate kind of artifact: they are named, capability-checked, fetched over RPC, never persisted in worker state, and have their own audit trail rather than artifact metadata rows.

**Method: `secrets.fetch`** (worker-to-orchestrator).

```
Request:
{
  "jsonrpc": "2.0",
  "method": "secrets.fetch",
  "params": {
    "name": "github-app-installation-token"
  },
  "id": 4
}

Response (success):
{
  "jsonrpc": "2.0",
  "result": {
    "value": "ghs_xxxxxxxxxxxxxxxxxxxx",
    "expires_at": 1715116800
  },
  "id": 4
}
```

**Authentication.** The orchestrator identifies the calling worker from the RPC connection binding, established at connection setup with the one-time `STUDIO_WORKER_TOKEN` specified in Worker RPC protocol, Authentication. No additional authentication is needed for `secrets.fetch`; the connection identity is the worker identity.

**Capability binding.** The orchestrator loads the calling worker's manifest and checks the `secrets` grant list for an entry matching `name`. Three conditions must hold: a grant exists with the same name; the grant is part of the worker's currently active capability set (not revoked, not expired); and the grant's delivery mechanism is compatible with `secrets.fetch`. If the grant specifies `delivery: rpc`, the fetch proceeds. If the grant specifies `delivery: env` or `delivery: file`, those are resolved at worker spawn time and the orchestrator may still serve a `secrets.fetch` for them (the worker wants to refresh a value it already has), but the default is that `delivery: rpc` is the explicit opt-in. If any condition fails, the orchestrator returns `-32001 capability_denied` with a message naming the missing grant.

**Delivery format.** The secret value is returned as a plain string in the `value` field. It never touches disk in the worker environment. The worker receives it in the RPC response handler and can hold it in a local variable. The secret lives only in the worker's process memory for the lifetime of that variable. Workers should discard the variable after use (Python `del`; the next GC cycle collects the string), but the protocol does not enforce this. The defense-in-depth is process isolation: the worker's memory is private to its namespace via bubblewrap. `expires_at` is a Unix timestamp; it may be null for secrets without a known expiry.

**Audit trail.** Every `secrets.fetch` call appends a line to `memory/audit/credential-use.jsonl`, extending the file format specified in Persistence and audit:

```json
{
  "worker_id": "ulid",
  "bundle_id": "ulid",
  "task_id": "task-node-3",
  "secret_name": "github-app-installation-token",
  "purpose": "github_auth",
  "method": "secrets.fetch",
  "timestamp": 1715030400
}
```

The `secret_name` is recorded, never the value. The `purpose` comes from the manifest grant. The `method` field distinguishes RPC-fetched secrets from env-var delivery (`method: "env"`) and file delivery (`method: "file"`), which existing audit entries use.

**Short-lived and long-lived secrets.** `secrets.fetch` is designed for short-lived credentials. The primary v1.1 use case is the GitHub App installation token: the orchestrator calls the GitHub API to generate an installation token (1-hour expiry) on demand, caches it, and returns it. The worker uses it and discards it. For long-lived secrets (API keys that do not rotate), `env` and `file` delivery mechanisms from the manifest are more appropriate. `secrets.fetch` can serve them too, but the worker would need to call it repeatedly with no refresh benefit. If a secret has a known expiry, the orchestrator includes `expires_at` in the response; workers should re-fetch before expiry.

**Refresh and rotation.** Refresh is initiated by the worker calling `secrets.fetch` again for the same name. The orchestrator may return the same value (if still valid) or a new one (if rotation is due). Rotation policy is the orchestrator's responsibility. For GitHub App installation tokens, the orchestrator caches tokens per installation and serves them until 5 minutes before expiry, then generates a new one on the next fetch. Workers see a consistent value per fetch. If a secret has been revoked at its source (the GitHub App's private key was rotated), the orchestrator's next attempt to generate a token fails, and `secrets.fetch` returns `-32009 secret_unavailable` with a message and a `retryable` boolean. If the name does not exist in the orchestrator's secret store, the error is `-32010 secret_not_found`.

Error codes for `secrets.fetch`: `-32001 capability_denied` (worker lacks a secrets grant for this name, or the grant's delivery mechanism is not compatible); `-32009 secret_unavailable` (the secret store cannot produce this secret, with `retryable: true` for transient upstream failures and `retryable: false` for permanent revocation); `-32010 secret_not_found` (the name does not exist); `-32602 invalid_params` (the `name` field is missing or empty).

### Crash recovery and consistency

A publish spans the artifact layer, the SQLite database, the filesystem, and the executor's event queue. The operations are:

1. Worker sends `artifact.publish` RPC.
2. Orchestrator receives data, computes BLAKE3 hash.
3. Orchestrator writes bytes (to disk for large artifacts, to SQLite BLOB for inline).
4. Orchestrator UPSERTs `artifact_metadata` and INSERTs into `artifact_refs` in one SQLite transaction.
5. Orchestrator enqueues `new_artifact` into the executor's event queue.
6. Orchestrator returns success response to the worker.

Four crash scenarios:

**Crash during steps 1 or 2 (data in flight, nothing stored).** The worker's RPC call fails with a connection error. Following the DAG executor's retry policy, the worker retries the publish. No state exists to clean up.

**Crash during step 3 (bytes stored, metadata not committed).** For inline artifacts, the BLOB write is inside the SQLite transaction, so the rollback on restart removes it. For on-disk artifacts, the file exists on disk but the metadata transaction rolled back; no metadata row exists. The file is orphaned. The periodic background sweep's `sweep_orphans` cleans it. The worker retries the publish. The retry writes the same bytes to the same file path (overwrite, same as a no-op) and this time the metadata transaction commits.

**Crash during step 4 (SQLite committed, notification not sent).** The artifact exists in both `artifact_metadata` and `artifact_refs`. The `new_artifact` event was never enqueued, or was enqueued in memory and lost. On restart, the reconciler recomputes the ready set from `dag_nodes` state and `artifact_refs`. The artifact is visible in `artifact_refs`, so the ready-set computation picks it up and unblocks any successors. No special recovery code needed; the reconciler's existing logic handles this.

**Crash during steps 5 or 6 (notification enqueued, response not sent).** The artifact is fully published. The in-memory event is lost. Same recovery as above: the reconciler picks up the artifact via ready-set recomputation. The worker did not receive the response and treats the publish as failed. On retry, it re-publishes. The re-publish is an UPSERT on `(namespace, name, version)`, making it idempotent. The hash is deterministic (same bytes produce the same hash). The file on disk is the same, and overwriting it is a no-op.

**The non-atomic boundary** between disk writes and SQLite commits is accepted. A two-phase commit protocol spanning disk and SQLite was considered and rejected as disproportionate for a single-process system where the failure window is a crash, not a network partition. The recovery mechanisms (worker idempotent retry, reconciler ready-set recomputation, orphan sweep) handle the boundary correctly. The design converges: every publish either fully commits or is retried until it does.

**Idempotency of publish.** `artifact.publish` is idempotent by design. The hash is deterministic. The UPSERT on the unique descriptor key means re-publishing to the same descriptor overwrites the previous mapping. If the bytes are identical (worker retried with the same output), the hash is identical and the file on disk is unchanged. If the bytes differ (worker produced different output on retry), the hash differs and the metadata row is updated. Consumers that resolved the descriptor before the retry may see the old bytes if they fetch before the update. This is acceptable because worker retries are rare and the artifact is versioned by descriptor; if consumers need a specific immutable snapshot, they should declare an explicit version in the descriptor. The `"latest"` semantics for reused versions correctly reflect that a re-publish changes what `"latest"` points to.

### Integration with DAG validation

The Task DAG schema section specifies validation rule 4: every artifact a task reads is either external (provided as bundle input) or written by some predecessor in the DAG; no reads from the future.

**Static enforcement at DAG validation time.** The schema validator builds a dependency graph from the DAG. It collects all `outputs.artifacts[*].ref` descriptors (what nodes promise to produce) and all `inputs.artifacts[*].ref` descriptors (what nodes declare they will consume). For each input artifact, if the namespace is `global` or the artifact is marked as external in the input spec, the check is skipped (the artifact is assumed to pre-exist). Otherwise, the validator finds all ancestor nodes (transitive predecessors through `dag_edges`) that list this descriptor in their outputs. Validation uses literal field comparison, not glob matching, because node specs declare concrete descriptors; patterns exist only in the capability manifest. If no ancestor publishes the artifact: validation error. If an ancestor publishes it but a non-ancestor does too (a sibling or a node in an unrelated branch): validation warning, because the static structure suggests an ordering ambiguity even if the runtime scheduler will sort it out.

**Dynamic enforcement at runtime.** The executor does not re-run full DAG validation (it trusts the validated DAG). It enforces artifact dependencies through scheduling: a node in `pending` state does not transition to `ready` until all its declared input artifacts exist in `artifact_refs` or, for entry nodes only, in `artifact_metadata` (for bundle inputs injected outside the DAG). If a node declares an input artifact that no predecessor publishes, the node stays `pending` forever. The 8-hour stalled-bundle detector from the Review Deck v1 spec catches this as a stall, surfaces it, and the bundle eventually fails. This is defense-in-depth: static validation catches malformed DAGs before execution; runtime scheduling catches dynamic situations where a predecessor promised to publish but failed to (the worker crashed with no retries remaining, the gate evaluated to false and the producing branch was skipped).

### Streaming decision

v1.1 does not support streaming for artifact transfer. The 100 MB per-artifact limit makes single-message base64-encoded JSON-RPC transfer acceptable, if inefficient. The 33% base64 overhead and the encode and decode CPU cost are tolerable at v1.1 throughput (hundreds of artifacts per bundle, not thousands per second).

Streaming becomes necessary when artifacts routinely exceed 100 MB or when the base64 overhead becomes a measurable bottleneck on the orchestrator's event loop. The criteria for "routinely" and "measurable" are empirical; the first contact with real workloads will answer them.

A design sketch for streaming is provided so the path is clear when the time comes. `artifact.stream_put(descriptor, total_size_bytes)` returns `{stream_id, chunk_size}`. The worker sends chunks over a binary side channel (raw TCP with length-prefixed frames, separate from the JSON-RPC control channel). The final chunk signals completion; the orchestrator computes the hash and returns it. `artifact.stream_get(descriptor)` returns `{stream_id, total_size_bytes, chunk_size}`; the orchestrator sends chunks over the binary side channel. Hash verification happens at the end when the worker computes the hash of the assembled bytes and compares to the returned hash. The binary side channel is the right substrate for streaming because JSON-RPC 2.0 has no streaming primitive and base64 is unacceptable for multi-GB transfers. This sketch is deferred in full; the single-message publish and fetch specified here are sufficient for v1.1.

### Testing strategy

Three layers, following the pattern established in the DAG executor's testing strategy.

**MockArtifactStore** implements the `ArtifactStore` interface with an in-memory dictionary. It supports configurable delays (simulate slow disk), configurable failures (disk full, verification failure, orphan injection), and direct inspection of stored artifacts for test assertions. It is the primary test backend for executor tests that need artifact interactions.

**Property-based tests on content addressing.** For any random bytes, `hash = BLAKE3(bytes)`, `put(descriptor, bytes)`, `get(descriptor)` returns the same bytes and the same hash. For any two different byte sequences, their BLAKE3 hashes differ (with overwhelming probability; the test asserts inequality for a large random sample). For any artifact fetch, the hash is re-verified; a mock that flips a bit in the stored bytes triggers `verification_failed`. For any descriptor pattern P and concrete descriptor D, `glob_match(P, D)` is consistent: if P matches D, each field matches individually; if P does not match D, at least one field fails.

**GC determinism tests.** Create artifacts with known expiration times, advance a virtual clock, run the GC sweep, assert exactly the expected artifacts are collected. Verify that `ref_count > 0` prevents collection even when `gc_eligible_at` is in the past. Verify orphan cleanup: create files on disk with no metadata rows, run `sweep_orphans`, assert they are removed. Verify cap enforcement: publish artifacts until the cap is exceeded, assert GC is triggered and space is reclaimed or publishes are rejected.

**Replay tests** using `artifact_metadata` and `audit_log` traces, analogous to the DAG executor's replay tests. A trace of publishes and fetches replayed against the artifact store verifies refactor correctness.

No real filesystem I/O in unit tests except when specifically testing `LocalFilesystemArtifactStore`. The test suite runs on CI without special filesystem setup.

### Open questions and flagged decisions

**Inline threshold of 4096 bytes.** The reasoning (page-size alignment, common small-artifact sizes) is sound, but the value should be revisited after observing actual artifact size distributions in the first few dozen bundles. It is configurable and easy to change.

**Retention windows.** The 7-day default for complete and rejected bundles and the 30-day default for failed bundles are calibrated to an assumed operator review cadence. If the operator routinely ignores forensic artifacts or post-mortems happen much faster or slower, these numbers should move. Both are configurable.

**Global artifact default TTL.** Global artifacts currently live forever until cap-evicted or explicitly deleted. A configurable default TTL (for example, 90 days) would prevent unbounded accumulation without surprising workers, since workers that need a global artifact indefinitely should be rare. The current "forever" default is safe but may produce operational surprise when the global cap is hit and artifacts start disappearing.

**Version immutability.** Currently re-publishing to any version string overwrites. Making non-`"latest"` versions immutable (rejecting re-publishes with a new `version_immutable` error) would prevent accidental overwrites and make the version field a meaningful stability signal. This is a v1.2 design question; v1.1 accepts the current overwrite semantics.

**`artifact.list` pagination.** At v1.1 scale (hundreds of artifacts per bundle), a single unpaginated response is fine. If dynamic expansion routinely produces thousands of artifacts, pagination becomes necessary. The design slot is straightforward (add `limit` and `cursor` parameters to the request).

**`secrets.fetch` purpose filter.** The worker currently requests by name only. If the same secret name is granted for multiple purposes in the manifest, the orchestrator resolves to the first matching grant. The worker cannot say "give me the `github_auth` version." This is fine for v1.1 since most secrets have a single purpose.

**Credential-use audit aggregation for `secrets.fetch`.** Workers that refresh short-lived tokens every hour produce one audit line per hour per worker. This could become noisy for long-running bundles with many workers. An aggregation window (log a summary every N fetches) is a future refinement.

### Rejected alternatives

**SHA-256 as the content hash.** Considered. Rejected in favor of BLAKE3. SHA-256 is more widely recognized and has FIPS certification, but the performance delta (5 to 10 times slower) is real at artifact scale, and the system has no regulatory requirement for FIPS-certified algorithms. BLAKE3's cryptographic strength is sufficient for a threat model concerned with integrity against corruption and error rather than adversarial hash collision. Its tree-hashing structure is also forward-looking for streaming verification.

**Content hash embedded in the descriptor.** Considered. Rejected because descriptors are declared before bytes exist. Embedding the hash would require a two-phase declaration (declare intent, publish bytes, update the descriptor with the hash), breaking the property that a reviewer sees the complete artifact topology in the DAG before execution begins.

**All artifacts stored inline in SQLite.** Considered. Rejected because SQLite BLOB performance degrades with very large values, and storing multiple GBs of artifacts in a single SQLite file makes backup and corruption recovery harder. The 4 KB inline threshold captures the common case while keeping the database small and fast.

**All artifacts stored on disk, no inline.** Considered. Rejected because small artifacts benefit from SQLite's atomic transactions (a crash during publish of a 2 KB JSON blob produces no orphan file; the transaction simply rolls back). Single-file backup is also simpler when small artifacts are included.

**Pure reference-counting GC, no time-based expiry.** Considered. Rejected because global artifacts with no declared consumers would never be collected, leading to unbounded storage growth. Time-based expiry coupled with cap-pressure LRU eviction solves this.

**Pure mark-and-sweep GC.** Considered. Rejected because the star topology means the orchestrator owns every artifact reference. Reference counts are always accurate and do not require a separate mark phase. Mark-and-sweep exists to address distributed systems where reference counts can drift; that failure mode is not applicable here.

**Immediate GC on bundle termination, no retention window.** Considered. Rejected because forensic value is real. When a bundle fails, the operator needs the artifacts to understand why. A 7-day or 30-day retention window costs disk space that is bounded and cheap, and provides substantial operational value.

**Two-phase commit protocol for artifact publish spanning disk and SQLite.** Considered. Rejected as disproportionate for a single-process system. The failure window is an orchestrator crash (not a network partition), and the recovery mechanisms (worker idempotent retry, reconciler ready-set recomputation, orphan sweep) handle the non-atomic boundary correctly.

**Worker-to-worker artifact handoff bypassing the orchestrator.** Previously rejected in Bundle lifecycle: execution and integration for peer-to-peer worker communication generally. Re-stated here as applying specifically to artifact transfer. The reasoning is unchanged: it breaks the capability model, explodes the security surface, and complicates k8s deployment.

**`artifact.request` and `worker.prepare_handoff` as the artifact transfer mechanism.** These methods are protocol-reserved stubs in the Worker RPC protocol section. They were designed before the artifact protocol was fully specified. With `artifact.publish` and `artifact.fetch` now specified, these methods are superseded. They should be removed from the RPC method list since they were never implemented and the new methods fully replace them.

### Deferred items

These items replace the current "Artifact protocol details" entry in the v1.1 Deferred items section:

**Artifact streaming (stream_put and stream_get).** Deferred until artifact sizes routinely exceed 100 MB or base64 overhead becomes a bottleneck. The design sketch is in the streaming decision subsection.

**Version immutability enforcement.** Making non-`"latest"` versions reject re-publishes would prevent accidental overwrites. Flagged in open questions for a v1.2 design pass.

**Artifact signing.** Content hashing provides integrity; signing would provide non-repudiation. Valuable for compliance use cases but not in v1.1.

**Transparent compression at the ArtifactStore layer.** Can be added without RPC or schema changes; not needed at v1.1 throughput.

**`artifact.list` pagination.** Needed when artifact counts exceed the single-response practical limit. Not at v1.1 scale.

**Binary side channel for artifact data.** If JSON-RPC base64 overhead becomes a bottleneck, a separate binary channel alongside the JSON-RPC control channel is the natural path. Coupled with the streaming decision.

**Cross-bundle artifact sharing semantics.** The `namespace: global` pathway is the architectural home but the design for cross-bundle read grants, namespace collision policies, and global artifact lifecycle when multiple bundles reference the same artifact is not specified. Deferred under cross-bundle dependencies.

**Global artifact default TTL.** Whether global artifacts should have a configurable default expiry rather than living forever. Flagged in open questions.

**Credential-use audit aggregation for `secrets.fetch`.** Workers refreshing short-lived tokens every hour produce one audit entry per hour per worker. Aggregation is a future refinement.

**Artifact-level immutability and pinning flags.** A `pinned` flag and an `immutable` flag on `artifact_metadata` rows, settable at publish time or by the reviewer, would give more granular retention and integrity control. Not in v1.1.

## Bundle lifecycle: planning and approval

A bundle is the unit of human approval and execution. Its lifecycle starts when an idea (from any source) is picked up by a bundler agent and ends when the work is shipped, parked, or killed. This section covers the planning and approval portion: the input schema, the bundler's planning job, the pre-execution review tracks and their integration with the approval matrix, modification requests and re-scoring, default actions, cooldown durations, and multi-surface race resolution. The execution portion follows.

### Bundle input schema

The bundle input is the typed contract between whoever files work and the bundler agent. The orchestrator validates it before handing it to the bundler. The task-level I/O spec (Task DAG schema) is the structural model; the bundle-level schema mirrors it at a higher abstraction level.

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

**Field specifications:**

**`idea`** (required, string, max 65536 bytes). Free-text description of the work requested. Deliberately unstructured; the bundler's job is to structure it. The orchestrator rejects empty strings and strings over 64KB with error `INVALID_INPUT: idea must be 1-65536 bytes`.

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

**Orchestrator validation (pre-bundler).** Before handing the input to the bundler agent, the orchestrator runs these validations in order:

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

### Planning

**Planning** is done by a bundler agent. The bundler reads the inbound idea, consults memory (similar past bundles, calibration data, prior killed ideas with reasoning), drafts requirements, drafts an RFC, drafts a UX flow if relevant, drafts an implementation plan, drafts a verification plan, and decomposes the work into a task DAG with capability manifests. It also computes a complexity score and a risk score and writes a concerns section.

The bundler is required to populate the concerns section. "No concerns" on a high-risk bundle is treated as a calibration signal that something is off, not as confirmation that the bundle is safe.

### Pre-execution review tracks

**Pre-execution review tracks** run before the bundle reaches the human reviewer. Three specialist tracks, each emitting structured findings into the bundle artifact:

1. **General adversarial critique.** Generalist critic looking for weak reasoning, unaddressed counter-cases, scope creep, hidden complexity, mismatch between requirements and RFC, mismatch between RFC and implementation plan. Findings tagged `severity:{low|med|high}` and `status:{resolved|accepted-risk|unresolved}`.

2. **Security review.** Specialist security critic with a different prompt and a different lens: threat model, authentication and authorization, data handling, input handling (every external input treated as hostile), dependencies (CVEs, supply chain), secrets and tokens (no leaks into logs, error messages, client-side code, or git history), failure modes (fail closed vs. fail open). Output includes a structured threat model added to the bundle body when the bundle touches auth, data handling, external surfaces, secrets, billing, or PII (otherwise the threat model section is omitted, not stub-filled). Findings tagged `severity:{info|low|med|high|critical}` and `status:{resolved|accepted-risk|unresolved}`. Hard rules: critical findings always require explicit human review even when resolved; high findings disable auto-ship; bundles touching auth, billing, secrets, or PII require security sign-off and never auto-ship.

3. **QA / verification planning.** A QA agent that doesn't test the code (the code might not exist yet) but produces a Verification Plan: acceptance criteria (observable, testable conditions tied back to requirements), test surface (unit, integration, end-to-end, load, manual smoke, with coverage targets), pre-merge gates (CI, coverage threshold, security findings resolved, manual smoke checklist), post-ship verification (specific metrics, time windows, expected ranges; this is the data the post-mortem feedback loop will consume), and a rollback plan. Hard rules: no bundle reaches human review without a Verification Plan; bundles without a viable rollback auto-bump Reversibility to 3 in stakes scoring.

The same QA agent is called again post-execution to validate the shipped product against the Verification Plan it itself produced. This is a deliberate dual use: pre-execution, the QA agent does verification planning; post-execution, the QA agent runs the plan against the actual shipped artifact. The two jobs are genuinely different (planning vs. validation) but share infrastructure (the same agent, the same capability scope, the same rubrics) and naturally compose. The full handoff seam is specified in Bundle lifecycle: execution and integration.

Pre-execution review tracks may emit capability requests as a separate output stream from findings. A security review agent flagging "this finding would be more reliable with secret-scanning tooling I don't have" generates both a finding (about the bundle) and a capability request (about the system).

### Pre-execution review track integration with approval matrix

**Pre-execution review tracks run before the approval matrix.** This ordering is ratified and load-bearing. The sequence is: bundle proposed, pre-execution review tracks run, review findings stored, approval matrix evaluates, decision. The approval matrix's decision logic depends on review track outputs: a bundle with a critical security finding cannot auto-ship regardless of its risk and complexity scores. If review tracks ran after the matrix, the matrix would make decisions on incomplete information, and the auto-ship gate would have to be re-evaluated after review tracks complete, which is effectively the same ordering with extra steps.

**Data flow.** Each track's findings are stored as bundle-scoped artifacts (descriptors: `bundle:adversarial-findings`, `bundle:security-findings`, `bundle:verification-plan`). They are also inlined into the bundle's proposal for the reviewer's direct inspection. The approval matrix evaluator (a deterministic function in the orchestrator, not an LLM call) reads the bundle's complexity and risk scores, the review track findings, and the mandatory-review trigger list. It produces a tier decision and an auto-ship eligibility boolean.

**Matrix decision logic** (pseudocode):

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
        return Tier.FULL_REVIEW, auto_ship=False, reason="unresolved security findings"

    # Rollback plan gates auto-ship
    verification_plan = findings.get("verification_plan")
    has_viable_rollback = verification_plan and verification_plan.rollback_feasible
    if not has_viable_rollback:
        bundle.risk_scores.reversibility = max(bundle.risk_scores.reversibility, 3)

    # Auth / billing / secrets / PII gate auto-ship
    touches_sensitive = any(tag in bundle.tags for tag in ("auth", "billing", "secrets", "pii"))
    if touches_sensitive:
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

The matrix decision (tier, auto_ship boolean, reason) is stored in the `bundles` table (`tier` column) and in `approval_decisions` (with `decision = tier` and `actor = "system"` for auto-decisions). The reviewer sees the decision on all surfaces and can override upward (escalate to a higher tier) but not downward.

### Approval matrix

**Approval matrix.** Complexity (0–10) and risk (0–10) are scored independently. Complexity factors: estimated lines of code touched (banded), number of components/subdirectories touched, number of worker tasks in the decomposition, cross-component coordination required, new abstractions introduced. Risk factors: touches security-sensitive paths (path patterns configurable per repo in `settings.json`), touches data-handling paths, touches public/external interfaces, reversibility, production proximity (currently 0 in v1), net-new dependency additions. Each factor's contribution is shown in the approval surface so the reviewer can sanity-check the math.

The 3×3 matrix gates bundles into one of four tiers:

|                | Complexity 0–3 (simple) | Complexity 4–6 (moderate) | Complexity 7–10 (complex) |
|----------------|-------------------------|---------------------------|---------------------------|
| Risk 0–2 (minimal)  | Auto-approve            | Auto-approve + notify     | Approve-with-summary       |
| Risk 3–5 (moderate) | Approve-with-summary    | Approve-with-summary      | Full human review          |
| Risk 6–10 (high)    | Full human review       | Full human review         | Full review + cooldown     |

The four tiers:

- **Auto-approve.** Bundler creates the feature branch and starts work without asking. The reviewer is informed via a single line in a daily digest.
- **Approve-with-summary.** Bundler posts a summary (proposal one-liner, complexity/risk scores with breakdown, files-to-be-touched list, test plan summary, estimated wall-clock duration, link to full RFC). Default action if reviewer doesn't respond in the configured window: **hold** (require explicit response). All cells default to hold. The per-cell overrides in `settings.json` under `approval.default_action_overrides` start at `"hold"` and may be changed to `"approve"` after the calibration loop has accumulated enough history to justify auto-approve for specific cells. The timeout window is configurable in `settings.json` under `approval.summary_timeout_hours` (default 4 hours).
- **Full human review.** Bundler posts the full RFC, verification plan, worker decomposition, capability requests, and concerns section. No default action; bundle waits indefinitely.
- **Full review with cooldown.** Same as full review, plus a minimum 1-hour dwell time before approval can be granted, even if the reviewer responds immediately. Forcing function for high-stakes changes: look at it, walk away, come back. For bundles flagged `irreversible` (rollback is not machine-executable and manual recovery would require >1 hour of operator time, as assessed by the Verification Plan's rollback plan), the cooldown is 24 hours rather than 1 hour. The `irreversible` flag is set by the bundler during planning and is reviewable during approval. Both cooldown durations are configurable in `settings.json`. Override available for genuine emergencies via `/force-approve` or MCP equivalent; overrides write an audit log entry with the reason and are tracked in calibration.

**Mandatory-review triggers** bypass the matrix entirely. These force full review regardless of complexity and risk: any modification to `AGENTS.md` files, the capability manifest itself, control-plane code or `settings.json`, GitHub Actions workflows or CI config; any new external dependency from a non-allowlisted source; deletion of more than N files (default 10); bundles whose own complexity/risk scoring is suspiciously low for the LOC estimate; and `target: new-repo` (creating a new product repo is an irreversible namespace action and always requires explicit human consent). These are listed in `settings.json` under `mandatory_review_triggers` and are extensible.

**Bundlers may self-escalate** to a higher tier when the score-driven tier feels wrong. They may not self-de-escalate.

### Modification requests

**Modification requests** (`/modify [instructions]` or its MCP equivalent) are the middle path between approve and reject, available when the bundle is in `proposed`, `in_review`, or `approved` (pre-execution). During execution, Redirect is the equivalent verb (see Bundle lifecycle: execution and integration). The flow:

1. Reviewer issues `/modify [instructions]` via any surface.
2. The bundle transitions `in_review → proposed` (or stays in `proposed` if already there).
3. The bundler revises the proposal based on the instructions: re-drafts requirements, RFC, implementation plan, and verification plan as needed; may re-decompose the task DAG.
4. The revised proposal re-enters pre-execution review. If the modification was narrow, review tracks examine only the delta. If broad, full re-review.
5. The revised proposal enters the approval matrix with new scores.

**Modification forces re-score.** Yes, the bundler re-scores on modification. If a modification meaningfully changes the bundle's risk profile, the approval tier should reflect the new risk profile, not the old one. The "bouncing between tiers" concern is a UI problem, not a scoring problem. The surface should show the score delta: "Complexity: 2 → 3 after modification." The transition history makes the bounce visible and interpretable. The `bundle_output.steering_events.modification_count` records modifications; a bundle with more than three pre-execution modifications is surfaced in calibration as a signal of unstable requirements or poor initial bundling.

### Default actions, cooldown durations, and multi-surface race resolution

**Default action for summary-tier timeouts.** PM ratified: default-hold across the board. Regardless of risk cell, the default when the PM does not respond within the configured window is **hold** (require explicit response). This applies until the calibration loop has accumulated enough history to justify auto-approve on specific cells. The PM can change individual cells in `settings.json`:

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

**Cooldown duration.** 1 hour for full-review-cooldown tier, 24 hours for bundles flagged `irreversible`. The `irreversible` flag is a new field on the bundle proposal, set by the bundler when the Verification Plan's rollback plan concludes rollback is not machine-executable and manual recovery would require >1 hour of operator time. In v1.1, with no production and most changes being reversible, the `irreversible` flag will be rare. The flag exists primarily as a design slot for when production becomes real.

**Multi-surface action ordering and race resolution.** First-write wins, second-write fails with a conflict error. The mechanism is SQLite's serialized transaction model. When an approval decision arrives from any surface, the orchestrator opens a SQLite transaction, reads the current bundle state, validates the transition, writes the decision, and commits. Two surfaces racing within the same second are serialized by SQLite's write lock: one commits first, the other sees the updated state and rejects the transition. The semantics exposed to the reviewer: the last decision chronologically is authoritative, provided it's a legal transition from the state at the moment it executes. The audit trail preserves the full sequence. The system does not attempt to merge or reconcile contradictory decisions. If a decision arrives and finds the bundle in a state where that decision is no longer legal, the surface receives an error response with enough context that the reviewer understands what happened: "Bundle <id> is in state <current>, cannot <action>. (It was <previous> when you loaded it; a decision from <other_surface> at <timestamp> changed it.)"

### Calibration loop

**Calibration loop.** After every bundle completes (success or failure), the orchestrator records pre-execution complexity and risk scores plus the actual outcome (success/failure, time taken, lines actually changed, tests added, rework cycles, post-merge issues). Deltas go into `memory/calibration/scoring-outcomes.jsonl`. Periodically, the orchestrator surfaces patterns ("Bundles scored complexity=3 actually averaged 5.2 — bundler under-estimating"), and the reviewer adjusts weights in `settings.json`. The post-mortem prompt fires when any tracked axis diverges from prediction by more than 50%.

## Bundle lifecycle: execution and integration

Once a bundle is approved, the orchestrator transitions it to `approved`, then to `in_progress`, and execution begins. The mechanics of how the executor drives the task DAG (node lifecycle, scheduling, ready-set computation, gate and aggregator semantics, dynamic expansion, retry policies, crash recovery) are specified in the DAG executor section. This section covers the lifecycle-level concerns above the executor: the output schema, the `target:` field and two-tier repo boundary, the full bundle state machine, mid-flight steering mechanics (Pause, Redirect, Abort, Rollback), the post-execution verification handoff, and the structural concerns of decomposition, state sharing, source trees, and integration.

### Bundle output schema

The bundle output is the typed record of everything the bundle produced. It is written incrementally during execution and finalized at terminal state. Consumers: the calibration loop (`memory/calibration/scoring-outcomes.jsonl`), the post-mortem prompt, the approval surface (rendered differently per tier), and future bundles that consult memory.

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

**Population lifecycle:**

**At proposal time** (before the bundle reaches the approval matrix): the `proposal` block is fully populated by the bundler. The `identity.bundle_id` and `identity.created_at` are already set by the orchestrator from input receipt. All other blocks are empty or null.

**During execution**: the `calibration` block fields are updated after each worker completes (loc, duration, tokens, retries, expansions are accumulated). The `steering_events` block is updated on each mid-flight reviewer action. The `cost` block is accumulated as workers consume resources. These are written to the `bundles` row's `outcome_json` column on each update, inside the same SQLite transaction as the triggering event.

**At terminal state**: all remaining fields are populated. `outcome`, `product_artifacts`, `artifact_manifest`, `verification`, and `memory_pointers` are finalized. `identity.completed_at` and `identity.total_wall_clock_seconds` are set.

**Approval surface rendering per tier:**

The approval surface (MCP resource `studio://bundles/{id}`, GitHub Issue body, CLI `studio show`) renders different subsets of the output depending on tier:

- **Auto-approve tier**: `bundle_id`, `created_at`, `proposal.complexity_score`, `proposal.risk_score`, `proposal.target`, `proposal.concerns` (truncated to first 3). A single sentence summary: "Bundle <id> auto-approved: <target> change scored C=<n> R=<n>."
- **Summary tier**: All of `proposal` block. `outcome` if terminal. `calibration` divergence flags if any. `verification.outcome` if complete. Does not include full `cost` breakdown, full `artifact_manifest`, or `memory_pointers`.
- **Full review tier**: The complete `bundle_output`, including all pre-execution track findings (adversarial, security, verification plan) inlined into the proposal body. The reviewer sees every field.
- **Full review with cooldown**: Same as full review, with the cooldown timer displayed prominently.

### The `target:` field

The `target:` field declares where the bundle's output lands. Three values: `new-repo`, `existing-repo:<name>`, `control-plane`. This section specifies the decision rule, the control-plane/product boundary, the mechanics for each value, approval matrix interaction, and cross-target policy.

#### Decision rule: how `target:` is set

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
        if is_new_product and not modifies_existing:
            return ("new-repo", "bundle creates a new deployable product")
        elif modifies_existing and not is_new_product:
            repo = resolve_existing_repo(proposal)
            return (f"existing-repo:{repo}", f"bundle modifies existing repo '{repo}'")
        elif is_control_plane_only:
            return ("control-plane", "all changes are internal to the control plane")
        else:
            raise AmbiguousTargetError(
                "cannot determine target automatically",
                candidates=["new-repo", "control-plane", "existing-repo:..."]
            )

    # Step 3: hint provided, check coherence
    if hint == "new-repo":
        if modifies_existing and not is_new_product:
            return ("existing-repo:...", "target_hint was 'new-repo' but bundle modifies existing repo; overridden")
        return ("new-repo", "matches target_hint: creates new deployable product")

    elif hint.startswith("existing-repo:"):
        repo_name = hint.split(":", 1)[1]
        if not repo_exists_in_registry(repo_name):
            raise InvalidTargetError(f"target_hint references non-existent repo '{repo_name}'")
        return (hint, f"matches target_hint: modifies '{repo_name}'")

    elif hint == "control-plane":
        if not is_control_plane_only:
            raise AmbiguousTargetError(
                "target_hint is 'control-plane' but proposal includes non-control-plane changes",
                candidates=["control-plane", "new-repo", "existing-repo:..."]
            )
        return ("control-plane", "matches target_hint: all changes are control-plane")
```

**Classification helpers:**

`proposal_creates_new_deployable_unit(proposal) -> bool` returns True when the proposal's primary output is a new service, frontend, CLI tool, or other self-contained deployable. The bundler makes this call by analyzing the requirements: if the proposal describes a thing that has its own deploy step, its own port, its own data store, or its own user-facing surface distinct from existing products, it's a new deployable unit. Ambiguous cases are escalated to the reviewer.

`references_existing_repo_in_registry(proposal) -> bool` returns True when the proposal explicitly names a repo from `memory/products/registry.json` as a modification target. The bundler checks the registry during planning.

`all_changes_are_control_plane_content(proposal) -> bool` returns True when all files the proposal plans to touch are classified as control-plane content per the boundary specification below.

`resolve_existing_repo(proposal) -> str` returns the repo slug. If the proposal references exactly one existing repo, that slug. If it references multiple, the bundler escalates (cross-target not supported).

**When the bundler cannot determine the target**, it raises `AmbiguousTargetError` which is surfaced to the reviewer as a concern. The reviewer resolves by providing a specific target during approval or by issuing `/modify target: <value>`. The bundle stays in `in_review` until the target is resolved.

#### Control-plane vs. product content boundary

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

#### Mechanics per value

##### `new-repo`

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
     "private": true,
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

8. **Artifact publication.** The worker publishes a `new-repo-result` artifact: `{slug, url, clone_url, created_at}`, referenced by subsequent worker tasks in the same bundle that need to operate on the new repo.

9. **Subsequent workers.** The remaining worker tasks in the bundle's DAG target the new repo. They clone it (via the GitHub App token), create per-worker branches off `main`, and follow the standard execution flow from Execution structure.

**If the bundle is aborted after repo creation:** the repo is left in place with its scaffold and any partial feature branches. The registry entry's `status` is set to `abandoned`. A follow-up bundle can target the repo with `target: existing-repo:<slug>`.

**New repo README reference to originating bundle:**
```markdown
# <product-name>

<product-description>

---
*Created by [bundle <bundle-id>](<control-plane-repo-url>/issues/<issue-number>)*
```

##### `existing-repo:<name>`

**Repo name resolution.** The `<name>` is resolved against `memory/products/registry.json` by exact match on `product_slug`. If the name does not exist in the registry, the orchestrator rejects the bundle at planning time with error: `INVALID_TARGET: repo "<name>" not found in memory/products/registry.json`. The registry is authoritative; a repo that exists on GitHub but not in the registry cannot be targeted.

**Execution flow.** Workers operate in the target product repo using the same pattern as the control-plane flow:
- A bundle base branch is created off the target repo's default branch (`main`), named `bundle/<bundle-id>`.
- Each worker gets its own worktree on a sub-branch: `bundle/<bundle-id>/worker-<n>`.
- DAG-order merging proceeds identically: workers read from the merged state of their predecessors.
- Final integration merge goes to `bundle/<bundle-id>`.
- On verification pass, the bundle branch is merged to `main` via a PR (same as control-plane flow).

**What's different from control-plane flow:** Nothing mechanical. The repo is different, the permissions are the same (the GitHub App has access to all repos in the org), and the worker lifecycle is identical. The difference is semantic: `existing-repo` bundles are product changes, not control-plane changes, so mandatory-review triggers for control-plane modification do not apply. The repo's own security-sensitive path patterns (from `settings.json`) determine whether the bundle gets the auth/billing/secrets/PII elevated review.

##### `control-plane`

**Execution flow.** The bundle operates against the control-plane repo itself. Mechanically identical to `existing-repo:<control-plane-slug>` except:

1. **Pre-execution snapshot.** Before the first worker task begins, the orchestrator creates a `control-plane-snapshot` global artifact containing the full state of the control-plane repo at that moment (a tarball or git bundle of the current HEAD). This is stored with extended retention (90 days) and is distinct from git history, providing a clean rollback baseline.

2. **Mandatory-review triggers.** In addition to the existing "modification to control-plane code or `settings.json`" trigger, the following are also mandatory-review for control-plane bundles:
   - Any modification to `AGENTS.md` at the control-plane repo root
   - Any modification to `memory/capabilities/manifest.md`
   - Any modification to agent prompt templates (`prompts/*.md`, `prompts/*.yaml`)
   - Any modification to worker base-image Dockerfiles (`docker/*`)
   - Any modification to `templates/new-product-repo/`

3. **No auto-ship.** Control-plane bundles can never auto-ship, regardless of complexity and risk scores. This is enforced by the approval matrix evaluator.

#### Approval matrix interaction

**`target: new-repo` is a mandatory-review trigger.** Added to the `mandatory_review_triggers` list in `settings.json`. Rationale: creating a repository is an irreversible namespace action (deleting a repo burns the URL and fragments clone history), changes the org's repo inventory permanently, and should always require explicit human consent.

**`target: control-plane` triggers the existing mandatory-review rule** for "modifying control-plane code or `settings.json`." The additional control-plane triggers listed above extend this rule. All control-plane bundles are full human review, never auto-ship.

**`target: existing-repo:<name>` does not trigger additional mandatory review** beyond what the bundle's content triggers (auth, billing, secrets, PII, etc.). The repo's security-sensitive path patterns in `settings.json` govern.

#### Cross-target bundles: rejected for v1.1

A bundle with a single `target:` value cannot modify files in both the control-plane and a product repo, nor in two product repos. This is an explicit constraint.

**What the bundler does when an idea naturally spans both:**

1. The bundler detects the cross-target scope during planning (the classification step in the decision rule algorithm).
2. The bundler does NOT silently split the idea. It surfaces the cross-target scope as a concern: "This idea spans both the control-plane (adding a capability) and the api repo (using the capability). The system requires single-target bundles. Recommended: split into two bundles and link via related_bundle_ids."
3. The reviewer sees the concern during approval. The reviewer can:
   - Accept the recommendation: reject this bundle with `/reject split into two` and file two separate inputs.
   - Force a single target: `/modify target: control-plane` to scope only the control-plane work, deferring the product work.
   - Override: in v1.1, there is no override for cross-target. The system rejects cross-target bundles at schema validation.

**Rationale.** Cross-target bundles complicate every lifecycle operation: approval (which repo's triggers?), execution (workers span repos, integration doesn't exist), rollback (rolling back one repo's changes while leaving the other creates an inconsistent state). The complexity is disproportionate to the use case volume in v1.1. The two-bundle workaround with `related_bundle_ids` covers the common case (capability plus first use).

**Escape hatch.** A `control-plane` bundle may modify `memory/products/<slug>/agent-overrides.yaml`, which is product-specific configuration stored in the control-plane. This is the only sanctioned cross-cutting modification. The escape hatch is narrow and deliberately documented so the implementing agent doesn't generalize it.

### Bundle state machine

The bundle state machine is the authoritative source for what transitions are legal, what triggers them, and what side effects they produce. It is implemented as a single Python class in the orchestrator core, `BundleStateMachine`, with one method per legal transition. Each method validates the current state, performs the transition inside a SQLite transaction, writes audit entries, and enqueues any required events to the executor's event pump.

**States.** Twelve states. Each is a string enum value stored in `bundles.state`.

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

**Transition table.** Each row is `(from_state, trigger, to_state, actor, side_effects)`. Every transition is one SQLite transaction.

| # | From | Trigger | To | Actor | SQLite writes | Event enqueued |
|---|------|---------|----|-------|---------------|----------------|
| 1 | (none) | `bundle_input_received` | `PROPOSED` | Orchestrator | INSERT `bundles` row, INSERT `audit_log` | (none) |
| 1a | `PROPOSED` | `kernel_direct_approval` | `APPROVED` | Reviewer | UPDATE `bundles.state`, UPDATE `bundles.approved_at`, UPDATE `bundles.approved_by`, INSERT `approval_decisions`, INSERT `audit_log` | (none) |
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
| 14 | `REDIRECTING` | `replan_completed` | `IN_REVIEW` | Bundler agent | UPDATE `bundles.state`, UPDATE `bundles.proposal_json` (new proposal), INSERT `audit_log`, INSERT re-plan provenance record | `review_tracks_dispatched` |
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

**Transitions requiring special attention:**

**Transition 1a** (`PROPOSED → APPROVED`) [PHASE-1-ONLY]: This transition exists in v1.1 Phase 1, when the bundler agent is not yet implemented. It allows the human reviewer to approve a bundle directly from the CLI surface (`studio approve <id>`) without the bundler producing a proposal or pre-execution review running. The transition is removed in Phase 2 when the bundler exists; at that point `bundler_completed` (transition 2) becomes the only legal path out of `PROPOSED`. The state machine validates this transition only when a `kernel_mode` flag is set at orchestrator startup; in full system mode the transition is treated as illegal with reason "Bundle has not been reviewed. Wait for pre-execution review and approval."

**Transition 3** (`IN_REVIEW → PROPOSED`): Triggered when a review track finds a blocking issue and the bundler must revise. Not the same as `/modify` (which is reviewer-initiated). The review track agent sets `bundles.concerns_json` with the blocking finding.

**Transition 8** (`IN_PROGRESS → PAUSED`): The `bundle_pause_requested` event triggers the Pause executor mechanics (see Mid-flight steering). The transition itself commits immediately; the pause may take seconds-to-minutes for in-flight workers to finish their current step.

**Transition 14** (`REDIRECTING → IN_REVIEW`): The re-plan provenance record captures the relationship:
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

**Transition 20** (`VERIFYING → IN_PROGRESS`): Auto-rollback. The bundle enters `IN_PROGRESS` because rollback execution is happening. If rollback completes, the bundle transitions to `FAILED`. If rollback itself fails, the bundle transitions to `FAILED` with `rollback_failed: true` in `outcome_json`.

**Transition 22** (`COMPLETE → COMPLETE`): Rollback of a shipped bundle. The bundle stays `COMPLETE` (it was shipped; the net is zero after rollback). The audit trail records the rollback.

**Transition 25** (`IN_PROGRESS → FAILED`): Unrecoverable DAG failure during execution (not verification failure). Per the executor design, in-flight workers are NOT auto-cancelled. The reviewer can issue Abort to clean up.

**Illegal transitions.** The state machine validates every requested transition. Illegal transitions raise `IllegalTransitionError`:
```python
class IllegalTransitionError(Exception):
    def __init__(self, current_state: str, attempted_transition: str, reason: str):
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        self.reason = reason
```

Serialized for external surfaces as:
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

The state machine validates by checking membership in a frozenset of `(from_state, to_state)` tuples. Common illegal transitions:

| Attempted | Reason template |
|-----------|----------------|
| Any transition from a terminal state | "Bundle is {state}; transitions from terminal states are not allowed." |
| `PROPOSED → IN_PROGRESS` | "Bundle has not been reviewed. Wait for pre-execution review and approval." |
| `APPROVED → PROPOSED` | "Bundle is approved. Use /modify to revise before execution starts." |
| `IN_PROGRESS → APPROVED` | "Bundle is executing. Pause first, then Redirect to re-plan." |
| `PAUSED → VERIFYING` | "Bundle is paused. Resume or Redirect first." |

**Surface observability.** SQLite: `bundles.state` is the authoritative source, updated in the same transaction as the transition. MCP: `studio://bundles/{id}` reads state directly. GitHub Issues: on every state transition, the bundle's issue is updated — body appends a transition timeline entry `"[{timestamp}] {from_state} → {to_state} by {actor} via {surface}"` and labels are swapped (old state label removed, new added: `state/proposed`, `state/in-review`, etc.). CLI: `studio show <bundle-id>` displays current state and transition history from `audit_log`.

**Implementation notes.** The state machine does NOT mutate executor state directly. It enqueues events (`bundle_pause_requested`, `bundle_abort_requested`, etc.) into the executor's event queue. The executor's event pump picks up these events on the next tick and performs the executor-level actions. The bundle state machine and the DAG executor communicate through the event queue and SQLite, not through direct method calls. The two state spaces (bundle state and DAG node states) are in separate tables and are not locked together.

The implementing agent defines all 12 enum values in `BundleState` (Python `StrEnum`) even though Phase 1 implements transition handlers for only a subset. The unused values (`redirecting`, `verifying`, `parked`) are valid future states defined in the schema. Their presence at definition time prevents a migration when Phase 2 adds the bundler, QA agent, and redirecting machinery. Phase 2 adds transition handlers; it does not add new enum values.

### Mid-flight steering mechanics

All four verbs (Pause, Redirect, Abort, Rollback) are PM-ratified. The state machine and executor communicate through the event queue and SQLite, not through direct method calls.

#### Pause

**Trigger:** Reviewer issues Pause from any surface. Legal from `IN_PROGRESS` only.

**Complete sequence:**

1. **State machine transition.** `bundles.state` transitions `IN_PROGRESS → PAUSED`. Commits immediately. `steering_events` in `outcome_json` records: `{action: "pause", at: <ISO8601>, by: <actor>, note: null}`. `bundle_pause_requested` event enqueued.

2. **Executor receives event.** On next tick, the event pump processes `bundle_pause_requested`:
   - Identifies all `dag_nodes` rows for this bundle in state `running`.
   - Sends `worker.pause()` RPC to each such worker.
   - Sets a `scheduler_halted` flag in the executor's in-memory state for this bundle (prevents new dispatches).
   - The event pump continues to process other events (worker completions still arrive).

3. **Worker behavior.** On receiving `worker.pause()`:
   - The worker finishes its current step (the in-progress tool call, test run, file write, or LLM API call).
   - The worker does NOT checkpoint mid-step. Rationale: mid-step checkpointing requires workers to serialize and reconstitute state, which is more coupling than the kill-all-on-crash policy accepts.
   - After completing the current step, the worker calls `worker.final_report` with a `{"paused": true, "current_phase": "<phase>", "progress": {...}}` payload, then halts.
   - The worker does NOT exit the process. It stays alive, connected to the RPC channel, waiting for `worker.resume()`.

4. **Worker state updates.** As each worker completes its pause: `dag_nodes.state: running → paused`. `node_state_history` records: `{from_state: "running", to_state: "paused", reason: "bundle_paused"}`.

5. **Surface state.** While workers are finishing their steps, the surface shows `PAUSED` state. The GitHub Issue body appends: "Pause requested. Waiting for N workers to finish current steps..." When all workers have paused: "Paused. All workers idle."

**What is preserved:** All worker worktrees. All `dag_nodes` rows with current states. The SQLite database state. The bundle's feature branch.

#### Resume (from Pause)

**Trigger:** Reviewer issues Resume from any surface, optionally with a note string. Legal from `PAUSED` only.

1. **State machine transition.** `bundles.state` transitions `PAUSED → IN_PROGRESS`. `steering_events` records: `{action: "resume", at: <ISO8601>, by: <actor>, note: "<note or null>"}`. `bundle_resume_requested` event enqueued.

2. **Executor receives event.** On next tick:
   - Reads the `steering_events` note from `outcome_json`.
   - For each `dag_nodes` row in state `paused`, sends `worker.resume()` RPC.
   - Removes `scheduler_halted` flag.
   - Re-ticks the scheduler: recomputes ready set from current node states.

3. **Worker behavior.** On receiving `worker.resume()`: reads its current task spec. If the executor's note is present, the worker prepends it to its working context (visible to the LLM on the next prompt). Resumes execution from where it paused.

**Note delivery mechanism.** Notes are delivered via the `worker.resume()` RPC response, not via `worker.inject_context`. The `resume` RPC returns `{note: "<note or null>"}` alongside the standard ack.

#### Redirect

**Trigger:** Reviewer issues Redirect from any surface with a `new_instructions` string. Legal from `PAUSED` only. If bundle is in `IN_PROGRESS`, reviewer must Pause first.

**Ratified design:** "Discard current DAG, run planner on current worktree state as fresh bundle, completed work as baseline."

1. **State machine transition.** `bundles.state` transitions `PAUSED → REDIRECTING`. `steering_events` records: `{action: "redirect", at: <ISO8601>, by: <actor>, note: "<new_instructions>"}`. `bundle_redirect_requested` event enqueued.

2. **Snapshot current state.** The executor produces a `bundle-state-snapshot` artifact containing:
   - A git tree-ish reference: the merged state of all completed worker sub-branches, resolved to a concrete commit SHA. If no workers completed, the bundle base branch HEAD.
   - A JSON manifest of all `dag_nodes` rows in terminal states (`completed`, `failed`, `skipped`, `cancelled`), each with: `node_id`, `terminal_state`, `output_artifacts`, and `branch_ref`.
   - The prior DAG structure (nodes and edges) for the audit trail.

3. **Re-planning dispatch.** The orchestrator spawns a new planning task within the existing bundle's identity. The planner agent receives:
   - The original `bundle_input`.
   - The reviewer's `new_instructions` (from the redirect steering event).
   - The `bundle-state-snapshot` artifact reference.
   - Memory context (calibration data, prior decisions) same as original planning.

4. **Planner produces new DAG.** The planner evaluates which completed work is reusable. If the new instructions invalidate prior work, the new DAG includes replacement workers. The planner decides; the reviewer can override during re-approval.

5. **Capability manifest constraint.** The new DAG's node capability manifests must be subsets of the original bundle manifest. If the new instructions require capabilities beyond the original manifest, the planner must request them via the capability-request flow. Redirect does not silently expand the capability envelope.

6. **State machine transition.** On planner completion: `REDIRECTING → IN_REVIEW`. The new proposal replaces the old one in `bundles.proposal_json`. The re-plan provenance record (Transition 14) is written. `review_tracks_dispatched` event enqueued (abbreviated review: only the delta examined, unless review track agents self-escalate).

7. **Approval and resume.** The new DAG goes through the approval matrix. Bundle transitions `IN_REVIEW → APPROVED → IN_PROGRESS` and execution resumes with the new DAG.

**What "current worktree state" means concretely:** `git merge-base --octopus <worker-1-branch> <worker-2-branch> ...` to find the common ancestor of all completed work. If merge-base fails (rare), falls back to the last successfully merged integration point.

#### Abort

**Trigger:** Reviewer issues Abort from any surface. Legal from `APPROVED`, `IN_PROGRESS`, `PAUSED`, `REDIRECTING`. Abort from `PROPOSED` or `IN_REVIEW` is treated as Reject (Transition 5).

1. **State machine transition.** `bundles.state` transitions to `ABORTED`. `bundles.completed_at` set. `bundles.outcome_json` populated with `{outcome: {status: "aborted", rationale: "<reason>"}}`. `steering_events` records: `{action: "abort", at: <ISO8601>, by: <actor>}`. `bundle_abort_requested` event enqueued.

2. **Cancellation order: all simultaneously**, not reverse-DAG-order. The executor sends `worker.cancel(reason="bundle_aborted")` RPC to every worker with an active connection.
   - 30-second grace period for worker to finish current step and commit.
   - SIGTERM after grace.
   - SIGKILL 10 seconds after SIGTERM.
   - Workers in `ready`/`pending` (not yet spawned) transition directly to `cancelled` without RPC.
   - Workers in `paused` (already idle) transition to `cancelled` without grace period.

3. **Draft PR handling.** If the bundle opened draft PRs, the orchestrator closes them via `PATCH /repos/{org}/{repo}/pulls/{number} {"state": "closed"}` with comment: "Bundle aborted by reviewer. Feature branch `<branch>` is preserved for recovery."

4. **Explicit preservation/revocation decisions:**
   - `artifact_refs` rows: **preserved.** Remain until the aborted-bundle retention window expires and periodic GC sweep collects them.
   - Capability grants made specifically for this bundle: **revoked.** `capabilities.revoked_at` and `capabilities.revoke_reason = "bundle_aborted"` set.
   - `audit_log` entries: **never deleted.** Full audit trail from creation to abort is immutable.
   - Git branches: feature branch and worker sub-branches remain. Not deleted by the system.
   - In-flight RPC calls: abandoned. Workers killed via SIGKILL lose uncommitted working state.

#### Rollback

**Design decision: rollback is a new bundle**, not a special bundle kind, not a direct orchestrator action.

**Rationale.** Rollback is a software change like any other: it touches the same repos, code paths, and deployment mechanisms as the original work. It needs capability grants, a task DAG, worker execution, and verification. A rollback that fails is itself a failed bundle with its own post-mortem. This handles trivial and nontrivial rollbacks uniformly.

**Trigger paths:**

1. **Manual.** Reviewer issues Rollback: `studio rollback <bundle-id>`. Creates a `bundle_input` with `parent_bundle_id = <bundle-id>`, `filed_via = <cli|mcp|github_issue>`. Enters normal bundle lifecycle.

2. **Automatic on QA verification failure.** Triggered when ALL of:
   - Verification Report outcome is `failed` or `partial`
   - Bundle's stakes are `Low`
   - Verification Plan's `rollback_plan.machine_executable` is `true`
   - Verification Plan's `rollback_plan.auto_rollback_eligible` is `true`
   
   When all conditions met, the orchestrator auto-creates the rollback bundle input (`filed_by = "system"`, `filed_via = "agent_generated"`). Auto-approved and executes immediately.

**What the rollback executor runs:** The rollback bundle's bundler reads the original bundle's rollback plan:
```yaml
rollback_plan:
  machine_executable: bool
  auto_rollback_eligible: bool
  steps:
    - description: str
      kind: git_revert | api_call | deploy_previous | manual
      spec: { ... }
```
The bundler translates this into a task DAG. For `git_revert`, the worker runs `git revert <merge-commit>`. For `deploy_previous`, the worker re-deploys the previous known-good artifact. The bundler is not bound by the plan; if the plan says "revert commit X" but commit X has conflicts, the bundler proposes an alternative.

**How rollback is verified:** The rollback bundle gets its own Verification Plan. The QA agent checks: the rolled-back state matches pre-original-bundle state for touched files, unrelated changes are preserved, and the rolled-back product passes the same CI and smoke tests.

**Terminal states after rollback:**
- **Successful:** Original bundle stays `COMPLETE`. Rollback event appended to its `steering_events`. Rollback bundle completes with `outcome: shipped`.
- **Failed:** Original bundle annotated with `rollback_failed: true`. Reviewer paged. Rollback bundle completes with `outcome: failed`.
- **Auto-rollback:** Original bundle transitions `VERIFYING → IN_PROGRESS` (Transition 20) during rollback, then to `FAILED` when rollback completes. Original bundle's `outcome_json.verification.rollback_triggered` is `true`.

### Post-execution verification handoff (QA dual-use seam)

The QA agent's dual use: pre-execution, it produces a Verification Plan; post-execution, it runs the plan against the actual shipped artifact. The seam between the two uses:

**Artifacts at the seam.** Pre-execution produces the Verification Plan, stored as `{namespace: bundle, name: verification-plan, version: v1, content_type: application/json}`. The plan includes: acceptance criteria, test surface, pre-merge gates, post-ship verification metrics, and a rollback plan (machine-executable boolean, steps, auto-rollback eligibility). Post-execution receives: the Verification Plan artifact, the merged bundle branch, the worker reports, the CI run results, and access to the deployed artifact. It produces a Verification Report: `{namespace: bundle, name: verification-report, version: v1, content_type: application/json}`. The report includes: per-criterion pass/fail with evidence, aggregate outcome (passed, failed, partial), failed criteria with descriptions, coverage gaps, and a rollback recommendation.

**Verification failure handling.** When the Verification Report's outcome is `failed` or `partial`: the bundle transitions `verifying → failed`. A `status:verification-failed` label is applied to the GitHub Issue, and the bundle enters the Human Review Board's Needs Input column. If the Verification Plan declared auto-rollback eligibility and stakes are Low and rollback is machine-executable, the orchestrator spawns a rollback bundle automatically. Otherwise, the reviewer decides: spawn a rollback bundle manually (`studio rollback <bundle-id>`), park the bundle, or kill it. The reviewer can also Retry (re-trigger verification, which is idempotent) or Override (mark the bundle `complete` despite verification failure, with an audit log entry explaining the override). Override is a deliberate "I accept the risk" decision tracked in calibration.

**The Verification Report as calibration data.** If the QA agent consistently produces plans that pass verification but the shipped product has bugs discovered later, the QA agent's plans are under-testing. If it produces plans that fail on criteria that turned out to be irrelevant, it is over-testing. Both are calibration signals. The Verification Report includes a `plan_quality_self_assessment` field, stored alongside scoring calibration data in `memory/calibration/`.

### Execution structure

**Decomposition is hybrid: static DAG with bounded dynamic expansion.** Each bundle proposal includes a planned task DAG. Workers can request expansion mid-execution, but the request goes through the orchestrator's approval flow, not through the worker spawning children directly. This preserves the property that the orchestrator is the spawn authority and keeps the capability model clean. It also keeps the DAG inspectable (a human reviewer sees the planned envelope; expansions stay within or escalate). Pure static decomposition was rejected because real coding work doesn't always decompose cleanly up front. Pure dynamic spawning was rejected because it makes resource usage hard to reason about, makes capability boundaries hard to enforce, and makes human review nearly impossible.

**Coordination topology is a star.** The orchestrator is the hub. Workers do not communicate with each other directly. Artifact handoff between workers goes through the orchestrator: worker A produces an artifact, calls `artifact.publish(descriptor, data)`, the orchestrator stores it; worker B calls `artifact.fetch(descriptor)` and gets it. The orchestrator mediates every cross-worker exchange and can enforce capabilities ("worker B is allowed to read artifacts from worker A") and audit them.

Peer-to-peer worker communication was considered and rejected. It would break the capability model, since the orchestrator could no longer mediate cross-worker action. It would also explode the security surface, because every worker would become a server, not just a client. It would make k8s deployment harder, since worker pods would need to be addressable, which Jobs aren't naturally.

**State sharing is per-worker filesystem isolation, with the artifact RPC as the canonical channel.** Each worker has its own working directory; sharing happens explicitly via `artifact.publish` and `artifact.fetch`. A shared filesystem across workers was rejected because on k8s it would require ReadWriteMany PVCs (only available on certain storage classes), creating surprise behavior differences between local and cluster deployments. Per-worker isolation forces explicit declaration of inter-worker dependencies (matches the DAG model, good for auditability), eliminates the "two workers stepped on each other's files" failure mode, and maps cleanly to the capability model (artifact reads and writes are capability-checked operations). The cost: workers can't casually share large state. If that becomes painful, a separate "shared scratch" mechanism with explicit opt-in can be added; not in v1.1.

**Source-tree access is per-worker working tree on per-worker branch off the bundle base branch.** Implementation: git worktrees locally (cheap, fast, single object store, easy garbage collection), full clones on k8s where shared filesystems aren't a given. The branch-per-worker model is the right abstraction either way. Single shared working tree was rejected (parallel edits to the same file are a disaster, and pessimistic locking defeats parallelism). Per-worker tree with a single shared branch was rejected (integration is non-trivial when multiple workers touch overlapping files, and there's no clean place for review).

**Integration is DAG-order branch merging with a bundle-integration step at completion.** Workers work on per-worker branches off the bundle base branch. DAG dependencies define merge order: a worker that depends on worker A's output gets A's branch merged into its base before it starts. At bundle completion, the orchestrator does a final integration merge of all leaf-worker branches into a single bundle branch. The bundle branch is what gets reviewed and ultimately merged to the target branch. Conflicts during integration are escalated in tiers: orchestrator first tries trivial auto-merge; on failure, an LLM is asked to attempt resolution (which becomes a new worker task with a tight capability scope); if the LLM-resolution worker fails or its result fails tests, escalate to human.

Testing strategy: workers run tests on their own branch (fast feedback during work). The orchestrator runs integration tests on the merged bundle branch before declaring the bundle done.

**Two-tier repo architecture.** The control-plane repo houses bundles, RFCs, decks, decisions, capability requests, and memory. Each shipped product gets its own dedicated product repo, created at execution time by the developer agents. Each product repo is independently versioned, deployable, ownable, and disposable.

Each new product repo gets a templated scaffold from `templates/new-product-repo/` in the control-plane: README, `docs/` (architecture overview, API reference, data model, key decisions), `INSTALL.md`, `DEPLOY.md`, `AGENTS.md` (durable semantic memory for future agents working on this repo), `LICENSE`, `.github/` (issue templates, PR template, CODEOWNERS, branch protection config), `CHANGELOG.md` with the initial release entry auto-generated from the bundle's RFC, a working CI pipeline, and a reproducible deploy mechanism. Updating the template updates all future product repos.

Product is single per repo; multiple components live in subdirectories (`api/`, `web/`, `infra/`, `docs/`) inside a single product repo. Workers operate on subdirectories within a product repo, not across product repos. This was an explicit choice: splitting more aggressively (one component, one repo) would explode operational overhead.

Linkage between control-plane and product repos: the bundle in the control-plane gets a `product-repo:` field populated at repo-creation time; the new product repo's README links back to the originating bundle; control-plane memory stores the mapping in `memory/products/registry.json`. Naming convention is configured in the main `settings.json` (default `${org}/${product-slug}` where slug comes from bundle title); default visibility (private vs. public) is also configured globally.

**Failure handling during execution** uses a tiered policy similar to stakes:

- Low-impact failures (flaky test, transient network blip): auto-retry with backoff.
- Mid-impact failures (dependency conflict, ambiguous requirement requiring clarification): surface to human as Needs Input.
- High-impact failures (production deploy fails, irreversible state corruption detected): immediate rollback plus alert.

Production deploy failures are not currently a relevant case, since there is no production in v1.1. The tier exists in the policy for when production becomes real.

**Worker lifecycle.** Orchestrator picks worker class C for task T based on T's requirements, spawns the worker via WorkerRunner, gives it its own working tree on its own branch, gives it its declared capability set, and gives it pointers to its declared input artifacts. The worker reads its task spec (passed via env var or mounted file): bundle context, RFC excerpt, verification plan excerpt, conditions, `AGENTS.md`, capability manifest subset, model and thinking-mode config. It invokes OpenCode against the configured Ollama Cloud model. It emits heartbeats on state transitions and at maximum 60-minute intervals, including the `phase` field. It may request capability expansion mid-task via RPC; this goes through the approval flow before new workers spawn. On completion (success, failure, or stuck), it commits to its sub-branch, emits a `worker-report.json`, and exits. The orchestrator reviews exit state, merges the sub-branch to the feature branch (or kills it), updates `tasks.json`, and decides next steps.

Workers that go stuck (hit the configured per-worker-class timeout, currently 3 stuck-iterations [PROVISIONAL] as the kill-and-respawn threshold) are killed and reassigned. The reset-and-iterate pattern (small bounded tasks, kill-and-respawn over long-context grinding) was an intentional adoption from the production-tested patterns surveyed during the orchestrator design conversation.

## Surfaces

There are three surfaces into the orchestrator state machine, all reaching the same source of truth, all interchangeable for actions: MCP server (primary), GitHub Issues (secondary), CLI (tertiary).

**MCP server, primary.** Runs as its own process on dev.learhy.net, alongside the orchestrator. Talks to the orchestrator over a Unix domain socket. Exposes remote MCP over HTTPS via Caddy with a long-lived bearer token in the Claude Desktop config. Fallback transport is an stdio bridge running on the laptop that tunnels to the box over SSH (same server, different transport).

This was chosen over GitHub Issues as primary because Claude Desktop is increasingly the reviewer's actual primary surface. MCP makes Claude Desktop a thinking surface, not just a notification surface: the reviewer can interrogate a bundle ("what does worker 3 actually do?", "what's the rollback plan if migration fails?", "show me last week's bundles that touched this subdirectory") before deciding, instead of evaluating a static template.

The MCP method surface:

**Tools (write/action).** All MCP tools require explicit human gesture (tool-confirmation flow). Each tool maps to the orchestrator state machine; the orchestrator's response is the tool's return value.

**`list_pending_bundles`**
```
Input:
{
  "filter": {
    "tier": "auto" | "auto_notify" | "summary" | "full_review" | "full_review_cooldown" | null,
    "state": "<state enum>" | null,
    "repo": "<string>" | null,
    "limit": <int, default 20, max 100>
  } | null
}

Output:
{
  "bundles": [
    {
      "id": "<ulid>",
      "state": "<state enum>",
      "tier": "<tier enum>",
      "target": "<string>",
      "complexity_score": <int 0-10>,
      "risk_score": <int 0-10>,
      "title": "<first line of idea>",
      "created_at": "<iso8601>",
      "approved_at": "<iso8601 or null>"
    }
  ],
  "total": <int>,
  "truncated": true | false
}
```

**`get_bundle`**
```
Input:
{
  "id": "<ulid>"
}

Output:
{
  "bundle": <full bundle_output as specified in Bundle lifecycle: execution and integration>
}
```
Error: `{"error": "NOT_FOUND", "detail": "Bundle <id> does not exist"}`.

**`approve_bundle`**
```
Input:
{
  "id": "<ulid>",
  "comment": "<string or null>"
}

Output (success):
{
  "transition": "IN_REVIEW -> APPROVED" | "APPROVED -> IN_PROGRESS" | "VERIFYING -> COMPLETE",
  "bundle_id": "<ulid>",
  "new_state": "<state enum>"
}
```
Error: `{"error": "ILLEGAL_TRANSITION", "current_state": "<state>", "detail": "<reason>"}`.

**`reject_bundle`**
```
Input:
{
  "id": "<ulid>",
  "reason": "<string>"
}

Output (success):
{
  "transition": "IN_REVIEW -> REJECTED" | "APPROVED -> REJECTED",
  "bundle_id": "<ulid>",
  "new_state": "rejected"
}
```

**`request_modification`**
```
Input:
{
  "id": "<ulid>",
  "instructions": "<string>"
}

Output (success):
{
  "transition": "IN_REVIEW -> PROPOSED",
  "bundle_id": "<ulid>",
  "new_state": "proposed",
  "message": "Bundler will revise based on instructions."
}
```
Legal from `proposed`, `in_review`, or `approved` (pre-execution only). Error: `{"error": "ILLEGAL_TRANSITION", ...}`.

**`escalate_bundle`**
```
Input:
{
  "id": "<ulid>",
  "reason": "<string>"
}

Output (success):
{
  "bundle_id": "<ulid>",
  "new_tier": "<tier enum>",
  "previous_tier": "<tier enum>",
  "message": "Bundle escalated to <tier>."
}
```
Escalates to the next higher tier. Cannot de-escalate. Error if already at `full_review_cooldown`.

**`pause_bundle`** — Legal from `in_progress` only.
```
Input:
{
  "id": "<ulid>"
}

Output (success):
{
  "transition": "IN_PROGRESS -> PAUSED",
  "bundle_id": "<ulid>",
  "new_state": "paused",
  "workers_waiting": <int>
}
```

**`resume_bundle`** — Legal from `paused` only.
```
Input:
{
  "id": "<ulid>",
  "note": "<string or null>"
}

Output (success):
{
  "transition": "PAUSED -> IN_PROGRESS",
  "bundle_id": "<ulid>",
  "new_state": "in_progress"
}
```

**`kill_worker`**
```
Input:
{
  "bundle_id": "<ulid>",
  "worker_id": "<ulid>",
  "reason": "<string>"
}

Output (success):
{
  "worker_id": "<ulid>",
  "action": "cancel_dispatched",
  "message": "Cancel sent to worker."
}
```

**`grant_capability`**
```
Input:
{
  "request_id": "<ulid>",
  "scope": { ... } | null,     // null = grant exactly as requested
  "expiry": "<iso8601 or null>"
}

Output (success):
{
  "capability_id": "<ulid>",
  "granted_scope": { ... },
  "expires_at": "<iso8601 or null>"
}
```

**`revoke_capability`**
```
Input:
{
  "capability_id": "<ulid>",
  "reason": "<string>"
}

Output (success):
{
  "capability_id": "<ulid>",
  "revoked_at": "<iso8601>"
}
```

**Resources (read-only context).** MCP resources are URIs that return typed JSON or Markdown content. All resources are read-only and do not require human gesture.

| URI | Returns | Description |
|-----|---------|-------------|
| `studio://bundles/pending` | `List[BundleSummary]` | All bundles not in terminal state |
| `studio://bundles/{id}` | `BundleOutput` | Full bundle output (same shape as `get_bundle` result) |
| `studio://bundles/{id}/workers` | `List[WorkerSummary]` | Workers for a bundle: id, state, phase, started_at |
| `studio://workers/active` | `List[WorkerSummary]` | All workers in `running` or `paused` state |
| `studio://workers/{bundle_id}/{worker_id}/report` | `WorkerReport` | Final worker report JSON |
| `studio://capabilities/manifest` | `CapabilityManifest` | Current system capability source of truth |
| `studio://capabilities/pending-requests` | `List[CapabilityRequest]` | Pending capability requests with status |
| `studio://memory/agents/{repo}` | `text/markdown` | `AGENTS.md` content for the named repo |
| `studio://calibration/recent` | `List[CalibrationEntry]` | Last 30 days of calibration data |
| `studio://decisions/recent` | `List[DecisionEntry]` | Last 30 days of decisions |
| `studio://system/status` | `SystemStatus` | Orchestrator health, worker pool, Ollama Cloud reachability |

**Prompts (canned interaction patterns).** MCP prompts are templated messages provided to the LLM client. They do not execute any code.

| Prompt name | Arguments | Template purpose |
|-------------|-----------|-----------------|
| `review-pending` | (none) | "Here are the pending bundles. For each, summarize the proposal, flag concerns, and recommend approve/reject/modify." |
| `morning-digest` | (none) | "Here is what happened overnight: completed bundles, new proposals, calibration alerts." |
| `risk-audit` | `repo: string` | "Audit recent bundles targeting <repo>. Identify patterns in risk scoring, failure modes, and security findings." |
| `bundle-deep-dive` | `id: string` | "Do a deep review of bundle <id>: read the full RFC, verification plan, worker decomposition, and all review track findings. Flag anything the automated review may have missed." |

Approval actions always require explicit human gesture: Claude Desktop can recommend, the human must click. This is just MCP's normal tool-confirmation behavior, but it's a deliberate design constraint, not an accident. Claude Desktop is itself an LLM-mediated surface, and asking it "should I approve this?" gets another model's recommendation on top of the bundler's proposal. That's mostly a feature (independent review), but the action itself stays explicit.

The MCP tools are designed to make it easy for Claude Desktop to surface its own uncertainty back to the human, not paper over it. Tool outputs include the bundler's concerns section verbatim; resources include calibration data so Claude can say "the bundler has been under-estimating complexity by ~30% lately, factor that in."

**GitHub Issues, secondary.** Each bundle proposal opens an issue in the control-plane repo with a structured template. Labels indicate tier (`approval/auto`, `approval/summary`, `approval/full-review`, `approval/cooldown`). Decisions can be expressed via comments (`/approve`, `/reject [reason]`, `/full-rfc`, `/modify [instructions]`). The orchestrator watches issue events via webhook and acts accordingly. This surface serves three roles: durable audit trail of record (every MCP action mirrors as a comment on the corresponding issue), mobile fallback (when the reviewer is away from Claude Desktop), and alternate write path.

**CLI, tertiary.** `studio approve <bundle-id>`, `studio list-pending`, `studio show <bundle-id>`. Useful when the reviewer is SSH'd into the box. Doesn't replace the issue or MCP flow but supplements it. Both surfaces produce the same orchestrator action.

**Cross-surface sync.** MCP actions mirror to GitHub Issues as comments (audit trail); GitHub Issue actions mirror to the orchestrator's decision log. Multi-surface action ordering and race resolution follow the first-write-wins policy specified in Bundle lifecycle: planning and approval, Multi-surface action ordering and race resolution: SQLite serialization ensures one decision commits first; the second surface receives a conflict error with enough context to understand what happened.

**Notifications, v1.** GitHub native only, deliberately simplest. Workers assign the reviewer to issues requiring attention; GitHub's email and mobile push handle delivery. Assignment triggers: bundle enters Needs Input column; bundle gets `acting-soon` label. No other transitions trigger assignment. Pull-based for everything else.

Architectural hooks for future expansion (zero-cost to bake in now):

- Workers include a structured `<!-- notify-reason: {reason} -->` HTML comment in every assignment-triggering comment.
- All notification-worthy events append to `memory/notifications/log.jsonl` with timestamp, bundle-id, reason, channel.
- All workers route notifications through a single `notify()` helper, not direct assignment calls. The helper today does GitHub assignment plus log append; future versions can branch on urgency to add digest, relay, or SMS without changing worker code.

Future expansion candidates (not built in v1): daily digest issue; tiered urgency rules; external channel relay via webhook consumer; trigger to add for Needs Input aging > 24h re-ping; trigger to add for medium-stakes bundle aging > 5 days in Review Queue.

**Capability Requests Board.** A separate GitHub Project, distinct from the Human Review Board and the Agent Activity Board. Tracks agent-initiated capability upgrade requests across the categories defined by the capability gap tiering: blocking, degrading, friction (with friction reports aggregated above a configurable threshold). Decision vocabulary is Grant, Grant-with-constraints, Defer, Deny, Investigate. Note that "Approve with conditions" (bundle approval surface) and "Grant with constraints" (capability surface) are both narrow-the-scope-and-proceed verbs for different surfaces; they're parallel but distinct. Same shape, different surface.

The capability gap tier configuration lives in the main config file. Default behavior is to surface all three tiers (blocking, degrading, friction); the reviewer accepted being swamped initially in exchange for not under-reporting. Aggregation thresholds for friction-pattern surfacing are deferred (a reasonable starting heuristic is 3 reports in 7 days, but not committed).

## Persistence and audit

The system maintains state in two places: SQLite for operational state (the orchestrator's working memory) and the `memory/` directory tree for durable artifacts and long-term audit. They're complementary. SQLite is hot; `memory/` is forensics-grade.

**SQLite (`/var/lib/studio/state.db`)** holds operational state per the schema sketched in the architecture section: bundles, workers, capabilities, capability requests, approval decisions, capability checks, and a catch-all audit log. The DAG executor adds tables for DAG node and edge state, node state history, expansion provenance, the unified approval-request lifecycle, and artifact-publication references; those are specified in the DAG executor section. Single-process writer (orchestrator core), multiple-process readers (MCP server). WAL mode for concurrency. Atomic transactions across multi-table state changes. File-copy backups.

**`memory/` directory tree.** The durable layer, organized by purpose:

- `memory/decisions/` — every decision (ship, revise, spike, park, reframe, kill) with full reasoning. Bundler reads on every new bundle to surface relevant prior decisions.
- `memory/killed/` — full archived bundles for killed ideas; updates the duplicate-detector index.
- `memory/post-mortems/` — divergence post-mortems on shipped bundles.
- `memory/calibration/` — aggregated estimated-vs-actual data for bundler correction. Tracked axes: code surface estimated vs. actual, build cost estimated vs. actual, ongoing cost estimated vs. actual (sampled at 7d and 30d post-ship), agent-iteration count predicted vs. actual, blast-radius predicted vs. realized, predicted impact vs. observed impact (sampled per the bundle's own metrics).
- `memory/notifications/log.jsonl` — append-only notification event log.
- `memory/capabilities/manifest.md` — current capability source-of-truth.
- `memory/capabilities/requests/` — historical capability requests with decisions.
- `memory/capabilities/usage-log.jsonl` — capability usage events (which worker, which bundle, which capability, when).
- `memory/capabilities/reviews/` — periodic review outcomes (was this granted capability worth it?).
- `memory/capabilities/rate-limit-observations.jsonl` — Ollama Cloud rate-limit signals from workers, used for adaptive spawn rate.
- `memory/security-findings/` — historical security findings and resolutions; future security agents read for pattern detection.
- `memory/verification-plans/` — verification plans and post-ship verification outcomes; QA agent reads to calibrate plan quality over time.
- `memory/audit/credential-use.jsonl` — every secret name (not value) accessed, per worker, per task.
- `memory/executions/<bundle-id>/<worker-id>/report.json` — final worker reports.
- `memory/executions/<bundle-id>/<worker-id>/heartbeat.jsonl` — append-only heartbeat trail.
- `memory/products/registry.json` — mapping of bundles to spawned product repos.

The post-decision feedback loop is broader than originally scoped. It started as "post-mortems for shipped bundles." It expanded to include calibration on stakes scoring, security findings, verification plans, and capability grant ROI. The expanded scope is the right one; the narrower scope was an artifact of where in the design conversation it surfaced.

**AGENTS.md** is the durable semantic memory file at every repo root (control-plane and each product repo). Cross-tool portable; future-proof. Every agent's prompt context includes the relevant subset of the capability manifest plus the relevant `AGENTS.md` content at task start. Capabilities aren't useful if agents don't know they exist; documentation isn't useful if agents don't read it.

## Deferred items

These are known gaps. Each will need its own design pass before it's implementation-ready.

**Artifact protocol deferred items.** The following items are deferred from the Artifact Protocol section: artifact streaming (stream_put and stream_get); version immutability enforcement for non-"latest" artifact descriptors; artifact signing for non-repudiation; transparent compression at the ArtifactStore layer; `artifact.list` pagination; a binary side channel for artifact data transfer; cross-bundle artifact sharing semantics; a configurable default TTL for global artifacts; credential-use audit trail aggregation for `secrets.fetch`; artifact-level immutability and pinning metadata flags.

**Schema versioning policy.** Applies to the capability manifest schema and the task DAG schema. Forward-compatible additions only? Deprecation cycles? Migrations? Currently both schemas have `schema_version: "1.0"` but the upgrade rules are not specified.

**Capability manifest review UX.** How the human approval flow actually presents a manifest. Reviewers need tooling.

**Hostname-based egress enforcement implementation.** The egress proxy with name-based filtering. The mechanism is clear in principle; the operational details (cache strategy, what happens when DNS resolves to multiple IPs, how to handle TLS SNI versus plaintext HTTP) are not.

**State-partitioning specification.** Made moot by the roll-our-own DAG decision (since LangGraph isn't being adopted, there's no third-party state to partition), but the principle generalizes: the boundary between hot-state (SQLite) and durable-state (`memory/`) deserves explicit rules for what goes where.

**Multi-tenant SaaS concerns.** Out of scope for v1.1 by definition. Listed here so the boundary stays explicit.

**Performance review track and Compliance review track.** Pre-execution review tracks beyond General Adversarial, Security, and QA. Performance (load, latency, resource consumption) makes sense once shipping infra-heavy bundles regularly. Compliance (privacy regulations, accessibility, OSS licensing) makes sense once there's external accountability.

**Persistent-log Agent Activity Board.** The current policy is strict ephemeral (agent activity issues exist only while running, persistence handled by Review Deck comments and `memory/`). Revisit trigger: if `memory/` is being manually queried for agent run history more than twice weekly, switch to persistent-log model.

**Multi-agent support per worker class.** OpenCode is the sole coding agent in v1; the schema slot for per-worker-class agent overrides exists but only OpenCode is wired up. v1.2 candidate.

**Multiple GitHub Apps (one per role).** Single App with author-identity differentiation works for v1; per-role permission scoping becomes worth the operational overhead once Developer agents need credentials other roles shouldn't have.

**Repository split (separate agent-activity repo).** Currently a single repo houses bundles and agent-activity issues. Revisit if access-scope or volume issues emerge.

**External channel notification relay.** Daily digest issue, tiered urgency rules, Slack/Discord/SMS via webhook consumer. The architectural hooks exist (the structured `notify-reason` HTML comment, the `notify()` helper, the `memory/notifications/log.jsonl`); the implementations are deferred.

**Inline AI Q&A on deck artifacts.** Currently routed elsewhere (Steering Comment surface). Could move into the deck if it earns its place.

**Supply-chain hardening for distributable k8s deployment.** Image signing (cosign), SBOM publishing pipeline, pinned base images by digest. Whole separate workstream.

**k8s-specific concerns when k8s becomes a deliverable.** Helm chart with RBAC manifests, NetworkPolicies, PodSecurityStandards, SealedSecrets or external-secrets-operator integration, pod-eviction event watching, supported deployment methods documentation matrix.

**Backup regimen for `/memory` and SQLite state.** Operator responsibility; called out in the ops checklist; not automated by v1.1.

**Worker concurrency framework integration patterns.** Beyond the architectural decisions about decomposition, topology, and state sharing, the practical patterns for running concurrent workers (timeout handling for parallel branches, how aggregator nodes interact with worker pool semaphore, how artifact handoff sequences across DAG levels) are sketched but not specified.

**Soft-abort on fatal failure.** Should a bundle that enters `failed` automatically cancel its still-running in-flight workers, or leave them running as the executor's current design does? The current default (leave running) preserves partial work but consumes worker budget unproductively. Argued for a future refinement where failure inside the bundle's critical path triggers soft-abort while failure on a side branch does not, but that adds a notion of "critical path" that the v1.1 schema does not have.

**Per-bundle concurrency budget.** The executor tracks per-bundle concurrency as a distinct concept but defaults it to the global cap. A real per-bundle cap becomes important when the reviewer has more than one active high-priority bundle, which is not the typical v1.1 mode. Includes scheduler priority and preemption as related future work.

**Critical-path analysis.** The scheduler does not currently identify critical-path nodes (nodes whose delay would delay the bundle's completion). Useful for both scheduling heuristics and reviewer surfacing ("this worker is on the critical path; its timeout is the bundle's timeout"). Deferred.

**Stale ready-set detection.** A node in `ready` state that is never dispatched (because the global budget has been persistently full with other bundles' work) is currently unbounded. A long-waiting ready node should eventually surface as a stall signal. Probably reuses the 8-hour stalled-bundle detector with a per-node variant.

**Expression sublanguage extensions.** The grammar has `matches` for regex. Other extensions worth adding: `length(list)`, `any(list, predicate)`, `sum(list, field)`. Each should pass a sandboxed-evaluation review.

**Reducer parameter schema validation.** Reducers accept a parameter dict from the aggregator node spec. Parameter schemas are currently informal (documented in each reducer's docstring). A JSON Schema per registered reducer, validated at DAG schema validation time, would catch configuration mistakes earlier.

**Cancellation observability.** When an aggregator cancels siblings, the cancellation reason is attached to the cancelled nodes but is not currently surfaced in the reviewer's DAG view in a prominent way. The mermaid rendering marks cancelled nodes with the dashed border, but the reason (e.g., "cancelled because first_success elsewhere") is only in the audit log. Worth surfacing in hover-over text or the capability-request board.

**Human-approval gate timeout.** v1.1 does not specify a default timeout for `human_approval` gates. The 8-hour stalled-bundle detector catches the case indirectly but doesn't distinguish "waiting on human" from "wedged." A per-gate `approval_timeout` field with a default of "indefinite" seems right; some gates may want a 72-hour default-reject.

**DAG validation performance.** v1.1 DAG validation rules are linear in graph size for each, but several rules traverse the graph. For large DAGs (thousands of nodes, possible with aggressive dynamic expansion), validation cost is non-trivial. Incremental validation during expansion (re-check only the grafted subgraph) is an optimization.

**Multi-orchestrator coordination.** The executor assumes a single orchestrator process drives a bundle. If the system ever runs multiple orchestrator replicas (for HA, not for scale), bundle ownership needs a locking mechanism (SQLite row-level lock or an advisory lock). v1.1 is explicitly single-process; deferred for the HA pass.

**Cross-bundle dependencies.** Bundle independence is assumed throughout (flagged in observations). If cross-bundle dependencies become real, the executor needs a notion of "waiting on external bundle state" that is not a gate node of any currently-defined kind.

**Reducer-aware concurrency accounting.** If a custom reducer is added later that spawns a worker (the rejected "worker-spawning reducer" pattern resurfaces), the scheduler must account for the reducer's worker against the bundle's worker budget. Out of scope for v1.1 because custom reducers are out of scope.

**Retry policy on grafted nodes.** Grafted nodes carry their own `retry_policy`. A bundle-level cap on total retries across all nodes (to prevent a runaway expansion from racking up enormous retry counts) is not specified.

**Gate `rpc_query` retry semantics.** A failing `rpc_query` is a gate failure subject to retry policy. Should transient RPC failures (network blips) be distinguished from predicate-false responses? The current design treats them the same, which means a bundle can be defeated by a flaky endpoint. A future refinement might classify errors: transient RPC errors trigger retry without consuming an attempt; predicate-false responses count as attempts.

**Parked bundle lifecycle.** How parked bundles are discovered, resumed, or cleaned up. The parked state exists in the bundle state machine (specified in Bundle lifecycle: execution and integration) but the workflow around it (periodic digest surfacing, auto-cleanup after N days, resume-from-parked mechanics) is not specified.

**Multi-target bundles (cross-repo execution).** Deferred to v1.2. The single-target constraint (one `target:` value per bundle, specified in Bundle lifecycle: execution and integration) holds for v1.1. If capability-plus-first-use patterns prove common, a v1.2 design pass should revisit with a concrete proposal for multi-target DAGs and cross-repo integration steps.

**Abbreviated review threshold on Redirect.** The conditions under which a Redirect's delta is small enough for abbreviated review vs. requiring full re-review are not formally specified. Review track agents self-escalating when the delta exceeds their confidence threshold is the fallback.

**`irreversible` flag formal schema slot.** The concept is introduced in Bundle lifecycle: planning and approval (cooldown carve-out). The exact schema field, its interaction with the approval matrix evaluator, and its surfacing in the reviewer's UI are not specified at field level.

**Rollback bundle calibration as a separate tracking class.** Rollback bundles are bundles and get calibration data like any other. Whether they should be tracked as a separate class (do rollback bundles have systematically different complexity vs. actual profiles?) is a future question.

**Auto-rollback eligibility for medium-stakes bundles.** Currently restricted to Low-stakes bundles. Expanding to medium-stakes is gated on empirical rollback reliability data. Blocker: no production data exists in v1.1. Provisional: Low-stakes only until rollback bundles demonstrate >95% success rate over at least 20 rollbacks.

## Open questions and flagged decisions

Items where the design has punted, raised a concern, or made a call that should be revisited. Four open questions were ratified by the PM during the bundle lifecycle completion design pass: pre-execution review ordering (confirmed), modification re-scoring (yes, always), steering vocabulary (all four verbs ratified), and default action for summary-tier timeouts (default-hold across the board). These are no longer open.

**Provisional wall-clock and heartbeat numbers.** First-run timeout defaults of 2 hours for small tasks, 4 hours for medium, 8 hours for large. 60-minute maximum heartbeat interval. These are explicitly provisional; they need to survive first contact with real workloads before being ratified.

**Verification-driven auto-rollback criteria.** Three conditions: stakes Low, rollback machine-executable, and auto-rollback declared in the Verification Plan. The "stakes Low" condition means auto-rollback never fires for medium or high-stakes bundles, which is conservative. If auto-rollback proves reliable in practice (the rollback bundle succeeds >95% of the time), expanding to medium-stakes bundles is a calibration-driven decision.

**Friction-pattern aggregation threshold for capability requests.** "N reports over a window." A reasonable starting heuristic is 3 reports in 7 days. Not committed.

**Whether agents may request capabilities for other agents.** E.g., the bundler notices that critique agents would benefit from X. Adds power and adds noise; if allowed, flag as second-hand.

**"Self-imposed limits" surface.** Inverse of capability requests: agents flagging "I have access to X but I don't think I should be using it for this task" or "I notice I have permission to do Y but this seems risky." Architecturally interesting; lower priority.

**Agent activity issues, same repo or separate.** Currently same-repo. Revisit if access-scope or volume issues emerge.

**Whether to count the schemas as part of "the twelve."** Resolved during consolidation: the capability manifest schema and the task DAG schema are operationalizations of decisions already made (capability-mediated isolation, star topology, bounded dynamic expansion, roll-our-own executor), not standalone design choices. They are first-class sections in this document, but not items in the decision list.

**Quorum aggregator with post-quorum completions.** When a quorum aggregator has fired with cancel-remaining enabled, and one of the cancellation targets completes cleanly in the grace window, the output is captured but not used in the reduction. Is there a use case for "quorum fires, but update the output if late completions change the reduction"? Probably no (the reduction's output has already been consumed downstream), but worth noting in case calibration data later argues otherwise.

**Bundle-failure cancellation policy.** When a bundle is marked `failed` due to a node failure, the executor leaves still-running in-flight nodes alone. The rationale is preserving partial work for recovery and reviewer inspection. The cost is that worker budget is consumed by work whose output may no longer be useful. Whether to add a soft-abort policy (cancel in-flight nodes when the bundle is committed-failed) is in deferred items but worth flagging here as a real trade.

**Mermaid rendering frequency.** The mermaid is cheap to re-render but it does run on every MCP resource fetch and GitHub comment update. For large DAGs (hundreds of nodes after aggressive expansion), even cheap rendering could become a noticeable hit on the orchestrator's main loop. Caching with state-hash invalidation is straightforward to add if it becomes a problem; not done in v1.1.

## Rejected alternatives

Decisions that were explicitly considered and not adopted, with the rationale that survived.

**Docker per worker with hardened defaults.** This was the original v1.1 worker isolation choice in the worker-environment discussion: drop `CAP_SYS_ADMIN`, read-only root filesystem with explicit writable mounts, bridge network with explicit egress, no host network, no docker socket mount, no privileged mode. It was superseded by bubblewrap after the capability-enforcement analysis in the orchestrator design phase: the case for kernel-level enforcement of network and process namespaces beat the case for trusting workers to honor wrapped HTTP clients. The earlier decision predated the capability model being fully specified. The supersession was not flagged at the time, which is itself worth noting (see Observations from consolidation).

**Plain bind-mount or filesystem-only chroot for worker isolation.** Considered as a lighter alternative to bubblewrap. Rejected because filesystem isolation alone leaves network namespace, PID namespace, and kernel attack surface unconstrained. A worker that bypasses its own wrapped HTTP client (LLM hallucinates `subprocess.run(["curl", ...])`) is unbounded; the capability claim is enforced only by the worker's own goodwill. Bubblewrap closes these for single-digit milliseconds of startup overhead.

**Capability-checking the orchestrator itself.** Considered. Rejected because it relocates the trust root rather than eliminating it: the brain still has total power because it can ask the hands to do anything within the policy, and the policy has to allow everything the orchestrator legitimately needs. Mitigations (narrow privileged surface, systemd hardening, k8s RBAC, heavy audit logging, careful code review) bound the blast radius adequately.

**LangGraph as the DAG executor framework.** Considered. The state-partitioning concern was the showstopper: LangGraph wants to own the state schema, that state flows through checkpoints and gets serialized, and the system has things in its state that must not end up in checkpoint blobs (capability grants, credentials, GitHub tokens, SA tokens). The mitigation plan was "two state stores, enforce the boundary in code review forever" which was a tell that the fit wasn't clean. Steel-manned and rejected after the initial flip-flop. Patterns from LangGraph (checkpointing at node boundaries) and Temporal (deterministic-with-explicit-side-effects) are adopted without the dependencies.

**CrewAI, Temporal, Airflow as DAG frameworks.** Considered briefly. CrewAI's role-based abstraction is a poor fit for a system with tasks-with-capabilities rather than agents-with-roles. Temporal and Airflow are designed for different problem shapes (long-running business workflows, not LLM-driven coding tasks); integration with the capability model would be substantial work.

**Single monolithic orchestrator process.** Considered for the process topology. Rejected in favor of the hybrid (orchestrator core + separate MCP server + ephemeral worker subprocesses), because the MCP server's failure modes are independent and shouldn't take down in-flight bundles, and because workers as subprocesses are easier to isolate.

**Decomposed orchestrator services.** The opposite extreme: separate processes for state machine, worker pool, capability enforcer, audit logger. Rejected because these responsibilities share state intensely; splitting them would add IPC overhead and consistency complexity for no real benefit.

**Go or Rust for the orchestrator.** Considered. Python won despite the long-running-process bias usually pushing the other direction, because the LLM and agent ecosystem is Python-first and orchestrator throughput isn't the bottleneck. Rust remains the migration target if performance ever becomes a real bottleneck.

**Multiple SQLite databases (separating hot operational state from append-only audit data).** Considered. Rejected for v1.1 in favor of a single file. SQLite handles GBs of data fine; if `audit_log` growth becomes a query-performance problem, partition then.

**Worker re-attachment on orchestrator restart.** Considered. Rejected for v1.1 in favor of kill-all on restart. Reconstructing live worker connections and reattaching to running subprocesses is genuinely hard to get right and the failure modes are nasty. Restarts of a stable service should be rare; the cost of redoing in-flight bundle work is bounded.

**Stdout JSON-lines for worker-to-orchestrator communication.** Considered as a simpler alternative to bidirectional RPC. Rejected because bidirectionality is needed for future use cases (mid-task context injection, prepare-for-handoff coordination, pause/resume signaling, secret fetch). Locking out bidirectional communication now would be a costly backtrack.

**Peer-to-peer worker communication.** Considered. Rejected because it breaks the capability model (orchestrator can no longer mediate cross-worker action), explodes the security surface (every worker becomes a server), and makes k8s deployment harder (worker pods would need to be addressable, which Jobs aren't).

**Single shared filesystem across workers.** Considered. Rejected because on k8s it would require ReadWriteMany PVCs (only available on certain storage classes), creating surprise behavior differences between local and cluster deployments. Per-worker isolation with artifact RPC is the canonical channel.

**Single shared working tree across workers (or single branch with multiple workers).** Considered. Rejected because parallel edits to the same file are a disaster, pessimistic locking defeats parallelism, and there's no clean place for review.

**Pure static decomposition or pure dynamic spawning.** Both considered. Pure static was rejected because real coding work doesn't always decompose cleanly up front. Pure dynamic was rejected because it makes resource usage hard to reason about, makes capability boundaries hard to enforce, and makes human review nearly impossible. Hybrid (static plan plus bounded expansion through orchestrator approval) was the synthesis.

**GitHub Issues as primary surface.** Originally proposed. Superseded by MCP after Claude Desktop emerged as the reviewer's actual primary surface during the design conversation. GitHub Issues retains its role as durable audit trail and mobile fallback; MCP is the conversational thinking surface.

**Single-product-with-multi-component as separate repos** (one component, one repo). Considered. Rejected because splitting too aggressively explodes operational overhead. Single product per repo, multi-component inside via subdirectories.

**Multiple GitHub Apps per role for v1.** Considered. Rejected for v1 in favor of single App with role-tagged commit author identity. Per-role permission scoping is the v2 motivation.

**Daily digest, tiered urgency, external channel relay for notifications.** All considered. All deferred. v1 is GitHub-native assignment-based notifications, with architectural hooks for expansion.

**Loops in the task DAG.** Considered. Rejected because loops make static analysis dramatically harder and the human reviewer should see exactly the structure that will execute. "Retry until X" is expressed via `retry_policy`. "Iterate over a list" is expressed via dynamic expansion (worker spawns one sub-task per item). Acceptable expressiveness limitation.

**Generic expression language for `on_property` edge conditions.** Considered. Rejected in favor of a restricted sublanguage (field access, comparison, boolean combinators; no function calls, no loops). Trades expressiveness for analyzability and security. Anything more complex becomes a gate node.

**"Allow everything except X" semantics in the capability manifest.** Considered. Rejected. The schema is purely additive. Subset-checking stays trivial; the threat model stays clean. Common patterns like "read the working tree except `.env`" must be expressed by listing what's allowed rather than what's excluded.

**A general-purpose workflow DSL for `on_property` edge conditions.** Considered. Rejected in favor of the restricted sublanguage. The sublanguage is small enough to audit for sandbox escape, small enough to implement without a dependency, and expressive enough for the "branch on a test-result property" use case. Anything more complex is a gate node.

**Pluggable schedulers.** Considered for the executor. Would let bundles declare priority, critical-path-first, or other policies. Rejected for v1.1 because the single-box deployment has no workload that justifies the added complexity. A single policy (FIFO) is correct; plugging in a different one later is a scheduler-module swap with no downstream impact.

**Pluggable reducers (loaded from bundle-supplied code).** Considered. Rejected because a bundle-supplied reducer is unbounded code running in the orchestrator process; it would blow the trust boundary. Custom reducers via worker nodes (the successor-worker pattern) preserve capability bounds.

**Synchronous dispatch inside the executor's tick.** Considered: on each tick, the scheduler dispatches all ready nodes synchronously and waits for each to enter `running` before returning. Rejected because WorkerRunner spawn involves user-namespace setup and can take tens of milliseconds; serializing all dispatches inside a single tick would delay event handling. Current design: ready-set is computed synchronously in the tick; actual dispatch is fired-and-forgotten via asyncio tasks, each of which reports back via the event queue when the node is `running`.

**Multiple checkpoint levels (node-boundary plus mid-node).** Considered. Rejected because mid-node checkpointing would require workers to understand checkpoint semantics (pause their work at a safe point, serialize state, reconstitute on resume), which is far more coupling than the kill-all-on-crash policy accepts. The LangGraph argument for mid-node checkpointing depends on long-running stateful agent loops with durable conversational state; workers in this system are bounded subprocesses whose state is reconstructible from the task spec plus the worktree, so coarser checkpointing suffices.

**Bundle-as-expression model for the DAG.** Considered: represent the bundle as a compositional expression tree (`seq(a, par(b, c), aggregate(d))`) and compile it to a DAG. Rejected because the explicit-DAG model is equivalent in expressiveness and more directly inspectable. Reviewers see the DAG that will execute, not an expression that compiles to one.

**A state machine per node, coordinated by an actor model.** Considered for the executor. Rejected because serializing mutations through a single event pump is simpler, avoids a large class of concurrency bugs, and matches the orchestrator's existing async structure. Actors are more natural in a distributed deployment, which is where this system may eventually go, but the executor's work happens in one process and the actor abstraction's benefit is small relative to its conceptual overhead.

**Lazy edge-condition evaluation at tick time.** Considered evaluating `on_property` edges lazily (only when the scheduler asks whether the destination is ready) versus eagerly (at the moment the source reaches `completed`). Chose eager evaluation because it simplifies the "fired" flag semantics (an edge's fired state is a definite fact, not a re-computable one), makes the audit trail cleaner (`fired_at` has a meaningful timestamp), and doesn't cost anything because evaluation is cheap.

## Observations from consolidation

The consolidation surfaced things the in-line conversation didn't explicitly address. They aren't decisions; they're observations that should inform the next design phase.

**Cross-cutting concerns and unstated assumptions.**

The trust model assumes single-tenant deployment everywhere. Multi-reviewer support is out of scope. Multi-tenant SaaS is out of scope. The `mode:hands-on` toggle, the recall window, the calibration loop, the capability-grant decision authority, the MCP single-token auth — all assume one human in the loop. The system would need substantial rework to support a co-founder, an advisor as secondary reviewer, or a contractor with scoped access. Not a problem; just a property worth being explicit about so that "trivial extension to two reviewers" doesn't get implicitly assumed.

The auto-ship safety story depends on execution-layer reversibility. Auto-shipping a Low-stakes bundle is safe because the recall window catches mistakes; the recall window is meaningful only if execution failures are detectable and revertible within 48 hours. The QA agent's post-execution validation is the mechanism that makes this real. If post-execution QA is slow or unreliable, the entire auto-ship safety argument weakens. Worth verifying in practice before relying on it.

Bundle independence is assumed throughout. Stakes scoring, timeouts, queue ordering all treat each bundle as isolated. Real bundles will entangle (shipping A makes B obsolete; B's spike informs A's design). The spec does not have a primitive for bundle dependencies. Probably fine for v1; flagged so it doesn't ambush a later phase.

Stakes factors are summed as if independent. They're probably correlated (high-novelty changes are more likely to be hard to reverse, etc.), so the composite over-counts when factors correlate. This biases scores conservative, which is probably fine. Calibration will surface this empirically.

The bundler is the trust point for honest planning. The system has no kernel-level defense against a bundler that systematically lies (under-scores, hides risks, omits concerns). The mitigation is calibration data, which is necessary but not sufficient — calibration takes time to accumulate, and a determined bundler could under-score for a long time before the pattern becomes statistically unmistakable. In practice, this is fine for an early-stage system where the bundler's prompt is under direct human control. It's worth flagging because if the bundler ever runs against a model the operator can't directly tune, the trust assumption changes.

**Drift detected during consolidation.**

The prior conversation accumulated several silent supersessions and scope creeps that weren't flagged at the time. Naming them because knowing where the design process had blind spots is useful signal for the next phase.

*Docker → bubblewrap, silent.* Worker isolation was specified as Docker-with-hardened-defaults during the worker-environment discussion. Later, during the orchestrator design, the question was reframed in terms of options a-d for sandboxing, and bubblewrap (option c) was recommended and accepted. The earlier Docker decision was never explicitly retracted. The bubblewrap argument (kernel-level capability enforcement) is genuinely stronger than the Docker argument, but the lack of an explicit "we're replacing the Docker decision" was a process miss.

*GitHub Issues → MCP, silent.* The bundle approval surface was originally GitHub Issues. Later, when the reviewer noted that Claude Desktop had become their primary surface, MCP was promoted to primary and GitHub Issues demoted to secondary. The transition itself was clean (the underlying state machine doesn't change), but the design did not flag this as a substantial reframing of how humans interact with the system.

*Decision-log numbering bug.* The prior Claude maintained a running decision log with items 1–12, then later replaced item 10 (originally Source-tree access) with the framework decision (which had been item 12), then re-replaced item 10 with the framework reversal, then added the capability manifest as "item 11" and the task DAG schema as "item 12" while overwriting the earlier 11 (Integration) and 12 (Framework). The net result is that the literal decision log is internally inconsistent. The schemas were promoted to first-class document sections in this consolidation rather than counted as items in the twelve, partly to avoid perpetuating the renumbering.

*Items 1–3 of the decision log are empty.* The prior Claude flagged these as "from the prior turn, which I don't have visibility into." On reflection, those slots were placeholders left over from numbering choices, not pointers to missing decisions. They are treated as nonexistent here.

*QA agent scope creep.* Originally specified as pre-execution verification planning only. Later expanded to include post-execution validation against the same plan. The expansion is the right one (it closes a real gap in the auto-ship safety story), but it accreted across turns rather than being introduced as a deliberate addition.

*Post-decision feedback loop scope creep.* Originally scoped as "post-mortems for shipped bundles." Expanded to include calibration on stakes scoring, security findings, verification plans, capability grant ROI, and rate-limit observations. The expanded scope is correct; the narrower scope was an artifact of where in the conversation it was first introduced.

*Review Deck v1 numerics carried forward without reconciliation.* The 75% confidence floor, the 8-hour stalled-bundle detector, the 48-hour low-stakes auto-ship window, the 5/10/21-day high-stakes escalation ladder, the 12-hour acting-soon window, and the 7-day Recently Decided window were all set in v1. v1.1 added pre-execution review tracks, the Capability Requests Board, MCP as primary surface, and several other layers, none of which formally re-evaluated the v1 numerics. They are referenced rather than re-specified in v1.1, but they should be revisited the next time the approval flow is touched, since the surrounding system has changed.

*Approval-matrix vs. pre-execution-review-track ordering.* Both are gates between bundle proposal and bundle execution. The natural ordering (review tracks run first, their outputs feed the matrix) was assumed in this consolidation but not explicitly stated in the prior conversation. Ratified in the bundle lifecycle design pass.

**Areas the next design phase should plan to address.**

The DAG executor, artifact protocol, bundle lifecycle, I/O schema, and target field semantics — the largest deferred chunks from the initial v1.1 consolidation — are now fully specified at machine-readable precision (typed schemas, enumerable state transitions, named error cases). The remaining deferred items are narrower in scope; the largest is the schema versioning policy, followed by the capability manifest review UX.

One integration note: the Architecture section's state machine reference (line 175) summarizes the 12-state/25-transition model. The SQLite schema comment for `bundles.state` at line 92 already enumerates all 12 states.

The next major architectural addition beyond the deferred items is the k8s deployment target and its associated work (Helm chart, NetworkPolicies, PodSecurityStandards, image signing, S3-backed artifact store), which is gated on the k8s milestone rather than on design completion.

A migration plan from v1 numerics to v1.1 (when v1.1 actually reaches a state that wants to revisit them) should be laid down before the surrounding system gets larger. If those numerics drift, they should drift deliberately.

A protocol for flagging supersessions during design is worth adopting explicitly. The drift items above all share a structure: a later turn made a decision that obsoleted an earlier one without saying so. A simple rule like "if a new decision changes a previous one, flag both with a supersession note" would have caught most of these.

## Editorial pass notes

This section is the human-readable audit trail of the editorial pass performed on 2026-05-08. Its primary purpose is to give the PM and the next design session a complete picture of what changed, what was tagged, and what remains unresolved. The implementing agent should read this section to understand which values are hard constraints and which are provisional defaults it should hardcode as tunable.

### Precision changes made

**[Architecture, Bundle state machine]** Replaced conversational summary ("In summary: twelve states...") with a typed table enumerating all 12 states with enum values, terminal/non-terminal classification, and descriptions. Added the `IllegalTransitionError` exception class definition and its JSON-RPC serialization format (code `-32001`). The state machine reference now cross-references the exact section "Bundle lifecycle: execution and integration" where the full 25-row transition table lives.

**[Architecture, Crash recovery]** Replaced prose description of crash recovery with a numbered 6-step procedure. Each step names the specific SQL operation (e.g., "Scan workers for rows in state running or paused; mark each failed with exit_reason = 'orchestrator_crash'") and the state transitions produced. Added explicit cross-reference to the DAG executor's "Checkpointing and crash recovery" subsection.

**[Architecture, SQLite schema]** Added `connection_lost` to the `workers.state` schema comment so the comment enumerates all valid worker states: `pending|running|complete|failed|killed|connection_lost`.

**[Worker RPC protocol]** Added full JSON-RPC 2.0 request/response schemas for all 14 methods (8 worker-to-orchestrator, 6 orchestrator-to-worker). Each schema includes field names, types, and value ranges. Methods previously described in prose-only now have typed schemas matching the precision level of the Artifact Protocol section's RPC method specifications.

**[Worker RPC protocol]** Added a formal error code table with 9 RPC-level error codes (`-32000` through `-32603`), each with name and meaning. Added notification vs. call classification table for worker-to-orchestrator methods. Added algorithmic description of the RPC dispatcher's capability check (wildcard matching rules for `rpc.methods` grants in both directions).

**[Capability manifest, Composition rules]** Added algorithmic subset-checking function `is_subset(task_manifest, bundle_manifest) -> tuple[bool, str]` with per-category rules. Each category (filesystem, network, process, secrets, RPC, resources) has explicit pseudo-code for the subset test. Added network protocol subsumption order (`tcp > udp, http, https; http > https`). Added RPC method pattern coverage rules and artifact descriptor pattern coverage rules using the glob algorithm from the Artifact Protocol section.

**[Surfaces, MCP tools]** Added full typed input/output schemas for all 11 MCP tools. Each tool now has a JSON input schema, JSON output schema for success, and JSON error schema for failure (with error codes and detail fields where applicable). Legal from-states are specified for state-changing tools.

**[Surfaces, MCP resources]** Added a typed table of all 11 MCP resources with URI template, return type, and description. Each resource is now machine-resolvable: an implementing agent knows exactly what type to return for each URI pattern.

**[Surfaces, MCP prompts]** Added a typed table of all 4 MCP prompts with argument names, types, and template purpose descriptions.

**[Surfaces, Cross-surface sync]** Updated to reference the ratified first-write-wins policy from "Bundle lifecycle: planning and approval, Multi-surface action ordering and race resolution" instead of the prior language deferring race resolution to implementation-time.

### Stale deferred references fixed

Four sections contained "is deferred" language for items that are now fully specified elsewhere in the document. These were false signals to an implementing agent. They have been replaced with cross-references to the specifying sections:

1. **[Worker environment, Secrets]** "that protocol is deferred" replaced with cross-reference to Artifact Protocol section for `secrets.fetch`.
2. **[Capability manifest, Secrets grants]** "the protocol semantics for secrets.fetch are deferred" replaced with cross-reference to Artifact Protocol section.
3. **[Task DAG schema, Aggregator nodes]** "Reducer registry semantics... are deferred" replaced with cross-reference to DAG executor's Reducer registry subsection.
4. **[Task DAG schema, Edges and edge conditions]** "Formal grammar for the sublanguage is deferred" replaced with cross-reference to DAG executor's "The `on_property` expression sublanguage" subsection.

### Provisional items tagged

The following values were tagged `[PROVISIONAL]` inline. An implementing agent should hardcode each as a named constant or configuration default and not treat it as a ratified constraint:

1. **Per-worker resource limits** (4 GB RAM, 2 CPU, 10 GB disk) — Architecture section.
2. **Worker timeout defaults** (2h small / 4h medium / 8h large) — Worker environment section. Already noted as provisional in prose; now tagged inline.
3. **Heartbeat maximum interval** (60 minutes) — Worker environment section.
4. **Ollama Cloud health check interval** (30 seconds) and **grace window** (5 minutes) — Worker environment section, both in JSON config block and in prose.
5. **Gate rpc_query timeout** (30 seconds) — DAG executor, Gate node mechanics.
6. **Cancellation grace period** (30 seconds) and **SIGKILL delay** (10 seconds) — DAG executor, Aggregator mechanics and Abort sections.
7. **Inline artifact threshold** (4096 bytes) — Artifact Protocol, Storage layer.
8. **Task artifact retention** (24 hours) — Artifact Protocol, Lifecycle and garbage collection.
9. **Bundle artifact retention** (7 days complete/rejected, 30 days failed) — Artifact Protocol, Lifecycle and garbage collection.
10. **Global storage cap** (50 GB) — Artifact Protocol, Storage layer.
11. **Stuck-iterations threshold** (3) — Bundle lifecycle, Execution structure.
12. **Per-bundle concurrency budget formula** (`max(2, global_budget // active_bundles)`) — DAG executor, Ready-set scheduling.
13. **Friction-pattern aggregation threshold** (3 reports in 7 days) — Surfaces section. Already marked "not committed"; now tagged.

### Cross-references fixed

Broken or vague cross-references corrected:

1. **Architecture section**: "In summary" prose replaced with table and explicit section reference to "Bundle lifecycle: execution and integration."
2. **Architecture section**: Crash recovery reference now names the exact subsection: "DAG executor section under Checkpointing and crash recovery."
3. **Worker environment**: Secrets section now references "Artifact Protocol section" instead of calling the protocol deferred.
4. **Capability manifest**: Secrets grants now reference "Artifact Protocol section" for `secrets.fetch`.
5. **Task DAG schema**: Aggregator nodes now reference "DAG executor section under Reducer registry."
6. **Task DAG schema**: Edge conditions now reference "DAG executor section under The `on_property` expression sublanguage."
7. **Surfaces**: Cross-surface sync now references "Bundle lifecycle: planning and approval, Multi-surface action ordering and race resolution."

### Items moved to Deferred

No half-specified behavior was moved to the Deferred Items section during this pass. The four items that appeared to be half-specified (secrets.fetch protocol, reducer registry, expression sublanguage grammar) were discovered to already be fully specified in later sections of the document. Their earlier "deferred" labels were stale. These were corrected to cross-references rather than split into the Deferred Items section, since the behavior was already fully specified elsewhere.

### Phase 1 readiness fixes (2026-05-08)

After the initial editorial pass, a Phase 1 readiness assessment identified three blocking issues and four non-blocking ambiguities. The following fixes were applied:

**Fixed blocking issues:**
1. **[workers.state schema]** Added `paused` to `workers.state` schema comment. Full enumeration: `pending|running|paused|complete|failed|killed|connection_lost`. The `running -> paused` transition occurs in the worker row when pause completes, matching the `dag_nodes` state transition.
2. **[Bundle state machine]** Added kernel-mode transition: `PROPOSED → APPROVED` (trigger: `kernel_direct_approval`, actor: Reviewer). This transition exists in v1.1 Phase 1 only, when the bundler agent is not yet implemented. It is removed in Phase 2 when the bundler exists and `bundler_completed` becomes the only path from `PROPOSED`. The transition is marked `[PHASE-1-ONLY]` in the transition table.
3. **[Worker RPC protocol, cap.check]** Specified `op_descriptor` structured format: `<category>.<operation>[:<resource>]`. Examples: `filesystem.write:/work/src/main.py`, `network.egress:api.github.com:443`, `process.exec:/usr/bin/git`, `rpc.method:artifact.publish`. The dispatcher parses the category prefix and dispatches to the appropriate category-specific capability check.

**Fixed non-blocking ambiguities:**
4. **[worker.heartbeat phase enum]** Added `starting` to the heartbeat phase enum. Full set: `starting | thinking | tool-call | writing-code | running-tests | idle`. The `starting` value is the canonical first heartbeat emitted after worker spawn, before the worker begins meaningful work.
5. **[worker.heartbeat state updates]** Specified that `worker.heartbeat` updates `workers.last_heartbeat` on every call. On the first heartbeat received from a worker in `pending` state, it additionally transitions `workers.state` from `pending → running`. Subsequent heartbeats from a worker already in `running` state only update `last_heartbeat`.
6. **[Crash recovery Phase 1 no-ops]** Step 1's "paused" branch: in Phase 1, no transition produces paused workers, so this branch is a no-op. Step 4 (replay approval decisions): in Phase 1, the `approval_requests` table is unused (no MCP, no GitHub Issues surface), so this step is a no-op. The implementing agent writes the reconciliation logic for all steps but the Phase 1 execution path never hits the paused or approval-replay branches.
7. **[State machine enum completeness]** The implementing agent defines all 12 enum values in `BundleState` (Python `StrEnum`) even though Phase 1 implements only 8 transition handlers. The unused values (`redirecting`, `verifying`, `parked`) are valid future states and their presence prevents a schema migration when Phase 2 adds the bundler, QA agent, and redirecting machinery.

### Gaps found but not filled

The following are design gaps that an implementing agent would encounter. They need a design pass before implementation can proceed. None were filled during this editorial pass.

**1. `irreversible` flag formal schema slot.**
- **Location**: Bundle lifecycle: planning and approval (cooldown carve-out). The Deferred items section already lists this as deferred.
- **Which agent hits it**: The agent implementing the approval matrix evaluator and the bundle output schema.
- **Question**: Where exactly in the bundle proposal schema does the `irreversible` flag live? Is it a field on the proposal (`proposal.irreversible: bool`) or a property derived from the verification plan's rollback assessment? The approval matrix cooldown logic needs to read it; the bundle output schema needs to serialize it.
- **Recommendation**: Add `irreversible: bool` to the `bundle_output.proposal` block and to the `bundles` table as a column. Default `false`. Set by the bundler during planning. The approval matrix evaluator reads `bundles.irreversible` to determine cooldown duration (1 hour vs. 24 hours).

**2. Mermaid rendering caching not specified.**
- **Location**: Open questions, "Mermaid rendering frequency."
- **Which agent hits it**: The agent implementing the MCP resource handlers and GitHub comment updates.
- **Question**: The spec says mermaid rendering "is cheap enough to re-render on every MCP resource fetch." For large DAGs with hundreds of nodes (possible after aggressive expansion), "cheap enough" may not hold. Should the implementing agent add caching, or should it re-render unconditionally and defer caching until a performance problem is observed?
- **Recommendation**: The implementing agent should re-render unconditionally in v1.1 and add a `TODO` comment marking the spot for a state-hash-based cache. The cache invalidation key is the max `node_state_history.id` for the bundle's DAG. This is a 10-line optimization deferred until needed.

**3. Approve-with-summary tier default action contradiction.**
- **Location**: Bundle lifecycle: planning and approval.
- **Which agent hits it**: The agent implementing the approval matrix evaluator and the timer-based auto-action logic.
- **Question**: The approval matrix table description says "Default if reviewer doesn't respond in the configured window: low-risk cells default-approve after 4 hours; moderate-risk cells default-hold." But the "Default actions, cooldown durations" subsection says "PM ratified: default-hold across the board" and the `settings.json` snippet shows all actions set to `"hold"`. Which is authoritative?
- **Recommendation**: The "default-hold across the board" ratification is authoritative. The matrix table description should be updated to say "Default action: hold (require explicit response). The per-cell overrides in settings.json start at hold and may be changed to approve after the calibration loop has accumulated enough history." This is a spec bug, not an editorial ambiguity.

**4. Gate human-approval default timeout.**
- **Location**: Deferred items, "Human-approval gate timeout."
- **Which agent hits it**: The agent implementing gate node mechanics and the stalled-bundle detector.
- **Question**: The spec does not specify a default timeout for `human_approval` gates. The 8-hour stalled-bundle detector catches the case indirectly. An implementing agent needs to know what value to use for `approval_timeout` on gate creation.
- **Recommendation**: Default to `null` (indefinite). The gate stays in `running` until a human decision arrives. The stalled-bundle detector's existing 8-hour window fires the `acting-soon` label for bundles with long-pending gates, which is the correct behavior for a human-in-the-loop interrupt. Gate-specific timeout with default-reject semantics is a v1.2 feature.
