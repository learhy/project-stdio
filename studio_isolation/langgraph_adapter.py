"""LangGraph adapter for Studio's isolation layer.

This module defines the canonical Studio StateGraph that replaces
executor.py, scheduler.py, reconciler.py, reducers.py, and expression.py.

Architecture (sequential — parallel fan-out via Send() in Phase 3):
    bundler → review_adversary → review_security → review_qa
           → approval_gate (interrupt for human-in-the-loop)
           → developer → qa_verification → complete

Each node wraps a Studio runner from studio_isolation. The graph uses
SQLite checkpointing to survive restarts.

Usage:
    from studio_isolation.langgraph_adapter import build_studio_graph, StudioGraphRunner

    runner = await StudioGraphRunner.create(db_path="/tmp/checkpoints.db")
    state = await runner.run("build a rate limiter", bundle_id="bundle-abc123")

    # For human-in-the-loop:
    async for event in runner.graph.astream_events(state, runner.config):
        if event["event"] == "on_interrupt":
            ...  # relay to human, then resume
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


# ── State ──────────────────────────────────────────────────────────────────────

from typing import TypedDict


class StudioGraphState(TypedDict, total=False):
    """The canonical state for the Studio LangGraph.

    This is the shared state object that flows through every node.
    Each node reads from and writes to this state.
    """

    # Input
    bundle_input: str
    bundle_id: str

    # Bundler output
    proposal: dict[str, Any]
    task_dag: dict[str, Any]

    # Review outputs
    review_findings: list[dict[str, Any]]
    approval_tier: str
    auto_ship: bool

    # Approval gate
    approved: bool
    approval_decision: str
    approval_reason: str

    # Developer output
    worktree_path: str
    branch_name: str
    changed_files: list[str]
    test_results: dict[str, Any]

    # QA output
    qa_passed: bool
    qa_report: dict[str, Any]

    # Delivery
    pr_url: str
    commit_sha: str
    error: str


# ── Node implementations ────────────────────────────────────────────────────────


async def node_bundler(state: StudioGraphState) -> dict[str, Any]:
    """Bundler node: decomposes intent into a proposal + task DAG.

    In production, this spawns a bundler worker via the Studio isolation layer.
    For now, it creates a placeholder proposal.
    """
    logger.info(f"[bundler] Processing bundle_input: {state.get('bundle_input', '')[:100]}")
    return {
        "proposal": {
            "title": state.get("bundle_input", ""),
            "summary": "Decomposition placeholder — Hermes meta-orchestrator fills this",
            "complexity_score": 2,
            "risk_score": 1,
        },
        "task_dag": {
            "nodes": [
                {"id": "research", "kind": "worker"},
                {"id": "implement", "kind": "worker"},
                {"id": "test", "kind": "worker"},
            ]
        },
        "review_findings": [],
    }


async def node_review_adversary(state: StudioGraphState) -> dict[str, Any]:
    """Adversarial review: challenge assumptions in the proposal."""
    logger.info("[review:adversary] Reviewing proposal")
    findings: list[dict[str, Any]] = list(state.get("review_findings", []))
    findings.append({
        "role": "adversary",
        "finding": "Proposal assumptions are reasonable (placeholder)",
        "severity": "info",
    })
    return {"review_findings": findings}


async def node_review_security(state: StudioGraphState) -> dict[str, Any]:
    """Security review: check for vulnerabilities, sensitive paths."""
    logger.info("[review:security] Reviewing security posture")
    findings: list[dict[str, Any]] = list(state.get("review_findings", []))
    findings.append({
        "role": "security",
        "finding": "No security concerns detected (placeholder)",
        "severity": "info",
    })
    return {"review_findings": findings}


async def node_review_qa(state: StudioGraphState) -> dict[str, Any]:
    """QA review: check completeness, test coverage, acceptance criteria."""
    logger.info("[review:qa] Reviewing quality")
    findings: list[dict[str, Any]] = list(state.get("review_findings", []))
    findings.append({
        "role": "qa",
        "finding": "QA plan looks adequate (placeholder)",
        "severity": "info",
    })
    return {"review_findings": findings}


async def node_approval_gate(state: StudioGraphState) -> dict[str, Any]:
    """Approval gate: human-in-the-loop via LangGraph interrupt().

    This node pauses execution and waits for human input.
    When Hermes detects the interrupt, it relays the question
    to Dan on Signal. Dan's response is injected via Command(resume=...).
    """
    logger.info(f"[approval_gate] Requesting human approval")

    # Evaluate approval tier from review findings
    from studio_isolation.approval import evaluate_approval_matrix
    from studio_isolation.models import BundleProposal

    try:
        # Format findings as dict keyed by role name for the approval evaluator
        findings_dict: dict[str, list[dict]] = {}
        for f in state.get("review_findings", []):
            role = f.get("role", "unknown")
            findings_dict.setdefault(role, []).append(f)

        proposal_dict = state.get("proposal", {})
        proposal = BundleProposal(
            complexity_score=proposal_dict.get("complexity_score", 1),
            risk_score=proposal_dict.get("risk_score", 1),
            target=proposal_dict.get("target", ""),
            requirements_summary=proposal_dict.get("summary", proposal_dict.get("title", "")),
        )
        decision = evaluate_approval_matrix(
            proposal=proposal,
            findings=findings_dict,
            triggers=[],  # No mandatory-review triggers in standalone mode
        )
        tier = decision.tier.value
        auto_ship = decision.auto_ship
    except Exception as e:
        logger.warning(f"Approval evaluation fell back to default tier: {e}")
        tier = "full_review"
        auto_ship = False

    if auto_ship:
        return {
            "approval_tier": tier,
            "auto_ship": True,
            "approved": True,
            "approval_decision": "approved",
            "approval_reason": "auto-ship based on approval matrix",
        }

    # Human-in-the-loop: pause for approval
    approval = interrupt({
        "question": f"Approve bundle {state.get('bundle_id', 'unknown')}?",
        "proposal": state.get("proposal", {}),
        "tier": tier,
    })

    return {
        "approval_tier": tier,
        "auto_ship": False,
        "approved": approval.get("approved", False),
        "approval_decision": approval.get("decision", "rejected"),
        "approval_reason": approval.get("reason", ""),
        "error": None if approval.get("approved", False)
                 else f"Rejected: {approval.get('reason', 'no reason given')}",
    }


async def node_developer(state: StudioGraphState) -> dict[str, Any]:
    """Developer node: implements the task using a Studio worker.

    In production, this spawns a developer worker via studio_isolation.
    """
    logger.info(f"[developer] Implementing task")
    return {
        "changed_files": [],
        "branch_name": f"studio/{state.get('bundle_id', 'unknown')}",
        "worktree_path": f"/tmp/studio-{state.get('bundle_id', 'unknown')}",
    }


async def node_qa_verification(state: StudioGraphState) -> dict[str, Any]:
    """QA verification node: runs tests and reports results.

    In production, this spawns a QA worker via studio_isolation.
    """
    logger.info(f"[qa] Running verification")
    return {
        "qa_passed": True,
        "qa_report": {
            "tests_run": 0,
            "tests_passed": 0,
            "summary": "QA verification placeholder — Hermes meta-orchestrator fills this",
        },
    }


async def node_complete(state: StudioGraphState) -> dict[str, Any]:
    """Completion node: marks the bundle as complete."""
    logger.info(f"[complete] Bundle {state.get('bundle_id')} complete")
    return {}


# ── Conditional routing ────────────────────────────────────────────────────────


def route_after_approval(state: StudioGraphState) -> str:
    """After approval gate: route to developer or end."""
    if not state.get("approved", False):
        return "complete"  # Rejected: go straight to end
    return "developer"


def route_after_qa(state: StudioGraphState) -> str:
    """After QA: route to complete."""
    # Always go to complete for now; retry logic in later phases
    return "complete"


# ── StudioGraphRunner — manages checkpointer lifecycle ────────────────────────


class StudioGraphRunner:
    """Manages a compiled Studio graph with an SQLite checkpointer.

    The aiosqlite.Connection is owned by this object and must be closed
    when no longer needed. The AsyncSqliteSaver checkpointer wraps that
    shared connection, keeping it alive for multiple invocations.

    Usage:
        runner = await StudioGraphRunner.create()
        state = await runner.run("build a rate limiter")
        await runner.close()
    """

    def __init__(self, conn: aiosqlite.Connection, checkpointer: AsyncSqliteSaver, graph: StateGraph) -> None:
        self._conn = conn
        self.checkpointer = checkpointer
        self.graph = graph

    @classmethod
    async def create(cls, db_path: str = ":memory:") -> "StudioGraphRunner":
        """Create a new StudioGraphRunner with checkpointer.

        Args:
            db_path: SQLite path for checkpoints. ":memory:" (ephemeral)
                     or a file path for durability.
        """
        conn = await aiosqlite.connect(db_path)
        checkpointer = AsyncSqliteSaver(conn)
        await checkpointer.setup()

        graph = _make_graph()
        compiled = graph.compile(checkpointer=checkpointer)
        logger.info("StudioGraphRunner created with checkpointer")
        return cls(conn=conn, checkpointer=checkpointer, graph=compiled)

    async def run(
        self,
        bundle_input: str,
        bundle_id: str = "bundle-001",
        auto_ship: bool = True,
    ) -> StudioGraphState:
        """Run the graph to completion with no human-in-the-loop pause.

        Args:
            bundle_input: The user's intent / task description.
            bundle_id: Unique thread ID for checkpointing.
            auto_ship: If True, the approval gate auto-approves.
        """
        config = {"configurable": {"thread_id": bundle_id}}
        initial_state: StudioGraphState = {
            "bundle_input": bundle_input,
            "bundle_id": bundle_id,
            "auto_ship": auto_ship,
        }
        return await self.graph.ainvoke(initial_state, config)

    def config_for(self, bundle_id: str) -> dict:
        """Build a LangGraph config dict for a bundle."""
        return {"configurable": {"thread_id": bundle_id}}

    def get_mermaid(self) -> str:
        """Return a Mermaid diagram of the graph."""
        return self.graph.get_graph().draw_mermaid()

    async def close(self) -> None:
        """Close the aiosqlite connection."""
        await self._conn.close()
        logger.info("StudioGraphRunner connection closed")


# ── Internal graph construction ────────────────────────────────────────────────


def _make_graph() -> StateGraph:
    """Build (but do not compile) the Studio StateGraph.

    Graph topology (sequential, parallel fan-out via Send() in Phase 3):
        bundler → review_adversary → review_security → review_qa
               → approval_gate → developer → qa_verification → complete
    """
    builder = StateGraph(StudioGraphState)

    builder.add_node("bundler", node_bundler)
    builder.add_node("review_adversary", node_review_adversary)
    builder.add_node("review_security", node_review_security)
    builder.add_node("review_qa", node_review_qa)
    builder.add_node("approval_gate", node_approval_gate)
    builder.add_node("developer", node_developer)
    builder.add_node("qa_verification", node_qa_verification)
    builder.add_node("complete", node_complete)

    builder.set_entry_point("bundler")

    # Sequential edges
    builder.add_edge("bundler", "review_adversary")
    builder.add_edge("review_adversary", "review_security")
    builder.add_edge("review_security", "review_qa")
    builder.add_edge("review_qa", "approval_gate")

    # Conditional: approval → developer or complete
    builder.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {"developer": "developer", "complete": "complete"},
    )

    # Developer → QA → complete
    builder.add_edge("developer", "qa_verification")
    builder.add_conditional_edges(
        "qa_verification",
        route_after_qa,
        {"complete": "complete"},
    )

    builder.add_edge("complete", END)
    return builder


# ── Compatibility wrappers (for existing code) ─────────────────────────────────


async def build_studio_graph(
    db_path: str = ":memory:",
) -> tuple[StateGraph, AsyncSqliteSaver]:
    """Build and compile the Studio graph. DEPRECATED: prefer StudioGraphRunner.

    Returns a tuple of (compiled_graph, checkpointer).
    """
    runner = await StudioGraphRunner.create(db_path=db_path)
    return runner.graph, runner.checkpointer


async def run_studio_graph(
    bundle_input: str,
    bundle_id: str = "test-001",
    db_path: str = ":memory:",
    auto_ship: bool = True,
) -> StudioGraphState:
    """Run the Studio graph. DEPRECATED: prefer StudioGraphRunner.run()."""
    runner = await StudioGraphRunner.create(db_path=db_path)
    try:
        return await runner.run(
            bundle_input=bundle_input,
            bundle_id=bundle_id,
            auto_ship=auto_ship,
        )
    finally:
        await runner.close()


async def get_graph_mermaid() -> str:
    """Return a Mermaid diagram of the graph."""
    runner = await StudioGraphRunner.create()
    try:
        return runner.get_mermaid()
    finally:
        await runner.close()
