# Hermes Meta-Orchestrator Architecture (v2 — LangGraph DAG Executor)

## Layer Model

```
┌─────────────────────────────────────────────────────────┐
│  DAN (Signal messages, decisions, approval)              │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  HERMES — Meta-Orchestrator                             │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Intent → Decomposition → LangGraph StateGraph      │   │
│  │ "Build rate limiter" → StateGraph definition       │   │
│  │ Research the codebase first, then build the graph  │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Monitor: LangGraph checkpoint streams, node status │   │
│  │ "Node 3 paused — awaiting Dan's decision"          │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Human-in-the-loop relay                           │   │
│  │ interrupt() fires → Hermes asks Dan on Signal     │   │
│  │ Dan replies → Hermes resumes LangGraph            │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Cross-project coordination                        │   │
│  │ "Plot needs multi-tenant. Graph can build it."    │   │
│  └──────────────────────────────────────────────────┘   │
│  Deliver to: Signal (you), GitHub (PRs/issues)          │
└────────────────────┬────────────────────────────────────┘
                     │  Hermes calls langgraph CLI / Python API
┌────────────────────▼────────────────────────────────────┐
│  LANGGRAPH — DAG EXECUTION ENGINE                        │
│  ┌──────────────────────────────────────────────────┐   │
│  │ StateGraph — the workflow definition               │   │
│  │ Nodes: bundler, developer, qa, review, gate        │   │
│  │ Edges: conditional routing, fan-out, fan-in        │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Checkpointing — survive restarts, resume mid-DAG   │   │
│  │ Interrupt() — human-in-the-loop mid-execution      │   │
│  │ LangGraph Studio — visual DAG debugger in browser  │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Adapter Layer (thin)                               │   │
│  │ LangGraph node → calls Studio's runner.spawn()     │   │
│  │ LangGraph node → calls Studio's capability.check() │   │
│  │ LangGraph node → writes artifacts via Studio       │   │
│  └──────────────────────────────────────────────────┘   │
│  Replaces: Studio's executor.py (1606 lines)            │
└────────────────────┬────────────────────────────────────┘
                     │  Python import (same process)
┌────────────────────▼────────────────────────────────────┐
│  STUDIO ISOLATION LIBRARY (no changes)                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │bubblewrap│  │  Docker  │  │Firecrackr│              │
│  │ (local)  │  │ (univ.)  │  │ (prod)   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Capability Enforcement (capability.py)             │   │
│  │ filesystem grants, network egress, process allowlist │ │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ RPC Protocol (rpc.py) — worker ↔ orchestrator     │   │
│  │ Heartbeat, artifact publish/fetch, token auth     │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Approval Matrix (approval.py) — kept as library   │   │
│  │ Complexity + risk scoring → tier assignment       │   │
│  └──────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │  Token auth / RPC / Unix socket
┌────────────────────▼────────────────────────────────────┐
│  WORKER AGENTS                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ OpenCode │  │ClaudeCode│  │  Codex   │              │
│  │ (primary)│  │ (heavy)  │  │ (OpenAI) │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Review Workers: adversarial, security, QA          │   │
│  │ Verification: pytest, lint, build, acceptance      │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## What LangGraph Replaces

Studio's `executor.py` (1,606 lines) handles: node lifecycle (pending → ready → running → completed/failed), edge condition evaluation, gate predicates, aggregator reduce functions, dynamic DAG expansion, heartbeat monitoring, artifact publishing.

LangGraph replaces all of this with:

| Studio Executor | LangGraph Equivalent |
|---|---|
| Node lifecycle | `StateGraph` node definitions + `Command` routing |
| Edge conditions | `add_conditional_edges()` |
| Gate predicates | `interrupt()` + human approval node |
| Aggregator joins | `Send()` fan-out + `add_edge()` converge |
| Dynamic expansion | `Command(goto=...)` dynamic routing |
| Heartbeat monitoring | Built-in checkpointing + timeout config |
| Crash recovery (kill-all) | Checkpointer (survives restarts) |
| Artifact publishing | Node output → State update (or Studio's artifact store) |

## Why LangGraph Wins Here

1. **Checkpointing** — Studio's executor has kill-all-on-crash. LangGraph checkpoints after every node transition. Server dies mid-DAG? Restart, graph resumes from exactly where it left off. No lost work.

2. **Human-in-the-loop** — LangGraph's `interrupt()` pauses execution, waits for external input, then resumes. Studio would need to build this from scratch. Hermes already has Signal — the relay is trivial: `interrupt()` → Signal message to Dan → Dan replies → Hermes injects response → graph resumes.

3. **Visual debugging** — LangGraph Studio shows every node state, every transition, every artifact, in a browser. Studio has `studio show` with a Mermaid diagram. Night and day difference for debugging complex workflows.

4. **Maintained by LangChain** — 60+ contributors, battle-tested across thousands of production deployments. Studio's executor is code Dan maintains himself.

5. **Complex routing native** — conditional branching, fan-out/fan-in, dynamic `Send()`, cycle detection, max steps enforcement. Studio's executor can do some of this (it has gate nodes and aggregators) but LangGraph was purpose-built for it.

## What Studio Keeps (Irreplaceable)

- **Isolation layer** — bubblewrap, Docker, Firecracker runners. Zero alternatives that give you hardware-level sandboxing with capability enforcement.
- **Capability system** — `capability.py`'s manifest parsing, `is_subset()` enforcement, process allowlists. This is the security boundary.
- **RPC protocol** — worker ↔ orchestrator communication, heartbeat pump, token auth, artifact transport. Clean, tested, works.
- **Approval matrix** — `approval.py`'s tier evaluation. Can be called from within a LangGraph node to gate human approval steps.

## Implementation Plan

### Phase 1: Extract Isolation Library (unchanged from v1 plan)
- Extract `runner.py`, `capability.py`, `rpc.py`, `artifact.py`, `approval.py` into a standalone Python package
- Test: spawn a bubblewrap worker via the library alone (no executor)

### Phase 2: Build LangGraph Adapter
- Define the canonical StateGraph template:
  ```
  bundler → [review: adversary | security | QA] → gate(approval) → developer → QA → complete
  ```
- Each node is a thin wrapper that calls Studio's `runner.spawn()` with the right worker type
- Gate node uses `interrupt()` for human approval
- Checkpointer: SQLite (matches Studio's existing DB, zero new infra)
- Test: run a graph end-to-end with a trivial bundle

### Phase 3: Wire Hermes as Meta-Orchestrator
- Hermes receives intent → decomposes → constructs StateGraph definition
- Hermes launches graph, monitors via checkpoint stream
- Human-in-the-loop: Hermes polls for interrupts, relays to Dan on Signal, injects response
- Delivery: Hermes reports PR URL when graph completes

### Phase 4: Fire Test Against Boundary
- Submit a real bundle targeting `learhy/boundary`
- Graph runs: bundler plans → review → Dan approves → developer codes → QA verifies → PR
- Fix any issues, iterate until it passes cleanly

## What Gets Deleted

After LangGraph integration is stable:
- `executor.py` — replaced entirely
- `scheduler.py` — LangGraph handles scheduling via checkpointer
- `reconciler.py` — kill-all isn't needed with checkpointing
- `reducers.py` — LangGraph nodes handle their own aggregation
- `expression.py` — LangGraph's conditional edges replace gate predicates
- `visualizer.py` — LangGraph Studio replaces Mermaid diagrams

Lines deleted: ~3,000. Lines maintained: zero (LangGraph team handles it).
