# AGENTS.md — Studio Orchestration System

## What this repo is

This is the Studio agent orchestration system: a Python async orchestrator that accepts bundle submissions, drives worker tasks through a DAG executor, enforces capability-based security, and writes outcomes to SQLite. Phase 1 shipped the linear kernel. Phase 2 added the bundler agent, full DAG executor, artifact protocol, review tracks, approval matrix, GitHub Issues surface, MCP server, and QA verification. Phase 3 hardened the system with egress proxy, schema versioning, ops tooling, security features (token expiry, secret rotation, audit), and rate-limit-aware GitHub integration. Phase 4 extended worker execution to remote hosts via SSH fleet, Kubernetes Jobs, and Docker containers, added TCP/TLS transport with mTLS, and added RunnerSelector for mixed-fleet operation.

## Layout

```
studio/
  orchestrator/    # kernel: DB, state machine, RPC, executor, runner, CLI, main
    db.py          # SQLite schema v11, connection pool, decorator-based migrations
    models.py      # Pydantic models for submissions, manifests, settings, DAG, review
    state_machine.py  # 20+ bundle lifecycle transitions
    rpc.py         # JSON-RPC 2.0 dispatcher, connection manager, worker auth
    runner.py      # 4 runner impls + RunnerSelector + capability translation functions
    executor.py    # DAG executor: worker, gate, aggregator nodes + dynamic expansion
    scheduler.py   # Periodic dispatch + heartbeat timeout checks
    reconciler.py  # Kill-all crash recovery
    capability.py  # op_descriptor parsing + is_subset() checking
    github.py      # GitHubClient (App JWT auth) + GitHubRateLimiter
    approval.py    # Deterministic approval matrix evaluator
    artifact.py    # ArtifactStore (publish/fetch/list/GC) + SecretStore
    proxy.py       # Egress proxy (CONNECT tunnel, SNI inspection)
    ops.py         # Stall detection, escalation ladder, recall, health
    notify.py      # Notification dispatcher (file log + GitHub issue comments)
    visualizer.py  # Mermaid DAG renderer
    expression.py  # Expression evaluator for artifact gate predicates
    reducers.py    # Aggregator reduce functions
    tls.py         # mTLS: CA bootstrap, worker certificate issuance
    settings.py    # Settings loader (JSON → Pydantic)
    cli.py         # CLI entry point (submit/approve/reject/list/show/kill/status/...)
    main.py        # Orchestrator: wires all subsystems, Starlette HTTP server, polling
  workers/         # worker processes
    bundler.py     # Bundler agent: idea → proposal + DAG
    developer.py   # Developer agent: implements tasks in worktrees
    review.py      # Review tracks: adversarial, security, QA roles
    qa.py          # Post-execution QA verification agent
    worker.py      # Base worker: RPC client, heartbeat pump, artifact helpers
  mcp/             # MCP server (separate process)
    server.py      # Starlette MCP HTTP server
    tools.py       # MCP tool definitions (submit, approve, reject, modify, status)
    resources.py   # MCP resource handlers (bundles, capabilities)
  tests/           # 948 unit tests across 35 test files
  systemd/         # studio-orchestrator.service
docker/            # Container images
  Dockerfile.orchestrator
  Dockerfile.worker
  Dockerfile.proxy
deploy/helm/studio-workers/  # Helm chart for k8s runner (ServiceAccount, RBAC, NetworkPolicy)
docker-compose.yml           # Orchestrator-in-Docker with dynamic worker spawning
pyproject.toml               # Python 3.12, hatchling build
settings.json.example        # All runtime configuration reference
```

## Key invariants

- **Single SQLite writer.** The orchestrator process is the only writer. WAL mode enabled.
- **Capability check on every RPC.** Every method call is validated against the worker's manifest.
- **Manifest subset enforcement.** DAG expansion nodes must have manifests that are subsets of the bundle-level approved grant (`capability.is_subset()`).
- **Kill-all on crash recovery.** No attempt to resume in-flight workers after orchestrator crash. Reconciler kills everything.
- **Event pump is the single mutator.** Executor state changes go through one async task (game-engine tick pattern).
- **All schema-mutating operations are in transactions.** Atomic multi-table writes via `db.transaction()`.
- **Worker tokens are 256-bit random with expiry.** Default 15-minute expiry. RPC rejects expired tokens. Passed via `STUDIO_WORKER_TOKEN` env var.
- **Schema versioning via PRAGMA user_version.** Sequential `@migration(N)` decorators. `SCHEMA_VERSION` constant in db.py must match migrations.
- **Content schema version validated at boundary.** Capability manifests, task DAGs, submissions — all checked against `KNOWN_*_VERSIONS` sets before acceptance.
- **GitHub API is non-blocking.** All calls return safe defaults on failure, never raise. Rate limiter paces calls when below threshold.

## Bundle lifecycle (full)

```
(none) ──1──► PROPOSED ──→ IN_REVIEW ──4──► APPROVED ──6──► IN_PROGRESS
                 │  ↑          │                    │
                 │  └──3───────┘                    ├──8──► PAUSED ──→ ...
                 │  (modify)                        │
                 └──1b──► REJECTED                  ├──9──► VERIFYING ──17──► COMPLETE
                                                    │        │
                                                    │        └──19──► FAILED
                                                    │
                                                    ├──25──► FAILED (execution)
                                                    └──7──► FAILED (timeout)
```

Transition 1 fires immediately on submit. For bundle-input-only (no pre-built DAG), bundler worker spawns, produces proposal + DAG, then transition completes to IN_REVIEW. Transition 4 gates on approval matrix evaluation (tier-based: AUTO → immediate, FULL_REVIEW → human decision, FULL_REVIEW_COOLDOWN → timed wait).

## Two submit paths

- **Kernel-direct** (Phase 1): submit with `task_dag` present → bundle goes PROPOSED → kernel approve → APPROVED → execution
- **Bundle-input-only** (Phase 2+): submit with just `bundle_input` (no `task_dag`) → bundle goes PROPOSED → bundler worker spawns → bundler produces proposal + DAG → PROPOSED → IN_REVIEW

## DAG execution model

The executor supports three node kinds:

- **worker**: Spawns an isolated worker subprocess. The runner translates the capability manifest into bubblewrap arguments. Worker connects back over Unix socket with token auth.
- **gate**: Blocks until a predicate is satisfied. Supported: `artifact_property` (evaluates on_property expressions against published artifacts), `human_approval` (waits for CLI/MCP/GitHub approval). `rpc_query` is stubbed (DEFERRED).
- **aggregator**: Collects outputs from multiple upstream nodes. Join modes: `all`, `any`, `quorum`, `first_success`. Output strategies: `collect`, `first`, `reduce`.

## Dynamic DAG expansion

Workers can request DAG expansion via `cap.request` RPC. The executor validates the fragment's node manifests are subsets of the bundle-level approved grant, inserts new nodes/edges, validates no cycles (50-node limit), and grafts onto the DAG. Expansions that exceed capability scope are denied with audit log entry.

## Review tracks + approval matrix

Three review roles (adversarial, security, QA) run in parallel after bundler planning. The review aggregator collects findings and fires `_evaluate_approval_matrix()`. The approval matrix evaluates deterministically:

- **complexity_score + risk_score** → tier (auto / auto_notify / summary / full_review / full_review_cooldown)
- **Mandatory review triggers** can force full_review (path patterns, tag matches, new repos)
- **Cooldown** applies for full_review_cooldown tier (reversible: 1h, irreversible: 24h)
- Self-escalation by bundler is honored but cannot downgrade

## GitHub Integration

Bundles in IN_REVIEW get a GitHub Issue created automatically (via `GitHubClient.create_issue`). PMs interact via slash commands (`/approve`, `/reject <reason>`, `/modify <instructions>`). The orchestrator polls issues every 60s (per-bundle tracking to reduce API calls) and processes a webhook endpoint at `/github/webhook` with HMAC-SHA256 signature validation. Bot comments are filtered out to prevent self-triggering. API calls are rate-limit-aware (back off when remaining < 100).

## Secret store

Secrets live in `memory/secrets/<name>.json` (file store takes precedence over env vars). The `SecretStore` supports hybrid lookup, provisioning, and rotation. `studio rotate-secret <name>` generates a new token_hex(32) value, writes to file store, and audit-logs affected workers. Workers that fetched the old secret continue using it until re-fetch.

## Egress proxy

Per-worker egress proxy enforces network grants. Each worker gets a dedicated Unix socket proxy process (or sidecar container for k8s/Docker runners). The proxy handles HTTP CONNECT tunneling with TLS SNI inspection for HTTPS destinations. HTTP requests are rewritten with the proxy as forward proxy. Identical egress enforcement semantics across all four runner types.

## Worker runners

Four runner implementations, all sharing the same `WorkerRunner` interface (`spawn_worker`, `kill_worker`):

- **LocalBwrapWorkerRunner**: Bubblewrap-isolated subprocesses on the orchestrator host. Full bwrap enforcement of exec_allowlist, filesystem isolation, and network namespaces. Used for development and single-machine deployments.
- **RemoteSSHWorkerRunner**: Workers on a managed fleet of Linux hosts via SSH + bubblewrap. Fleet registry in `settings.json` with per-host semaphores, health pings, and `least_loaded` / `round_robin` selection policy. Identical bwrap isolation model as local.
- **K8sJobWorkerRunner**: Workers as Kubernetes Jobs with sidecar egress proxy, NetworkPolicy egress enforcement, init containers for git clone, and Pod event watching for eviction/OOMKill detection. Helm chart at `deploy/helm/studio-workers/` provisions RBAC. Exec allowlist is enforced at RPC level, not kernel level — `allow_unenforced_grants` must be enabled.
- **DockerWorkerRunner**: Workers as sibling Docker containers. Per-worker internal network, named volumes for worktree and proxy socket, proxy sidecar with shared volume. Enables macOS/Windows support via Docker Desktop. Same security defaults as k8s (`--read-only`, `--no-new-privileges`, `--cap-drop ALL`, `--user 10000:10000`).

### RunnerSelector

`RunnerSelector` routes each task to the appropriate runner based on:

1. `runner_preference` in the task spec (`local`, `remote_ssh`, `k8s`, `docker`, or `any`)
2. Capability compatibility via `capability_to_runner_compatibility()` — k8s and Docker runners report `exec_allowlist` as unenforced
3. `allow_unenforced_grants` setting — if false, incompatible runners are skipped
4. Falls back to `default_preference` when task doesn't specify one

Selected runner type is recorded on the worker row and audit-logged.

## Ops tooling

Runs on a 60s loop:
- **Stall detection**: bundles in IN_PROGRESS > 8h without heartbeat → escalation
- **Escalation ladder**: at 5/10/21 days for high-stakes parked/cooldown bundles
- **Acting-soon**: 12h window before auto-action on summary-tier bundles
- **Health**: `studio health` returns orchestrator_ok, db_ok, uptime, active/stalled counts, by_state, by_tier breakdowns
- **Recall**: `studio recall <bundle-id>` within 48h window creates a reversal bundle through the normal bundler flow

## Security features (Bundle 3.4)

- **Worker token expiry**: 15-minute default, validated on every auth + RPC call
- **Capability audit**: `studio audit <bundle-id>` reports granted/used/unused/over-granted capabilities
- **Secret rotation**: `studio rotate-secret <name>` with audit trail
- **Expansion subset check**: Denies DAG expansions with nodes exceeding bundle grant scope
- **Audit log completeness**: worker_spawned, auth_failure (invalid/expired token), secret_access, secret_rotated, dag_expansion_denied, capability_check entries

## Testing

```bash
uv run python -m pytest studio/tests/ -v       # 948 tests
STUDIO_TEST_MODE=1 bash studio/tests/acceptance.sh  # acceptance tests
```

Test mode uses `NoopWorkerRunner` (no bubblewrap needed) and in-memory/temp-file databases. Key test files:

| File | Coverage |
|------|----------|
| test_db_migrations.py | Schema creation, all 11 migrations |
| test_state_machine.py | All bundle lifecycle transitions |
| test_executor.py | DAG execution, gates, aggregators, expansion |
| test_rpc.py | Auth, dispatch, heartbeat, artifact, secrets RPC, TCP/TLS transport |
| test_runner.py | Bwrap arg building, worker spawning |
| test_approval.py | Approval matrix tiers, cooldown, triggers |
| test_review.py | Review track workflows |
| test_artifact.py | Artifact publish/fetch/list/GC |
| test_github.py | GitHubClient, issue creation, comment parsing |
| test_qa.py | QA verification worker |
| test_developer.py | Developer worker |
| test_bundler.py | Bundler agent: idea → proposal + DAG |
| test_proxy.py | Egress proxy |
| test_mcp.py | MCP server + RPC client |
| test_ops.py | Stall detection, escalation, recall, health |
| test_security.py | Token hardening, subset check, secret store, audit log |
| test_reconciler.py | Crash recovery |
| test_scheduler.py | Periodic dispatch |
| test_capability.py | op_descriptor parsing, is_subset checking |
| test_cli.py | CLI argument parsing and output formatting |
| test_db.py | Connection pool, transaction handling |
| test_expression.py | Artifact property expression evaluator |
| test_fixtures.py | Test fixture validation |
| test_main.py | Orchestrator subsystem wiring, HTTP endpoints |
| test_models.py | Pydantic model validation |
| test_visualizer.py | Mermaid rendering |
| test_reducers.py | Reduce functions |
| test_worker.py | Worker base class |
| test_docker_runner.py | DockerWorkerHandle, capability_to_docker_args, DockerWorkerRunner, CLI handlers |
| test_kubernetes_runner.py | capability_to_pod_spec, K8sWorkerHandle, K8sJobWorkerRunner |
| test_runner_selector.py | RunnerSelector routing, compatibility checks, mixed-fleet dispatch |
| test_tls.py | CA bootstrap, worker certificate issuance, mTLS configuration |
