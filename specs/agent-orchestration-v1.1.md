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
  state TEXT NOT NULL,             -- proposed|approved|in_progress|verifying|complete|failed|rejected
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
  state TEXT NOT NULL,             -- pending|running|complete|failed|killed
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

The DAG executor adds further tables (`dag_nodes`, `dag_edges`, `node_state_history`, `dag_expansions`, `approval_requests`, `artifact_refs`) covering DAG state, transition history, expansion provenance, the unified approval-request lifecycle, and the executor's view of artifact publication. Those are specified in the DAG executor section.

**Bundle state machine.** Transitions are guarded; illegal transitions are rejected by the orchestrator and return errors to whoever requested them. The transitions:

```
proposed ─approve──→ approved ─start──→ in_progress ─workers_done──→ verifying
   │                    │                   │                            │
   │                    │                   │                            ├─verify_pass─→ complete
   ├─reject──→ rejected │                   │                            └─verify_fail─→ failed
   │                    │                   │
   └─modify──→ proposed │                   └─pause──→ paused ─resume──→ in_progress
   (revised)            │                                │
                        │                                └─kill──→ failed
                        └─pause──→ paused ─resume──→ approved
                                     │
                                     └─kill──→ failed
```

Every transition writes to `audit_log` and (if human-driven) to `approval_decisions`.

**Crash recovery.** On orchestrator startup, the policy is kill-all rather than try-to-resume in-flight workers. Reconstructing live worker connections and reattaching to running subprocesses is genuinely hard to get right and the failure modes are nasty. Restarts of a stable service should be rare (deploys, kernel upgrades), and the cost of redoing in-flight bundle work is bounded. On startup the orchestrator scans for workers in state `running`, marks them failed with reason `orchestrator_crash`, transitions affected bundles accordingly, replays unread approval decisions from external surfaces (GitHub Issues comments, MCP-side decisions that hadn't been ACKed), and then opens the webhook endpoint and MCP socket. Bundles in `verifying` re-trigger verification, since verification is idempotent by design. The full reconciliation sequence (kill-all, reconcile node states, apply retry policies, replay approval decisions, re-trigger bundle reconciliation, open surfaces) is specified in the DAG executor section.

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

**Per-worker resource limits**, configurable per worker class via the capability manifest, with these defaults: 4 GB RAM, 2 CPU, 10 GB disk. There is no global wall-clock kill in v1.1; that policy was originally specified but replaced with heartbeat-based liveness plus learned p95 timeouts per worker class, because Ollama Cloud iteration latency is meaningfully slower than frontier-API iteration latency and a single global timeout proved hard to set right. Provisional first-run timeout defaults are 2 hours for small tasks, 4 hours for medium, 8 hours for large. These are explicitly provisional and need to survive first contact with real workloads before being ratified.

**Heartbeats** are emitted on every state transition, with a maximum interval of 60 minutes. Each heartbeat includes a `phase` field with values like `thinking`, `tool-call`, `writing-code`, `running-tests`, or `idle`, so that "slow but alive" is distinguishable from "wedged." A worker that crosses 2× its expected timeout surfaces as a capability-board entry suggesting model upgrade or task decomposition, rather than being auto-killed.

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
      "health_check_interval_seconds": 30,
      "grace_window_minutes": 5,
      "on_grace_expiry": "fail-with-retry"
    }
  }
}
```

The reasoning behind the choices: deepseek-v4-pro for bundler, planner, and critique because it's the strongest reasoning model in the available catalog with a 1M context window (which matters for roles that ingest large RFC plus memory excerpts); critique uses Max thinking mode because deeper reasoning has the highest payoff per token in that role. kimi-k2.6 for developer because it's purpose-built for long-horizon coding across Rust, Go, Python, frontend, and DevOps domains, with native multimodal support and 256K context (sufficient for tasks scoped to a single subdirectory). deepseek-v4-flash for lightweight tasks (linter fixes, doc tweaks, commit messages) because it's faster and cheaper.

**Rate limits** are learned empirically. Workers emit `rate-limit-observed` signals (with `retry-after` header where present) into `worker-report.json`. The orchestrator aggregates these into `memory/capabilities/rate-limit-observations.jsonl` and adapts spawn rate when patterns emerge. No hard-coded ceilings in v1.

**Ollama Cloud unreachability.** Every 30 seconds, the orchestrator runs a cheap health check against a known endpoint. On failure: pause new worker spawns, mark in-flight workers as `paused-external-dependency`, allow a 5-minute grace window for transient blips. On grace expiry: fail in-flight workers gracefully with auto-retry on the same task once reachability returns. System status surfaces in the orchestrator dashboard and CLI. Clean failure semantics, no zombie workers consuming runner slots while waiting.

**Network egress** from the worker container is mediated by the host-side egress proxy. Hostname-based grants (rather than CIDRs) are first-class in the manifest, and the proxy does the L7 lookup. The host's own firewall is operator-maintained and not duplicated inside containers.

**Secrets.** GitHub Actions secrets for v1, scoped per repo, mounted into worker containers as environment variables only when the worker class declares the capability. An audit log entry records every secret name (not value) accessed per worker per task, in `memory/audit/credential-use.jsonl`. The longer-term intent is to migrate from env-var delivery to RPC-fetched short-lived credentials (`secrets.fetch(name)` over the worker RPC), so secrets live only in worker memory for the duration of the operation that needs them, but that protocol is deferred.

## Worker RPC protocol

Workers communicate with the orchestrator over a bidirectional RPC channel. Bidirectionality is non-negotiable: the unidirectional alternative (workers print structured JSON to stdout, orchestrator parses) was rejected explicitly because future use cases (mid-task context injection, prepare-for-handoff coordination, pause/resume signaling) need orchestrator-to-worker calls and locking that out now would be a costly backtrack.

**Protocol.** JSON-RPC 2.0, with length-prefixed framing (a 4-byte big-endian length, then a JSON payload) over a duplex byte stream. JSON-RPC 2.0 was chosen because it has well-defined semantics for both calls and notifications, supports both directions natively, has a standard error model, and has libraries in every language. The framing is intentionally transport-agnostic: locally the byte stream is a Unix domain socket; on Kubernetes it becomes a WebSocket connection over TLS, or upgrades to gRPC if JSON-RPC's lack of streaming primitives proves painful in practice. The method surface and dispatcher do not change with transport.

**Authentication.** Locally, the orchestrator generates a 256-bit token per worker, passes it via the `STUDIO_WORKER_TOKEN` environment variable (private to the worker's process), and the worker presents it as the first message. The orchestrator validates and binds the connection to a worker ID. The token is single-use; presenting it on a second connection fails. On Kubernetes, the worker pod gets a token mounted as a projected ServiceAccount token (k8s issues short-lived audience-bound tokens natively via the TokenRequest API), and the orchestrator validates via TokenReview. This is much stronger than env-var tokens because tokens are short-lived, audience-scoped, and revoked when the pod terminates.

**Method namespacing.** Methods are organized by family: `worker.*`, `cap.*`, `artifact.*`, `secrets.*`, etc. This lets new method families be added without name collisions.

**Worker-to-orchestrator methods:**

- `worker.heartbeat(progress_message)`: notification, no response.
- `worker.log(level, message, structured_data?)`: notification.
- `cap.request(scope, rationale)` returns `{granted, capability_id?, denied_reason?}`: synchronous; blocks the worker until human or auto decision.
- `cap.check(op_descriptor)` returns `{allowed, capability_id?}`: fast path for already-granted capabilities.
- `worker.progress_report(stage, percent, message)`: notification, structured progress.
- `artifact.request(source_worker_id, path)` returns `{artifact_data}`: inter-worker handoff, mediated.
- `worker.request_human_input(question, context)` returns `{response}`: escape hatch, surfaces via the approval channel.
- `worker.final_report(outcome, files_changed, tests_run, ...)`: terminal call before clean exit.

**Orchestrator-to-worker methods:**

- `worker.pause()`: worker checkpoints and stops; ack required.
- `worker.resume()`: worker continues.
- `worker.cancel(reason)`: worker cleans up and exits, with grace period before SIGTERM.
- `worker.query_status()` returns the worker's self-reported state.
- `worker.inject_context(data)`: push new info to a worker mid-task (for example, "the spec changed").
- `worker.prepare_handoff(target_worker_id, artifact_descriptor)`: worker packages the artifact for another worker.

**Connection-loss semantics.** If the worker's connection drops, the orchestrator marks the worker `connection_lost` and gives it a grace period to reconnect (workers can reuse their token within the grace window). After the grace period, the worker process is killed. This handles transient hiccups without making lost-connection equal lost-work for short interruptions. On Kubernetes, the orchestrator additionally watches pod events from the API server so it learns about evictions promptly rather than waiting for connection timeout.

Methods like `artifact.request` and `worker.request_human_input` are protocol-reserved in v1 even though their implementation is stubbed (they return "not implemented"). This avoids a protocol version bump when those features are actually built.

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

Secrets are named, never inlined; the manifest references a secret by name, and the orchestrator resolves the value at worker spawn time from its own secret store. The manifest never contains plaintext. Delivery mechanism is declared: `env` for legacy tools that read environment variables, `file` for tools that read credentials from disk, `rpc` for tools that ask the orchestrator over RPC. The `rpc` option is best for dynamic short-lived credentials (the worker calls `secrets.fetch(name)`, the orchestrator audits the fetch, and the secret only lives in worker memory for the duration of the operation), but the protocol semantics for `secrets.fetch` are deferred. `purpose` is enumerated so a reviewer can see "this task gets a `github_auth` secret" and immediately understand the implication; `custom` is an escape hatch and should be rare.

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

**Composition rules.** The bundle has its own manifest; tasks within it have task manifests. Three rules are enforced:

1. **Task grants must be a subset of bundle grants.** A task cannot request more than its bundle was approved for. Subset checking is per-category: filesystem subset means task paths are within bundle paths (with recursive flags handled correctly); network subset means task destinations are within bundle destinations; etc.
2. **The bundle is the human-approval unit.** Reviewers approve bundle manifests. Task manifests within an approved bundle do not need separate approval, since they're already bounded by the bundle grant.
3. **Expansion requests carry their own manifest.** When a worker requests sub-task spawning, the request includes the proposed task manifest. If it's a subset of the bundle manifest, auto-approve (the bundle already covered this). If not subset, escalate to human.

Rule 3 is the load-bearing one for dynamic expansion: workers can spawn sub-tasks without human-in-loop as long as they stay within the bundle's pre-approved envelope. Most expansions will, because well-decomposed bundles describe the full envelope up front.

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

`join: all` is the default. `any` and `first_success` are for the "race three approaches, take whichever finishes first" pattern. `quorum` is for "spawn five workers, majority opinion wins." Making aggregators explicit is a divergence from convention but is worth it because the semantics are visible in the schema rather than buried in executor behavior, reviewers can see "this bundle uses majority voting" without reading code, and the executor implementation is simpler (incoming edges to non-aggregator nodes always have `all` semantics; aggregators are the only place where it varies). Reducer registry semantics for `output_strategy: reduce` are deferred.

**Edges and edge conditions.**

```yaml
edges:
  - from: <node id>
    to: <node id>
    condition:
      kind: always | on_success | on_failure | on_property
      property: <expression>
```

`always` means the edge fires regardless of source outcome (cleanup nodes). `on_success` is the default. `on_failure` is for error-handling branches. `on_property` evaluates an expression against the source node's outputs to decide whether the edge fires. The expression sublanguage is intentionally restricted: field access on source node outputs, comparison operators, boolean combinators. No function calls, no loops. Anything more complex should be a gate node, not an edge condition. Formal grammar for the sublanguage is deferred (the schema fields that reference it are marked TBD pending that work).

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

The **per-bundle concurrency budget** caps concurrent workers within a single bundle. In v1.1 this is not separately configured (the global budget is the only cap), but the executor tracks it as a distinct concept to make per-bundle fairness straightforward to add later. A reasonable default is `max(2, global_budget // active_bundles)`, reconsidered in v1.2.

Gate and aggregator nodes do not consume the worker budget. They run in-process in the orchestrator and their cost is bounded by reducer and predicate execution, which v1.1 keeps cheap. If in-process aggregators ever become expensive (a worker-spawning reducer was considered and rejected, but a future variant might re-raise the question), they would consume budget the same way worker nodes do, because they would effectively become worker nodes.

Scheduling policy within the ready set is FIFO by `ready_at` timestamp for v1.1. There is no priority, no critical-path optimization, no SJF. Reasoning: bundles are small, DAGs are small (hundreds of nodes at most), and scheduling latency is negligible compared to node runtime. A more sophisticated policy is trivial to drop in later because the scheduler's input is just the ordered ready-set table.

Starvation is not possible with FIFO plus no priority. A node in the ready set will eventually dispatch as long as the global semaphore has any throughput at all. An Aborted bundle cancels its nodes explicitly, so they leave the ready set; they do not wait.

### Gate node mechanics

Three predicate kinds, each with concrete execution semantics.

**`artifact_property`** evaluates a boolean expression over an artifact's properties. The executor fetches the artifact via the artifact layer (capability-checked through the gate node's task manifest), parses the expression against the artifact's declared schema, and returns true or false. The expression sublanguage is the same one used for `on_property` edges; see below.

**`rpc_query`** issues an RPC to a service whose endpoint and method are declared in the gate spec. The RPC requires its own capability grant in the gate's task manifest. The RPC returns a boolean. Timeout defaults to 30 seconds and is configurable per gate. Transient RPC failures are currently not distinguished from predicate-false responses (both produce gate failure, subject to retry policy); a future refinement to classify error types is flagged.

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

**Aggregator cancellation protocol.** When an aggregator transitions from `pending` to `ready` via `first_success` or via a quorum-with-cancel-remaining satisfied condition, the executor identifies still-running sibling predecessors and cancels them. The protocol enumerates the aggregator's incoming edges whose sources are in state `ready` or `running`, then issues cancellation events. For worker nodes this means `worker.cancel` RPC with a 30-second grace period, then SIGTERM, then SIGKILL after another 10 seconds. For gate nodes with `rpc_query`, the RPC is abandoned and the node transitioned directly to `cancelled`. For gate nodes with `human_approval` still pending, the approval request is withdrawn (marked `expired`) and the node transitioned to `cancelled`.

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

The `artifact_refs` table is the executor's view of the artifact layer. The artifact layer owns the storage and content addressing; `artifact_refs` is the join table letting the executor answer "has any predecessor published this artifact yet?" without a round-trip to the artifact store. This is the minimum interface the executor needs from the artifact layer; the full artifact protocol is a separately deferred chunk.

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
4. **Apply.** In a single SQLite transaction: insert the fragment's nodes into `dag_nodes` with state `pending`; insert the fragment's edges into `dag_edges`; mark the expansion record `applied`; write `audit_log` entries for the expansion. Commit.
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

## Bundle lifecycle: planning and approval

A bundle is the unit of human approval and execution. Its lifecycle starts when an idea (from any source) is picked up by a bundler agent and ends when the work is shipped, parked, or killed. This section covers the planning and approval portion. The execution portion follows.

**Planning** is done by a bundler agent. The bundler reads the inbound idea, consults memory (similar past bundles, calibration data, prior killed ideas with reasoning), drafts requirements, drafts an RFC, drafts a UX flow if relevant, drafts an implementation plan, drafts a verification plan, and decomposes the work into a task DAG with capability manifests. It also computes a complexity score and a risk score and writes a concerns section.

The bundler is required to populate the concerns section. "No concerns" on a high-risk bundle is treated as a calibration signal that something is off, not as confirmation that the bundle is safe.

**Pre-execution review tracks** run before the bundle reaches the human reviewer. Three specialist tracks, each emitting structured findings into the bundle artifact:

1. **General adversarial critique.** Generalist critic looking for weak reasoning, unaddressed counter-cases, scope creep, hidden complexity, mismatch between requirements and RFC, mismatch between RFC and implementation plan. Findings tagged `severity:{low|med|high}` and `status:{resolved|accepted-risk|unresolved}`.

2. **Security review.** Specialist security critic with a different prompt and a different lens: threat model, authentication and authorization, data handling, input handling (every external input treated as hostile), dependencies (CVEs, supply chain), secrets and tokens (no leaks into logs, error messages, client-side code, or git history), failure modes (fail closed vs. fail open). Output includes a structured threat model added to the bundle body when the bundle touches auth, data handling, external surfaces, secrets, billing, or PII (otherwise the threat model section is omitted, not stub-filled). Findings tagged `severity:{info|low|med|high|critical}` and `status:{resolved|accepted-risk|unresolved}`. Hard rules: critical findings always require explicit human review even when resolved; high findings disable auto-ship; bundles touching auth, billing, secrets, or PII require security sign-off and never auto-ship.

3. **QA / verification planning.** A QA agent that doesn't test the code (the code might not exist yet) but produces a Verification Plan: acceptance criteria (observable, testable conditions tied back to requirements), test surface (unit, integration, end-to-end, load, manual smoke, with coverage targets), pre-merge gates (CI, coverage threshold, security findings resolved, manual smoke checklist), post-ship verification (specific metrics, time windows, expected ranges; this is the data the post-mortem feedback loop will consume), and a rollback plan. Hard rules: no bundle reaches human review without a Verification Plan; bundles without a viable rollback auto-bump Reversibility to 3 in stakes scoring.

The same QA agent is called again post-execution to validate the shipped product against the Verification Plan it itself produced. This is a deliberate dual use: pre-execution, the QA agent does verification planning; post-execution, the QA agent runs the plan against the actual shipped artifact. The two jobs are genuinely different (planning vs. validation) but share infrastructure (the same agent, the same capability scope, the same rubrics) and naturally compose. Failed criteria post-execution trigger one of: auto-rollback (if rollback is machine-executable and stakes are Low), or a `status:verification-failed` issue that pulls the bundle back into the Human Review Board's Needs Input column. The Verification Report becomes input to the post-mortem feedback loop: it's calibration data on how well the Verification Plan predicted reality.

Pre-execution review tracks may emit capability requests as a separate output stream from findings. A security review agent flagging "this finding would be more reliable with secret-scanning tooling I don't have" generates both a finding (about the bundle) and a capability request (about the system).

**Approval matrix.** Complexity (0–10) and risk (0–10) are scored independently. Complexity factors: estimated lines of code touched (banded), number of components/subdirectories touched, number of worker tasks in the decomposition, cross-component coordination required, new abstractions introduced. Risk factors: touches security-sensitive paths (path patterns configurable per repo in `settings.json`), touches data-handling paths, touches public/external interfaces, reversibility, production proximity (currently 0 in v1), net-new dependency additions. Each factor's contribution is shown in the approval surface so the reviewer can sanity-check the math.

The 3×3 matrix gates bundles into one of four tiers:

|                | Complexity 0–3 (simple) | Complexity 4–6 (moderate) | Complexity 7–10 (complex) |
|----------------|-------------------------|---------------------------|---------------------------|
| Risk 0–2 (minimal)  | Auto-approve            | Auto-approve + notify     | Approve-with-summary       |
| Risk 3–5 (moderate) | Approve-with-summary    | Approve-with-summary      | Full human review          |
| Risk 6–10 (high)    | Full human review       | Full human review         | Full review + cooldown     |

The four tiers:

- **Auto-approve.** Bundler creates the feature branch and starts work without asking. The reviewer is informed via a single line in a daily digest.
- **Approve-with-summary.** Bundler posts a summary (proposal one-liner, complexity/risk scores with breakdown, files-to-be-touched list, test plan summary, estimated wall-clock duration, link to full RFC). Default if reviewer doesn't respond in the configured window depends on the cell: low-risk cells default-approve; moderate-risk cells default-hold (require explicit response). Configurable in `settings.json`.
- **Full human review.** Bundler posts the full RFC, verification plan, worker decomposition, capability requests, and concerns section. No default action; bundle waits indefinitely.
- **Full review with cooldown.** Same as full review, plus a minimum 1-hour dwell time before approval can be granted, even if the reviewer responds immediately. Forcing function for high-stakes changes: look at it, walk away, come back. Override available for genuine emergencies.

**Mandatory-review triggers** bypass the matrix entirely. These force full review regardless of complexity and risk: any modification to `AGENTS.md` files, the capability manifest itself, control-plane code or `settings.json`, GitHub Actions workflows or CI config; any new external dependency from a non-allowlisted source; deletion of more than N files (default 10); bundles whose own complexity/risk scoring is suspiciously low for the LOC estimate. These are listed in `settings.json` under `mandatory_review_triggers` and are extensible.

**Pre-execution review tracks run before the approval matrix.** Their outputs feed into the approval matrix decision. Auto-ship is gated on pre-execution outcomes: any unresolved security finding of medium severity or above disables auto-ship regardless of stakes; bundles without a viable rollback can never auto-ship; bundles touching auth, billing, secrets, or PII can never auto-ship. (This ordering was inferred during consolidation; the prior conversation didn't make it explicit. Confirm.)

**Bundlers may self-escalate** to a higher tier when the score-driven tier feels wrong. They may not self-de-escalate.

**Modification requests** (`/modify [instructions]` or its MCP equivalent) are the middle path between approve and reject. The bundler revises the proposal based on instructions and re-posts. Whether modification forces a re-score is an open question; the natural answer is yes, since modification can change the risk profile, but that means moderately-modified bundles can bounce between tiers in a way that feels confusing.

**Calibration loop.** After every bundle completes (success or failure), the orchestrator records pre-execution complexity and risk scores plus the actual outcome (success/failure, time taken, lines actually changed, tests added, rework cycles, post-merge issues). Deltas go into `memory/calibration/scoring-outcomes.jsonl`. Periodically, the orchestrator surfaces patterns ("Bundles scored complexity=3 actually averaged 5.2 — bundler under-estimating"), and the reviewer adjusts weights in `settings.json`. The post-mortem prompt fires when any tracked axis diverges from prediction by more than 50%.

**Mid-flight steering vocabulary.** Once a bundle is in execution, the reviewer can intervene with three primary verbs and one optional fourth:

- **Pause.** Orchestrator finishes the in-flight worker step, then halts. State is preserved. Worktrees stay. Resumable later, optionally with notes that get prepended to the orchestrator's context.
- **Redirect.** Pause plus new instructions; orchestrator re-plans from the current state. Previous plan archived. Workers may be killed and respawned with new assignments.
- **Abort.** Kill all workers immediately, close the draft PR, delete worktrees, terminal aborted state. Partial commits remain on the feature branch (recoverable), but active execution is dead.
- **Rollback.** Post-merge or post-deploy only. Reverts the merged change and re-deploys. Distinct from Abort, which is for in-flight execution.

This vocabulary was Claude-recommended after the reviewer explicitly said "I honestly do not know the answer" during the design conversation, and was folded into the spec without being explicitly ratified. Treating it as accepted by default; flagged in open questions for confirmation.

## Bundle lifecycle: execution and integration

Once a bundle is approved, the orchestrator transitions it to `approved`, then to `in_progress`, and execution begins. The mechanics of how the executor drives the task DAG (node lifecycle, scheduling, ready-set computation, gate and aggregator semantics, dynamic expansion, retry policies, crash recovery) are specified in the DAG executor section. This section covers the lifecycle-level concerns above the executor: how decomposition is structured, how state is shared, how source trees are organized, how integration and merge work, and how repos are spawned for shipped artifacts.

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

Not every bundle should spawn a new repo. Some bundles modify existing product repos. Some bundles are internal to the control-plane itself. The bundle's RFC declares its target via a `target:` field: `new-repo`, `existing-repo:<name>`, or `control-plane`. The exact specification of this field is open (named but not formalized in the conversation), and the implicit boundary between "control-plane" and "product" content isn't fully drawn.

**Failure handling during execution** uses a tiered policy similar to stakes:

- Low-impact failures (flaky test, transient network blip): auto-retry with backoff.
- Mid-impact failures (dependency conflict, ambiguous requirement requiring clarification): surface to human as Needs Input.
- High-impact failures (production deploy fails, irreversible state corruption detected): immediate rollback plus alert.

Production deploy failures are not currently a relevant case, since there is no production in v1.1. The tier exists in the policy for when production becomes real.

**Worker lifecycle.** Orchestrator picks worker class C for task T based on T's requirements, spawns the worker via WorkerRunner, gives it its own working tree on its own branch, gives it its declared capability set, and gives it pointers to its declared input artifacts. The worker reads its task spec (passed via env var or mounted file): bundle context, RFC excerpt, verification plan excerpt, conditions, `AGENTS.md`, capability manifest subset, model and thinking-mode config. It invokes OpenCode against the configured Ollama Cloud model. It emits heartbeats on state transitions and at maximum 60-minute intervals, including the `phase` field. It may request capability expansion mid-task via RPC; this goes through the approval flow before new workers spawn. On completion (success, failure, or stuck), it commits to its sub-branch, emits a `worker-report.json`, and exits. The orchestrator reviews exit state, merges the sub-branch to the feature branch (or kills it), updates `tasks.json`, and decides next steps.

Workers that go stuck (hit the configured per-worker-class timeout, currently 3 stuck-iterations as the kill-and-respawn threshold) are killed and reassigned. The reset-and-iterate pattern (small bounded tasks, kill-and-respawn over long-context grinding) was an intentional adoption from the production-tested patterns surveyed during the orchestrator design conversation.

**Verification handoff.** When all workers in a bundle complete, the lead orchestrator marks execution complete and emits a `verification-requested` event. The QA agent picks up the bundle plus the Verification Plan it produced pre-execution and runs it against the merged bundle branch. The verification result feeds back: bundle marked Verified (close out and proceed to merge) or Failed (recall sequence per the failure handling tiers).

## Surfaces

There are three surfaces into the orchestrator state machine, all reaching the same source of truth, all interchangeable for actions: MCP server (primary), GitHub Issues (secondary), CLI (tertiary).

**MCP server, primary.** Runs as its own process on dev.learhy.net, alongside the orchestrator. Talks to the orchestrator over a Unix domain socket. Exposes remote MCP over HTTPS via Caddy with a long-lived bearer token in the Claude Desktop config. Fallback transport is an stdio bridge running on the laptop that tunnels to the box over SSH (same server, different transport).

This was chosen over GitHub Issues as primary because Claude Desktop is increasingly the reviewer's actual primary surface. MCP makes Claude Desktop a thinking surface, not just a notification surface: the reviewer can interrogate a bundle ("what does worker 3 actually do?", "what's the rollback plan if migration fails?", "show me last week's bundles that touched this subdirectory") before deciding, instead of evaluating a static template.

The MCP method surface:

- Tools (write/action): `list_pending_bundles(filter?)`, `get_bundle(id)`, `approve_bundle(id, comment?)`, `reject_bundle(id, reason)`, `request_modification(id, instructions)`, `escalate_bundle(id, reason)`, `pause_bundle(id)`, `resume_bundle(id)`, `kill_worker(bundle_id, worker_id, reason)`, `grant_capability(request_id, scope?, expiry?)`, `revoke_capability(capability_id, reason)`.
- Resources (read-only context): `studio://bundles/pending`, `studio://bundles/{id}`, `studio://bundles/{id}/workers`, `studio://workers/active`, `studio://workers/{bundle_id}/{worker_id}/report`, `studio://capabilities/manifest`, `studio://capabilities/pending-requests`, `studio://memory/agents/{repo}`, `studio://calibration/recent`, `studio://decisions/recent`, `studio://system/status`.
- Prompts (canned interaction patterns): `review-pending`, `morning-digest`, `risk-audit`, `bundle-deep-dive`.

Approval actions always require explicit human gesture: Claude Desktop can recommend, the human must click. This is just MCP's normal tool-confirmation behavior, but it's a deliberate design constraint, not an accident. Claude Desktop is itself an LLM-mediated surface, and asking it "should I approve this?" gets another model's recommendation on top of the bundler's proposal. That's mostly a feature (independent review), but the action itself stays explicit.

The MCP tools are designed to make it easy for Claude Desktop to surface its own uncertainty back to the human, not paper over it. Tool outputs include the bundler's concerns section verbatim; resources include calibration data so Claude can say "the bundler has been under-estimating complexity by ~30% lately, factor that in."

**GitHub Issues, secondary.** Each bundle proposal opens an issue in the control-plane repo with a structured template. Labels indicate tier (`approval/auto`, `approval/summary`, `approval/full-review`, `approval/cooldown`). Decisions can be expressed via comments (`/approve`, `/reject [reason]`, `/full-rfc`, `/modify [instructions]`). The orchestrator watches issue events via webhook and acts accordingly. This surface serves three roles: durable audit trail of record (every MCP action mirrors as a comment on the corresponding issue), mobile fallback (when the reviewer is away from Claude Desktop), and alternate write path.

**CLI, tertiary.** `studio approve <bundle-id>`, `studio list-pending`, `studio show <bundle-id>`. Useful when the reviewer is SSH'd into the box. Doesn't replace the issue or MCP flow but supplements it. Both surfaces produce the same orchestrator action.

**Cross-surface sync.** MCP actions mirror to GitHub Issues as comments (audit trail); GitHub Issue actions mirror to the MCP-side decision log. Several open-question implementation details remain (multi-surface action ordering, race-condition resolution if approval races rejection across surfaces within the same minute) and are deferred to implementation-time decisions, not architecture-time decisions.

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

**Artifact protocol details.** RPC method semantics for artifact read and write (`artifact.publish`, `artifact.fetch`, `artifact.list`), content-addressing scheme for global artifacts, garbage collection policy for bundle-scoped and task-scoped artifacts, the `secrets.fetch` RPC method semantics. Multiple parts of v1.1 reference artifact descriptors and the executor specifies the minimum interface it needs (the `artifact_refs` join table and a notification channel for new-publication events), but the full lifecycle isn't specified.

**Bundle-level input/output schema.** Bundles take inputs (the user's request) and produce outputs (the result). The task-level input/output is formalized in the task DAG node spec; bundle-level is not. Should mirror task spec.

**Schema versioning policy.** Applies to the capability manifest schema and the task DAG schema. Forward-compatible additions only? Deprecation cycles? Migrations? Currently both schemas have `schema_version: "1.0"` but the upgrade rules are not specified.

**Capability manifest review UX.** How the human approval flow actually presents a manifest. Reviewers need tooling.

**Hostname-based egress enforcement implementation.** The egress proxy with name-based filtering. The mechanism is clear in principle; the operational details (cache strategy, what happens when DNS resolves to multiple IPs, how to handle TLS SNI versus plaintext HTTP) are not.

**Two-tier repo boundary.** Specifically the `target:` field in bundle RFCs. Named but not specified. The implicit boundary between "control-plane content" and "product content" isn't fully drawn.

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

**Modification during pause: executor's role in re-planning.** The mid-flight steering vocabulary includes Pause and Redirect. Redirect means "pause plus new instructions; orchestrator re-plans." The executor's role in re-planning is not specified: does it re-ingest a new DAG while preserving completed nodes, or is the current DAG abandoned? The natural answer is that Redirect discards the current DAG and runs the planner on the current worktree state as a fresh bundle, with completed work part of the baseline; but that's a bundle-state-machine decision, not an executor decision. Flagged for the next pass on the steering vocabulary.

**Gate `rpc_query` retry semantics.** A failing `rpc_query` is a gate failure subject to retry policy. Should transient RPC failures (network blips) be distinguished from predicate-false responses? The current design treats them the same, which means a bundle can be defeated by a flaky endpoint. A future refinement might classify errors: transient RPC errors trigger retry without consuming an attempt; predicate-false responses count as attempts.

## Open questions and flagged decisions

Items where the prior conversation explicitly punted, raised a concern, or made a call that should be revisited.

**Mid-flight steering vocabulary acceptance.** The Pause / Redirect / Abort / Rollback verbs were Claude-recommended after the reviewer said "I honestly do not know the answer." Folded in without explicit ratification. Open question: ratify, revise, or expand.

**Provisional wall-clock and heartbeat numbers.** First-run timeout defaults of 2 hours for small tasks, 4 hours for medium, 8 hours for large. 60-minute maximum heartbeat interval. These are explicitly provisional; they need to survive first contact with real workloads before being ratified.

**Pre-execution review ordering.** Pre-execution review tracks run before the approval matrix; their outputs feed the matrix decision; auto-ship is gated on pre-execution outcomes. This ordering was inferred during consolidation; the prior conversation did not make it explicit. Confirm.

**Modification request re-scoring.** When a bundle is modified via `/modify`, should the bundler re-score it? Natural answer is yes, since modification can change the risk profile, but moderately-modified bundles bouncing between tiers can confuse the surface. Not committed.

**Default action for summary-tier timeouts.** Default-approve after 4 hours for low-risk-cells, default-hold for moderate-risk. Open whether to default-hold across the board until trust is built.

**Cooldown duration for the highest-risk tier.** Currently 1 hour. Some patterns argue for 24-hour "sleep on it" rules for irreversible changes.

**Multi-surface action ordering and race resolution.** What happens if approval-via-MCP races reject-via-Issues within the same minute? Implementation-time decision, not architecture-time.

**Friction-pattern aggregation threshold for capability requests.** "N reports over a window." A reasonable starting heuristic is 3 reports in 7 days. Not committed.

**Whether agents may request capabilities for other agents.** E.g., the bundler notices that critique agents would benefit from X. Adds power and adds noise; if allowed, flag as second-hand.

**"Self-imposed limits" surface.** Inverse of capability requests: agents flagging "I have access to X but I don't think I should be using it for this task" or "I notice I have permission to do Y but this seems risky." Architecturally interesting; lower priority.

**Agent activity issues, same repo or separate.** Currently same-repo. Revisit if access-scope or volume issues emerge.

**Two-tier repo `target:` field semantics.** Named but not specified. The implicit boundary between control-plane content and product content isn't drawn.

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

*Approval-matrix vs. pre-execution-review-track ordering.* Both are gates between bundle proposal and bundle execution. The natural ordering (review tracks run first, their outputs feed the matrix) was assumed in this consolidation but not explicitly stated in the prior conversation. Confirm.

**Areas the next design phase should plan to address.**

The DAG executor is the next major design chunk. Everything from decomposition onward is a constraint on it; nothing builds on top of it yet because it doesn't exist. The deferred items list above includes all of its sub-questions.

The artifact protocol is the second major chunk. Multiple parts of v1.1 reference artifact descriptors but the lifecycle (creation, retention, garbage collection, capability scoping) isn't specified. The `secrets.fetch` RPC is a specific instance.

A migration plan from v1 numerics to v1.1 (when v1.1 actually reaches a state that wants to revisit them) should be laid down before the surrounding system gets larger. If those numerics drift, they should drift deliberately.

A protocol for flagging supersessions during design is worth adopting explicitly. The drift items above all share a structure: a later turn made a decision that obsoleted an earlier one without saying so. A simple rule like "if a new decision changes a previous one, flag both with a supersession note" would have caught most of these.

End of document.
