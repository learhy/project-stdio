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

## Prerequisites

- **Linux** — the orchestrator uses Unix domain sockets, bubblewrap, and network namespaces. Test mode works on any Linux distribution; production needs kernel 5.x or later.
- **Python 3.12+** — verify with `python3 --version`. Install via `apt install python3.12` (Debian/Ubuntu), `dnf install python3.12` (Fedora), or [pyenv](https://github.com/pyenv/pyenv).
- **SQLite 3.x** — bundled with Python 3.12. Verify: `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
- **bubblewrap** — required for production worker isolation. Install with `apt install bubblewrap` (Debian/Ubuntu) or `dnf install bubblewrap` (Fedora). Verify with `bwrap --version`. Test mode does not need bubblewrap.
- **uv** — Python package manager used throughout. Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## Quick start

A single walkthrough from clone to working system using test mode (no bubblewrap, no real subprocesses).

### 1. Clone and install

```bash
git clone https://github.com/learhy/project-stdio.git
cd project-stdio

# Create virtual environment and install
uv venv
uv pip install -e ".[dev]"

# The studio CLI is now available via uv run:
#   uv run studio <command>
```

### 2. Start the orchestrator in test mode

Test mode uses `NoopWorkerRunner` — workers are simulated, no bubblewrap isolation, no real subprocesses.

```bash
STUDIO_TEST_MODE=1 \
  STUDIO_ORCH_DB_PATH=/tmp/studio.db \
  STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run studio-orchestrator &
```

### 3. Verify it's working

```bash
# CLI health dashboard
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio health
# Expected: Orchestrator: OK | DB: OK | Uptime: ...

# HTTP health endpoint (for monitoring tools)
curl http://localhost:7810/health
# Expected: {"status":"ok"}
```

### 4. Submit a hello-world bundle

```bash
# Kernel-direct path (pre-built DAG)
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run studio submit studio/tests/fixtures/hello-world.json
# Prints: Bundle submitted: <bundle-id>
```

You can also submit an idea and let the bundler worker build the DAG:

```bash
echo '{"bundle_input": {"idea": "Add a logout button to the settings page"}}' > /tmp/idea.json
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio submit /tmp/idea.json
```

### 5. Walk the bundle lifecycle

```bash
BUNDLE_ID="<your-bundle-id>"

# Inspect the proposal
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio show "$BUNDLE_ID"

# Approve it — this starts execution
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio approve "$BUNDLE_ID"

# Check the result (test mode completes near-instantly)
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio show "$BUNDLE_ID"
```

### 6. Run the test suite

```bash
uv run python -m pytest studio/tests/ -v
```

### 7. Optional: acceptance tests and MCP server

```bash
# End-to-end acceptance test
STUDIO_TEST_MODE=1 bash studio/tests/acceptance.sh

# Start the MCP server (pointed at the same database)
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  STUDIO_DB_PATH=/tmp/studio.db \
  STUDIO_MCP_PORT=8080 \
  STUDIO_MCP_TOKEN=test-token-123 \
  uv run studio-mcp &
```

## Production deployment

Running without `STUDIO_TEST_MODE` switches to full production mode: the orchestrator spawns real worker subprocesses under bubblewrap isolation with per-worker egress proxies.

### What changes from test mode

| Aspect | Test mode (`STUDIO_TEST_MODE=1`) | Production |
|--------|----------------------------------|------------|
| Worker runner | `NoopWorkerRunner` — simulated, instant completion | `LocalBwrapWorkerRunner` — real bubblewrap containers |
| Bubblewrap | Not needed | Required (`bwrap` must be on PATH) |
| Egress proxy | None | Per-worker Unix socket proxy enforcing network grants |
| Git worktrees | Not created | Real `git worktree add` per worker node |
| Database path | `/tmp/studio.db` | `/var/lib/studio/state.db` |
| Socket path | `/tmp/studio.sock` | `/run/studio/orchestrator.sock` |

### System dependencies

```bash
# Install bubblewrap
apt install bubblewrap

# Create the studio system user
useradd -r -s /sbin/nologin studio

# Verify bwrap is available
bwrap --version
```

### Directory setup

```bash
mkdir -p /run/studio /var/lib/studio memory/secrets
chown studio:studio /run/studio /var/lib/studio memory/ memory/secrets/
```

The orchestrator process needs write access to:
- `/run/studio/` — Unix domain socket (created at startup)
- `/var/lib/studio/` — SQLite state database (WAL mode)
- `memory/secrets/` — secret storage (at the repo root)

### Configuration

The orchestrator uses built-in defaults from the model classes and does not read `settings.json` at runtime. The shipped `settings.json` is a reference file (used by the MCP server, which does load it).

Override key paths via environment variables:

```bash
export STUDIO_ORCH_DB_PATH=/var/lib/studio/state.db
export STUDIO_SOCKET_PATH=/run/studio/orchestrator.sock
export OLLAMA_API_KEY="your-ollama-api-key"
```

`OLLAMA_API_KEY` is required for any LLM-dependent worker (bundler, developer, review, QA). Without it, worker subprocesses that call the Ollama Cloud API will fail.

### Running with systemd

The repo ships a unit file at `studio/systemd/studio-orchestrator.service`:

```bash
cp studio/systemd/studio-orchestrator.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now studio-orchestrator
```

Check status and logs:

```bash
systemctl status studio-orchestrator
journalctl -u studio-orchestrator -f
```

The unit file uses `Type=simple` with `Restart=on-failure`. It sets `WorkingDirectory=/var/lib/studio` and provides `STUDIO_ORCH_DB_PATH` and `STUDIO_ORCH_SOCKET_PATH` environment variables. Logs go to journald via `StandardOutput=journal` + `StandardError=journal`.

### Manual foreground run

```bash
STUDIO_ORCH_DB_PATH=/var/lib/studio/state.db \
  STUDIO_SOCKET_PATH=/run/studio/orchestrator.sock \
  OLLAMA_API_KEY="your-key" \
  uv run studio-orchestrator
```

Logs are written to stderr via Python's `logging.basicConfig`.

### Health check

The HTTP listener on port `7810` exposes a health endpoint suitable for external monitoring:

```bash
curl http://localhost:7810/health
# {"status":"ok"}
```

Use this with your monitoring stack — Nagios, Prometheus blackbox exporter, AWS target group health checks, etc. The port is configurable via `orchestrator.http_port` in the model defaults (change by setting the env var if needed).

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

## Environment variables

These override settings at runtime. The orchestrator reads them in `main()`; the CLI and MCP server read a subset.

| Variable | Overrides | Default (when unset) |
|----------|-----------|---------------------|
| `STUDIO_TEST_MODE` | Worker runner selection | (empty — production mode) |
| `STUDIO_ORCH_DB_PATH` | `orchestrator.db_path` | `/var/lib/studio/state.db` |
| `STUDIO_SOCKET_PATH` | `orchestrator.socket_path` | `/run/studio/orchestrator.sock` |
| `STUDIO_ORCH_SOCKET_PATH` | `orchestrator.socket_path` (fallback) | (none) |
| `STUDIO_DB_PATH` | MCP server DB read path | (must match `STUDIO_ORCH_DB_PATH`) |
| `STUDIO_MCP_PORT` | `mcp.port` | `8080` |
| `STUDIO_MCP_TOKEN` | `mcp.bearer_token` | (empty — no auth) |
| `OLLAMA_API_KEY` | LLM API key for worker subprocesses | (none — required for production) |

Socket path resolution order: `STUDIO_SOCKET_PATH` → `STUDIO_ORCH_SOCKET_PATH` (backward compat) → `settings.json` value → model default.

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

## Getting Started with GitHub

Studio integrates with GitHub Issues as an approval surface, allowing PMs and reviewers to approve, reject, or request modifications to bundles directly from issue comments. This section covers setting up the integration end-to-end.

### 1. Create a GitHub App

The orchestrator authenticates to GitHub as a GitHub App using JWT-based auth.

1. Go to **Settings > Developer settings > GitHub Apps** in your organization or user account.
2. Click **New GitHub App** and configure:
   - **Name**: `studio-orchestrator` (or your preferred name)
   - **Homepage URL**: your repo URL
   - **Webhook URL**: `https://<your-domain>/github/webhook` (the orchestrator's HTTP server handles this)
   - **Webhook secret**: generate a random string (e.g., `openssl rand -hex 32`)
3. Under **Permissions**, set:
   - **Issues**: Read & Write
   - **Metadata**: Read-only (mandatory)
4. Under **Subscribe to events**, check **Issues** and **Issue comments**.
5. Click **Create GitHub App**.
6. After creation, generate a **private key** and download the `.pem` file.
7. Note the **App ID** from the app's settings page.
8. Go to **Install App** and install it on your target repo or organization. Note the **Installation ID** from the installation URL (`.../installations/<id>`).

### 2. Configure Studio

Add your GitHub App credentials to `settings.json`:

```json
{
  "github": {
    "enabled": true,
    "app_id": "123456",
    "installation_id": "987654321",
    "private_key_path": "/etc/studio/github-app.pem",
    "webhook_secret": "your-webhook-secret",
    "owner": "your-org",
    "repo": "your-repo"
  }
}
```

Or use environment variables:

```bash
export STUDIO_GITHUB_ENABLED=true
export STUDIO_GITHUB_APP_ID=123456
export STUDIO_GITHUB_INSTALLATION_ID=987654321
export STUDIO_GITHUB_PRIVATE_KEY_PATH=/etc/studio/github-app.pem
export STUDIO_GITHUB_WEBHOOK_SECRET=your-webhook-secret
export STUDIO_GITHUB_OWNER=your-org
export STUDIO_GITHUB_REPO=your-repo
```

### 3. Expose the webhook endpoint

The orchestrator runs an HTTP server on `orchestrator.http_port` (default `7810`) that exposes:

- `GET /health` — liveness check
- `POST /github/webhook` — HMAC-SHA256 validated webhook receiver

In production, place a reverse proxy (nginx, Caddy) in front of the orchestrator to handle TLS termination. Example nginx config:

```nginx
location /github/webhook {
    proxy_pass http://127.0.0.1:7810;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

### 4. Create issues to submit bundles

Create a GitHub issue describing the change you want. The orchestrator polls for new issues (every `poll_interval_seconds`, default 60s), converts them into bundle submissions, and posts a comment with the bundle ID and review deck when ready.

Issue title and body become the bundle's idea description. The orchestrator spawns a bundler worker that produces a proposal + DAG, then the bundle enters `IN_REVIEW`.

### 5. Approve, reject, or modify from comments

Once a bundle is in review, post issue comments with slash commands:

| Comment | Effect |
|---------|--------|
| `/approve` | Approve the bundle and start execution |
| `/reject <reason>` | Reject the bundle with an explanation |
| `/modify <instructions>` | Send the bundle back for revision with specific guidance |

The orchestrator detects these commands via issue comment polling and transitions the bundle through the state machine. All actions are recorded as issue comments for a permanent audit trail.

Example workflow:

```
PM opens issue: "Add rate limiting to the API gateway"
  → orchestrator detects issue, creates bundle, posts review deck comment

Reviewer comments: "/approve"
  → bundle transitions APPROVED → IN_PROGRESS
  → worker executes, posts progress updates as issue comments

QA worker completes verification:
  → final outcome posted as issue comment, issue auto-closed
```

### 6. Verify the integration

Use the CLI to confirm GitHub connectivity and issue ingestion:

```bash
# Run in test mode first with GitHub polling enabled
STUDIO_TEST_MODE=1 \
  STUDIO_ORCH_DB_PATH=/tmp/studio.db \
  STUDIO_SOCKET_PATH=/tmp/studio.sock \
  uv run python -m studio.orchestrator.main &

# Check health — github_enabled should show true
STUDIO_SOCKET_PATH=/tmp/studio.sock uv run studio health
```

## Operational runbook

### Starting the orchestrator

See [Production deployment](#production-deployment) above for the full setup. Quick reference:

```bash
# Production (systemd)
systemctl start studio-orchestrator

# Manual (foreground)
STUDIO_ORCH_DB_PATH=/var/lib/studio/state.db \
  STUDIO_SOCKET_PATH=/run/studio/orchestrator.sock \
  uv run studio-orchestrator
```

Logs: `journalctl -u studio-orchestrator -f` (systemd) or stderr (foreground).

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

## License

Proprietary — all rights reserved.
