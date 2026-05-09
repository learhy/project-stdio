# Studio — Agent Orchestration System

**Phase 1 Kernel**: Python async orchestrator that accepts bundle submissions, drives linear DAGs of worker tasks under bubblewrap isolation, enforces capability-based security, and writes outcomes to SQLite.

## Overview

Studio is an agent orchestration system where a central orchestrator process manages the lifecycle of "bundles" — structured task DAGs executed by isolated worker subprocesses. Workers communicate with the orchestrator over a Unix domain socket via JSON-RPC 2.0, sending heartbeats, logs, progress reports, and final outcomes.

### Architecture

```
CLI (studio submit/approve/reject/...)
  │
  ▼
Orchestrator (single process)
  ├── SQLite (WAL mode) — single writer
  ├── BundleStateMachine — 8 transitions, 12 states
  ├── RpcDispatcher — 14 worker RPC methods
  ├── ConnectionManager — Unix socket, token auth
  ├── LinearDagExecutor — FIFO dispatch, concurrency cap
  ├── LocalBwrapWorkerRunner — bubblewrap isolation
  ├── Scheduler — periodic dispatch + heartbeat checks
  └── Reconciler — kill-all crash recovery
  │
  ▼
Worker subprocesses (bubblewrap containers)
  └── Developer worker — invokes coding agent, reports results
```

### Quick start

```bash
# Install
.venv/bin/pip install -e ".[dev]"

# Run tests
.venv/bin/python -m pytest studio/tests/ -v

# Start orchestrator (test mode — no bwrap needed)
STUDIO_TEST_MODE=1 STUDIO_ORCH_DB_PATH=/tmp/studio.db \
  STUDIO_ORCH_SOCKET_PATH=/tmp/studio.sock \
  .venv/bin/python -m studio.orchestrator.main &

# Submit a bundle
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  .venv/bin/python -m studio.orchestrator.cli submit \
  studio/tests/fixtures/hello-world.json

# Approve it
STUDIO_SOCKET_PATH=/tmp/studio.sock \
  .venv/bin/python -m studio.orchestrator.cli approve <bundle-id>

# Run acceptance tests
STUDIO_TEST_MODE=1 bash studio/tests/acceptance.sh
```

## CLI commands

| Command | Description |
|---------|-------------|
| `studio submit <file>` | Submit a bundle JSON file |
| `studio approve <id>` | Approve and start execution |
| `studio reject <id> [-r reason]` | Reject a proposed bundle |
| `studio list [--state s] [--json]` | List non-terminal bundles |
| `studio show <id>` | Show bundle detail and node states |
| `studio show-worker <id>` | Show worker detail and recent logs |
| `studio kill <id>` | Kill a running bundle's workers |
| `studio status` | Show orchestrator health and active bundles |

## Bundle lifecycle (Phase 1)

```
(none) ──1──► PROPOSED ──1a──► APPROVED ──6──► IN_PROGRESS
                  │                                │
                  └──1b──► REJECTED                ├──9──► VERIFYING ──17──► COMPLETE
                                                   │        │
                                                   │        └──19──► FAILED
                                                   │
                                                   └──25──► FAILED
```

## Capability manifest

Every bundle includes a capability manifest declaring its required grants:

- **filesystem**: read/write paths with recursive and create flags
- **network**: egress destinations and ports, ingress, DNS
- **process**: allowed binaries, subtask spawning limits
- **secrets**: named secrets (env, file, or RPC delivery)
- **rpc**: allowed RPC methods and artifact access patterns
- **resources**: CPU, memory, disk, wall time, LLM token budgets

Capability enforcement uses `op_descriptor` format: `<category>.<operation>[:<resource>]` with algorithmic `is_subset()` checking.

## Requirements

- Python 3.12+
- SQLite 3.x
- bubblewrap (for production worker isolation)
- Linux (Unix domain sockets, bwrap)

## License

Proprietary — all rights reserved.
