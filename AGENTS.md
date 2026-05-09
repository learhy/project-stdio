# AGENTS.md — Studio Orchestration System

## What this repo is

This is the Studio agent orchestration system: a Python async orchestrator that accepts bundle submissions, drives worker tasks through a DAG executor, enforces capability-based security, and writes outcomes to SQLite. Phase 1 (current) implements the kernel: linear DAGs, CLI-only surface, no bundler/MCP/GitHub Issues.

## Layout

```
studio/
  orchestrator/    # kernel: DB, state machine, RPC, executor, runner, CLI
  workers/         # worker processes: bootstrap + developer stub
  tests/           # unit tests + fixtures
  scripts/         # acceptance.sh
  systemd/         # studio-orchestrator.service
settings.json      # default configuration
pyproject.toml     # uv-managed, Python 3.12
```

## Key invariants

- **Single SQLite writer.** The orchestrator process is the only writer. WAL mode enabled.
- **Capability check on every RPC.** Every method call is validated against the worker's manifest.
- **Kill-all on crash recovery.** No attempt to resume in-flight workers after orchestrator crash.
- **Event pump is the single mutator.** Executor state changes go through one async task (game-engine tick pattern).
- **All schema-mutating operations are in transactions.** Atomic multi-table writes.
- **Worker tokens are single-use, 256-bit random.** Passed via `STUDIO_WORKER_TOKEN` env var.

## Phase 2 insertion points

- Bundler agent: new agent type, wires into `PROPOSED → IN_REVIEW` (transition 2)
- Approval matrix: evaluator function, wires into `IN_REVIEW → APPROVED` (transition 4)
- MCP server: separate process, connects over Unix socket at `/run/studio/orchestrator.sock`
- GitHub Issues: webhook receiver, writes to `approval_requests` table
- Mid-flight steering: Pause/Redirect/Abort/Rollback transitions in state machine
- Gates/aggregators: new node kinds in DAG executor
- Dynamic expansion: graft handler in executor
- Artifact protocol: `artifact.publish`/`artifact.fetch`/`artifact.list` RPC methods
- Network isolation: switch `kernel.network_isolation` from `"permissive"` to `"enforcing"`

## Build and test

```bash
uv pip install -e ".[dev]"
pytest studio/tests/ -v
bash studio/scripts/acceptance.sh
```
