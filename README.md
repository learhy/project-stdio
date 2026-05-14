# Studio — Agent Orchestration System

Python async orchestrator that accepts bundle submissions, drives worker tasks through a DAG executor under bubblewrap isolation, enforces capability-based security, and writes outcomes to SQLite.

## Overview

Studio is an agent orchestration system where a central orchestrator process manages the lifecycle of "bundles" — structured task DAGs executed by isolated worker subprocesses. Workers communicate with the orchestrator over a Unix domain socket via JSON-RPC 2.0, sending heartbeats, logs, progress reports, and final outcomes.

### Architecture

```
CLI / MCP / GitHub Issues (approval surfaces)
  │
  ▼
Orchestrator (single process)
  ├── SQLite (WAL mode) — single writer, schema v6
  ├── BundleStateMachine — 20+ transitions, 12 states
  ├── RpcDispatcher — worker RPC + CLI handlers
  ├── ConnectionManager — Unix socket, token auth with expiry
  ├── DagExecutor — worker / gate / aggregator nodes + dynamic expansion
  ├── LocalBwrapWorkerRunner — bubblewrap isolation + egress proxy
  ├── GitHubClient — JWT App auth, issue management, rate-limit aware
  ├── Scheduler — periodic dispatch + heartbeat checks
  ├── Reconciler — kill-all crash recovery
  ├── SecretStore — hybrid file/env secrets with rotation
  ├── OpsTooling — stall detection, escalation ladder, recall
  ├── Starlette HTTP server — /health + /github/webhook (HMAC-SHA256)
  └── Calibration loop — estimated-vs-actual scoring outcomes
  │
  ▼
Worker subprocesses (bubblewrap containers)
  ├── Bundler — idea → proposal + DAG
  ├── Developer — implements tasks in git worktrees
  ├── Review (adversarial / security / QA) — pre-execution review tracks
  └── QA — post-execution verification
```

## Quick start

```bash
# Create virtual environment and install
uv venv
uv pip install -e ".[dev]"

# The studio CLI is now available:
#   uv run studio <command>          (no activation needed)
#   source .venv/bin/activate && studio <command>  (activate first)

# Run tests
uv run python -m pytest studio/tests/ -v

# Start orchestrator (test mode — no bwrap needed)
STUDIO_TEST_MODE=1 STUDIO_ORCH_DB_PATH=/tmp/studio.db \
  STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.main &

# Submit a bundle (bundler path — idea only, via JSON file)
echo '{"bundle_input": {"idea": "Add a logout button to the settings page"}}' > /tmp/idea.json
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.cli submit /tmp/idea.json

# Submit a bundle (kernel-direct path — pre-built DAG)
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.cli submit \
  studio/tests/fixtures/hello-world.json

# Check the bundle state
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.cli show <bundle-id>

# Approve it
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.cli approve <bundle-id>

# Run acceptance tests
STUDIO_TEST_MODE=1 bash studio/tests/acceptance.sh

# Start MCP server (test mode — pointed at same orchestrator)
# STUDIO_DB_PATH must match STUDIO_ORCH_DB_PATH so the MCP server
# reads from the same database.
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  STUDIO_DB_PATH=/tmp/studio.db \
  STUDIO_MCP_PORT=8080 \
  STUDIO_MCP_TOKEN=test-token-123 \
  uv run python -m studio.mcp.server &
```

## CLI command reference

All commands are invoked as `uv run studio <command>` (or `studio <command>` after activating the venv).

### Bundle management

| Command | Description |
|---------|-------------|
| `studio submit <file.json>` | Submit a bundle JSON file (bundler path if `bundle_input` present, kernel-direct if `task_dag` present) |
| `studio approve <id>` | Approve and start execution |
| `studio reject <id> [-r reason]` | Reject a proposed bundle |
| `studio list [--state s] [--tier t] [--json]` | List non-terminal bundles with tier and age |
| `studio show <id> [--verbose] [--json]` | Show bundle detail: review deck with complexity, risk, estimates, plan, concerns, DAG status, and recent events |
| `studio kill <id>` | Kill a running bundle's workers |

Example `studio show` output:

```
Bundle: 01KRHT91RB58JND8JTN1XWMYBN
State: in_review (pending_review) — age 5m
Idea: Build a hello-world app using flask and docker.

Complexity: 2/10    Risk: 1/10    Irreversible: no
Estimate: 50 loc · 1m 0s · 1 worker(s) · 500 tokens
Plan: Create app.py with Flask and single GET / route returning JSON
Concerns: Test mode — no real planning performed

DAG: 5 nodes (0 completed, 0 running, 5 pending)

Recent events:
  5m ago  bundle_input_received — proposed (idea only)
  5m ago  bundle_planning_complete — proposed → in_review

Approve: studio approve 01KRHT91RB58JND8JTN1XWMYBN
```

#### Bundle JSON format

**Kernel-direct (pre-built DAG):**

```json
{
  "schema_version": "1.0-phase-1",
  "bundle_input": {
    "idea": "Add a hello-world endpoint to the API",
    "filed_by": "developer",
    "filed_at": "2026-05-08T10:00:00Z",
    "target_repo": "control-plane",
    "priority_hint": "normal"
  },
  "capability_manifest": {
    "schema_version": "1.0",
    "grants": {
      "filesystem": { "reads": [...], "writes": [...] },
      "network": { "egress": [...], "dns": {"enabled": true} },
      "process": { "exec": [...] },
      "rpc": { "methods": [...] },
      "resources": { "wall_time_limit": 3600, "llm_token_budget": {...} }
    }
  },
  "task_dag": {
    "schema_version": "1.0",
    "nodes": [{"id": "...", "kind": "worker", "spec": {"objective": "..."}}],
    "edges": [],
    "entry_nodes": ["..."],
    "exit_nodes": ["..."]
  }
}
```

`task_dag` presence triggers the kernel-direct path. The bundle goes PROPOSED → (kernel approve) → APPROVED → IN_PROGRESS.

**Bundle-input-only (idea, bundler path):**

```json
{
  "bundle_input": {
    "idea": "Add a logout button to the settings page",
    "filed_by": "developer",
    "filed_at": "2026-05-11T10:00:00Z",
    "target_repo": "control-plane"
  }
}
```

No `task_dag` triggers the bundler path. The orchestrator spawns a bundler worker that produces a proposal + DAG, then the bundle enters IN_REVIEW.

### Operational

| Command | Description |
|---------|-------------|
| `studio status` | Show orchestrator health and active bundles |
| `studio health` | Detailed health: uptime, DB status, state/tier breakdowns, recent errors |

Example `studio health` output:

```
Orchestrator: OK | DB: OK | Uptime: 5m 30s
Bundles: 3 total, 1 in_progress, 2 in_review, 0 stalled
  By state        By tier
  in_progress   1  pending_review  2
  in_review     2  full_review     1
Calibration: 12 outcomes, pass rate 75%
Recent errors: (none)
```
| `studio show-worker <id>` | Show worker detail, phase, heartbeat age, recent logs |
| `studio recall <bundle-id>` | Recall a completed bundle within 48h (creates reversal bundle) |

### Security

| Command | Description |
|---------|-------------|
| `studio audit <bundle-id>` | Report capability grants: used, unused, over-granted, denied operations |
| `studio rotate-secret <name>` | Rotate a secret: invalidate old value, provision new, audit affected workers |

### Calibration

| Command | Description |
|---------|-------------|
| `studio calibration-report` | Print estimated-vs-actual scoring outcomes from memory/calibration/ |

## Configuration reference (settings.json)

All keys and their defaults:

### `kernel`
| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `true` | Kernel mode (Phase 1 direct approve/reject) |

### `orchestrator`
| Key | Default | Description |
|-----|---------|-------------|
| `socket_path` | `/run/studio/orchestrator.sock` | Unix socket for worker + CLI connections |
| `db_path` | `/var/lib/studio/state.db` | SQLite database path |
| `socket_permissions` | `"0660"` | Unix socket permissions |
| `socket_owner` | `"studio:studio"` | Unix socket owner |
| `memory_root` | `"memory/"` | Root for secrets, calibration, notifications, post-mortems |
| `http_port` | `7810` | HTTP listener port for health + GitHub webhook |

### `worker`
| Key | Default | Description |
|-----|---------|-------------|
| `global_concurrency` | `4` | Max concurrent worker subprocesses |
| `default_timeout_hours.small` | `2` | Timeout for small tasks |
| `default_timeout_hours.medium` | `4` | Timeout for medium tasks |
| `default_timeout_hours.large` | `8` | Timeout for large tasks |
| `heartbeat_max_interval_minutes` | `60` | Max time between heartbeats |
| `heartbeat_timeout_multiplier` | `2.0` | Multiplier on expected duration for timeout |

### `egress_proxy`
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable per-worker egress proxy |
| `socket_dir` | `/run/studio` | Directory for proxy Unix sockets |
| `connect_timeout_seconds` | `10` | Upstream connect timeout |
| `read_timeout_seconds` | `30` | Upstream read timeout |

### `github`
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable GitHub Issues integration |
| `app_id` | `""` | GitHub App ID |
| `installation_id` | `""` | GitHub App installation ID |
| `private_key_path` | `""` | Path to App private key PEM file |
| `webhook_secret` | `""` | HMAC-SHA256 secret for webhook validation (empty = skip validation) |
| `poll_interval_seconds` | `60` | Seconds between issue comment polls |
| `owner` | `""` | GitHub repo owner |
| `repo` | `""` | GitHub repo name |

### `approval`
| Key | Default | Description |
|-----|---------|-------------|
| `low_complexity_max` | `3` | Max score for "low" complexity |
| `med_complexity_max` | `6` | Max score for "medium" complexity |
| `low_risk_max` | `2` | Max score for "low" risk |
| `med_risk_max` | `5` | Max score for "medium" risk |
| `summary_tier_default_action` | `"hold"` | Default for summary tier: "hold" or "ship" |
| `default_action_overrides` | `{}` | Per-bundle-type action overrides |
| `summary_timeout_hours` | `4` | Hours before summary-tier auto-action |
| `cooldown_hours_reversible` | `1` | Cooldown for reversible changes |
| `cooldown_hours_irreversible` | `24` | Cooldown for irreversible changes |
| `mandatory_review_triggers` | `[...]` | Triggers that force full_review (path patterns, tag matches, new repo) |

### `ops`
| Key | Default | Description |
|-----|---------|-------------|
| `stall_threshold_hours` | `8` | Hours without progress before stall escalation |
| `escalation_days` | `[5, 10, 21]` | Days before escalating parked/cooldown bundles |
| `recall_window_hours` | `48` | Hours after completion that recall is allowed |
| `acting_soon_hours` | `12` | Hours before auto-action to notify PM |
| `worker_token_expiry_minutes` | `15` | Worker token lifetime in minutes |

### `artifacts`
| Key | Default | Description |
|-----|---------|-------------|
| `inline_threshold_bytes` | `4096` | Max bytes to inline in DB (larger → file store) |
| `global_storage_cap_bytes` | `50000000000` | 50 GB global artifact cap |
| `per_bundle_cap_bytes` | `1000000000` | 1 GB per-bundle cap |
| `per_artifact_limit_bytes` | `100000000` | 100 MB per-artifact limit |
| `task_retention_seconds` | `86400` | 24h task-scoped artifact retention |
| `bundle_retention_complete_seconds` | `604800` | 7 days for completed bundle artifacts |
| `bundle_retention_failed_seconds` | `2592000` | 30 days for failed bundle artifacts |

### `mcp`
| Key | Default | Description |
|-----|---------|-------------|
| `port` | `8080` | MCP HTTP server port. Override with `STUDIO_MCP_PORT` env var. |
| `bearer_token` | `""` | MCP auth bearer token. Override with `STUDIO_MCP_TOKEN` env var. |

The MCP server reads `STUDIO_DB_PATH` to find the orchestrator's SQLite database.

### MCP tools

The MCP server exposes these tools to Claude Desktop:

| Tool | Description |
|------|-------------|
| `list_pending_bundles` | List bundles not in a terminal state. Optional filter by tier, state, repo. |
| `get_bundle` | Get full details for a bundle by ID (includes workers, DAG nodes, edges). |
| `approve_bundle` | Approve a bundle. Requires explicit human confirmation. |
| `reject_bundle` | Reject a bundle with a reason. Requires explicit human confirmation. |
| `request_modification` | Request modifications to a bundle. Sends it back for revision. |
| `escalate_bundle` | Escalate a bundle to the next higher review tier. |
| `pause_bundle` | Pause a bundle that is currently in progress. |
| `resume_bundle` | Resume a paused bundle. |
| `kill_worker` | Kill a specific worker in a bundle. |
| `grant_capability` | Grant a capability request. [DEFERRED to Phase 3] |
| `revoke_capability` | Revoke a granted capability. [DEFERRED to Phase 3] |

Note: There is no `submit_bundle` MCP tool yet. New bundles must be created via the CLI (`studio submit`) or GitHub Issues.

### `ollama_cloud`
| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | `"https://ollama.com/api"` | Ollama Cloud API base URL |
| `health_check_interval_seconds` | `30` | Health check interval |
| `grace_window_minutes` | `5` | Grace period after health check failure |

### `secrets_config`
Array of `{"name": "...", "env_var": "...", "purpose": "..."}` entries. Purpose must be one of: `github_auth`, `llm_api`, `registry_auth`, `custom`.

## Bundle lifecycle

```
(none) ──1──► PROPOSED ──→ IN_REVIEW ──4──► APPROVED ──6──► IN_PROGRESS
                 │  ↑          │                    │
                 │  └──3───────┘                    ├──8──► PAUSED
                 │  (modify)                        │
                 └──1b──► REJECTED                  ├──9──► VERIFYING ──17──► COMPLETE
                                                    │        │
                                                    │        └──19──► FAILED
                                                    │
                                                    ├──25──► FAILED (execution)
                                                    └──7──► FAILED (timeout)
```

Approval matrix tier assignment (from complexity + risk scores):

| Complexity | Risk | Tier |
|------------|------|------|
| Low (≤3) | Low (≤2) | auto |
| Low (≤3) | Med (≤5) | auto_notify |
| Low-Med | Low-Med | summary |
| High (>6) | — | full_review |
| — | High (>5) | full_review |
| Irreversible | Any | full_review_cooldown |

## Capability manifest

Every bundle includes a capability manifest declaring its required grants:

- **filesystem**: read/write paths with recursive and create flags, working tree config
- **network**: egress destinations/ports/protocols, ingress, DNS
- **process**: allowed binaries, subtask spawning limits
- **secrets**: named secrets (env, file, or RPC delivery)
- **rpc**: allowed RPC methods and artifact access patterns
- **resources**: CPU, memory, disk, wall time, LLM token budgets

Capability enforcement uses `op_descriptor` format: `<category>.<operation>[:<resource>]` with algorithmic `is_subset()` checking across all 6 categories. DAG expansion nodes are validated as subsets of the bundle-level approved grant.

## Approval surfaces

All three surfaces write to the same state machine and are not differentially trusted:

- **CLI**: `studio approve/reject <id>` — lowest latency, always available
- **GitHub Issues**: `/approve`, `/reject <reason>`, `/modify <instructions>` comments — async, structured, permanent record
- **MCP**: Claude Desktop tool interface — human must click to confirm each action

## Operational runbook

### Starting the orchestrator

```bash
# Production (systemd)
systemctl start studio-orchestrator

# Manual (foreground)
uv run python -m studio.orchestrator.main
```

The orchestrator uses `sd_notify` (Type=notify) to signal readiness. systemd starts `studio-mcp` only after the orchestrator is accepting connections.

### Monitoring

```bash
# Quick status
studio status

# Detailed health (includes DB ok, state/tier breakdowns, recent errors)
studio health

# Watch bundle state
watch -n 5 'STUDIO_SOCKET_PATH=/run/studio/orchestrator.sock studio list'

# Check calibration drift
studio calibration-report
```

Key health indicators from `studio health`:
- `orchestrator_ok` / `db_ok`: should both be `true`
- `stalled_bundles > 0`: investigate immediately
- `recent_errors`: look for patterns in failure messages

### Responding to escalations

Escalated bundles are visible in `studio health` under the escalation breakdown. They follow a 5/10/21-day ladder:

1. **Day 0**: Bundler places bundle in review deck
2. **Day 5**: First escalation — PM notified via GitHub issue comment
3. **Day 10**: Second escalation — issue labeled `escalated`, PM pinged
4. **Day 21**: Final escalation — issue labeled `escalated:critical`

For each escalated bundle, the PM should:
1. Run `studio show <bundle-id>` to review the proposal
2. Run `studio audit <bundle-id>` to review capability grants
3. Decide: `/approve`, `/reject <reason>`, or `/modify <instructions>`

### Responding to stalled bundles

A stalled bundle is one in `IN_PROGRESS` with no heartbeat for > 8 hours.

1. Run `studio show <bundle-id>` to check node states
2. Run `studio show-worker <worker-id>` for each stuck worker
3. If the worker is unreachable, run `studio kill <bundle-id>` to terminate
4. If the worker is stuck in a phase, consider recalling or manually intervening

### Rotating secrets

```bash
studio rotate-secret llm_api
```

This:
1. Generates a new `token_hex(32)` value
2. Writes it to `memory/secrets/<name>.json`
3. Audit-logs the rotation with affected worker IDs
4. Workers that previously fetched the old value continue using it until they re-fetch (push notification via `worker.inject_context` is deferred)

After rotation, restart any running workers that depend on the rotated secret.

### Database backup

```bash
# The SQLite file is safe to copy while the orchestrator is running (WAL mode)
cp /var/lib/studio/state.db /backup/state-$(date -I).db
```

### Upgrading the orchestrator

1. Pull new code
2. Stop orchestrator: `systemctl stop studio-orchestrator`
3. The new code may include schema migrations — they run automatically on `connect()`
4. If the on-disk DB is ahead of the code version, `DatabaseVersionError` is raised and the process exits. Upgrade the code, not the DB.
5. Start orchestrator: `systemctl start studio-orchestrator`

## Requirements

- Python 3.12+
- SQLite 3.x
- bubblewrap (for production worker isolation)
- Linux (Unix domain sockets, bwrap, network namespaces)

## First-time setup

From `git clone` to a working hello-world bundle:

1. **Create required directories:**
   ```bash
   mkdir -p memory/secrets /run/studio /var/lib/studio
   chown studio:studio /run/studio /var/lib/studio memory/ memory/secrets/
   ```

2. **Configure `settings.json`:** The repo ships with sensible defaults. Three required keys to set before starting:
   - `orchestrator.socket_path` — path to the Unix socket (default `/run/studio/orchestrator.sock`)
   - `orchestrator.db_path` — path to the SQLite state database (default `/var/lib/studio/state.db`)
   - `orchestrator.memory_root` — path to `memory/` relative to the repo root

   If using GitHub Issues, also configure all `github.*` keys (`app_id`, `installation_id`, `private_key_path`, `webhook_secret`, `owner`, `repo`).

3. **Configure Ollama Cloud credentials:** The bundler, developer, and review workers read the `OLLAMA_API_KEY` environment variable. Set it in the orchestrator's environment or add an entry to `secrets_config` in `settings.json`:
   ```json
   "secrets_config": [
     {"name": "ollama_api_key", "env_var": "OLLAMA_API_KEY", "purpose": "llm_api"}
   ]
   ```

4. **Test in test mode first:**
   ```bash
   STUDIO_TEST_MODE=1 STUDIO_ORCH_DB_PATH=/tmp/studio.db \
     STUDIO_SOCKET_PATH=/tmp/studio.sock \
     uv run python -m studio.orchestrator.main &
   STUDIO_SOCKET_PATH=/tmp/studio.sock \
     uv run python -m studio.orchestrator.cli submit \
     studio/tests/fixtures/hello-world.json
   ```
   Test mode uses `NoopWorkerRunner` — no bubblewrap or real workers needed.

5. **Where things live:**
   - Unix socket: `/run/studio/orchestrator.sock` — CLI and workers connect here
   - SQLite DB: `/var/lib/studio/state.db` — all bundle/worker/artifact state, WAL mode
   - `memory/`: lives at the repo root; contains `secrets/`, `calibration/`, `notifications/`, `post-mortems/`

## License

Proprietary — all rights reserved.
