# AGENTS.md — Studio Orchestration System

## What this repo is

This is the Studio agent orchestration system: a Python async orchestrator that accepts bundle submissions, drives worker tasks through a DAG executor, enforces capability-based security, and writes outcomes to SQLite. Phase 2 (current) implements the bundler agent, full DAG executor, and artifact protocol. Pre-execution review tracks and approval matrix follow in later bundles.

## Layout

```
studio/
  orchestrator/    # kernel: DB, state machine, RPC, executor, runner, CLI, main
  workers/         # worker processes: developer worker, bundler agent
  tests/           # unit tests + fixtures + acceptance.sh
  systemd/         # studio-orchestrator.service (pending)
pyproject.toml     # Python 3.12, hatchling build
```

## Key invariants

- **Single SQLite writer.** The orchestrator process is the only writer. WAL mode enabled.
- **Capability check on every RPC.** Every method call is validated against the worker's manifest.
- **Kill-all on crash recovery.** No attempt to resume in-flight workers after orchestrator crash.
- **Event pump is the single mutator.** Executor state changes go through one async task (game-engine tick pattern).
- **All schema-mutating operations are in transactions.** Atomic multi-table writes.
- **Worker tokens are single-use, 256-bit random.** Passed via `STUDIO_WORKER_TOKEN` env var.

## Two submit paths

- **Kernel-direct** (Phase 1): submit with `task_dag` present → bundle goes PROPOSED → kernel approve → APPROVED → execution
- **Bundle-input-only** (Phase 2): submit with just `bundle_input` (no `task_dag`) → bundle goes PROPOSED → bundler worker spawns → bundler produces proposal + DAG → PROPOSED → IN_REVIEW

## Phase 2 insertion points

- Bundler agent: implemented in `studio/workers/bundler.py`, wires into idea-only submit → PROPOSED → IN_REVIEW
- Approval matrix: evaluator function, wires into IN_REVIEW → APPROVED (transition 4) — Bundle 2.4/2.5
- MCP server: separate process, connects over Unix socket at `/run/studio/orchestrator.sock`
- GitHub Issues: webhook receiver, writes to `approval_requests` table
- Mid-flight steering: Pause/Redirect/Abort/Rollback transitions in state machine
- Gates/aggregators: new node kinds in DAG executor (Bundle 2.1, implemented)
- Dynamic expansion: graft handler in executor (Bundle 2.1, implemented)
- Artifact protocol: `artifact.publish`/`artifact.fetch`/`artifact.list` RPC methods (Bundle 2.2, implemented)
- Network isolation: switch `kernel.network_isolation` from `"permissive"` to `"enforcing"`

## Build and test

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest studio/tests/ -v    # 480 tests
STUDIO_TEST_MODE=1 bash studio/tests/acceptance.sh  # 15 acceptance tests
```
