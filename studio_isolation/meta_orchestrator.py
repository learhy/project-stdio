"""Hermes Meta-Orchestrator for Studio's LangGraph pipeline.

This module implements the Hermes-level orchestration layer that:
1. Receives intent (from Dan on Signal, CLI, or cron)
2. Decomposes intent into a LangGraph-compatible StateGraph initial state
3. Launches and monitors the graph via StudioGraphRunner
4. Relays human-in-the-loop interrupts (LangGraph interrupt()) to Signal
5. Reports results (PR URL, commit SHA) when the graph completes

Architecture:
    Dan (Signal) → Hermes (this module) → StudioGraphRunner
                                         → LangGraph StateGraph
                                         → Studio isolation workers

Usage:
    from studio_isolation.meta_orchestrator import MetaOrchestrator

    orch = await MetaOrchestrator.create(db_path="/tmp/checkpoints.db")
    result = await orch.execute(
        intent="Add a RateLimiter middleware to Boundary's gRPC interceptor chain",
        auto_ship=True,  # Skip human approval for auto-tasks
    )
    await orch.close()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Awaitable

from studio_isolation.langgraph_adapter import StudioGraphState, StudioGraphRunner
from studio_isolation.models import BundleProposal, CapabilityManifest

logger = logging.getLogger(__name__)


# ── Intent decomposition result ────────────────────────────────────────────────


@dataclass
class DecomposedIntent:
    """Output of intent → StateGraph initial state decomposition.

    Contains everything needed to launch a Studio graph for this intent.
    """

    bundle_id: str
    bundle_input: str
    target_repo: str = ""
    target_branch: str = ""
    proposal: dict[str, Any] = field(default_factory=dict)
    task_dag: dict[str, Any] = field(default_factory=dict)
    capability_manifest: dict[str, Any] = field(default_factory=dict)
    auto_ship: bool = False
    approval_tier: str = "full_review"
    tags: list[str] = field(default_factory=list)

    def to_initial_state(self) -> StudioGraphState:
        """Convert to a LangGraph-compatible initial state dict."""
        return {
            "bundle_input": self.bundle_input,
            "bundle_id": self.bundle_id,
            "proposal": self.proposal,
            "task_dag": self.task_dag,
            "auto_ship": self.auto_ship,
            "approval_tier": self.approval_tier,
            "approval_decision": "approved" if self.auto_ship else "",
            "approved": self.auto_ship,
            "review_findings": [],
            "branch_name": self.target_branch or f"studio/{self.bundle_id}",
        }


# ── Signal relay interface ─────────────────────────────────────────────────────

# Callback type: async function that sends a message and waits for a response
SignalRelay = Callable[[str], Awaitable[str]]


# ── Execution result ───────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    """Result of a full graph execution cycle."""

    bundle_id: str
    success: bool
    state: StudioGraphState
    pr_url: str = ""
    commit_sha: str = ""
    error: str = ""
    was_interrupted: bool = False
    human_decision: str = ""


# ── MetaOrchestrator ───────────────────────────────────────────────────────────


class MetaOrchestrator:
    """Hermes meta-orchestrator for Studio's LangGraph pipeline.

    Owns a StudioGraphRunner and provides the full lifecycle:
    decompose → launch → monitor → interrupt relay → report.

    Attributes:
        runner: The StudioGraphRunner managing the StateGraph and checkpointer.
        relay: Optional async callback for sending interrupt questions
               (e.g., to Signal). If None, interrupts raise an error.
    """

    def __init__(self, runner: StudioGraphRunner, relay: SignalRelay | None = None) -> None:
        self.runner = runner
        self.relay = relay

    @classmethod
    async def create(
        cls,
        db_path: str = ":memory:",
        relay: SignalRelay | None = None,
        studio_runner: Any = None,
        studio_db: Any = None,
    ) -> "MetaOrchestrator":
        """Create a new MetaOrchestrator.

        Args:
            db_path: SQLite path for checkpoints.
            relay: Optional Signal relay for human-in-the-loop interrupts.
            studio_runner: Optional Studio runner for real worker spawning.
            studio_db: Optional Studio DB handle.
        """
        runner = await StudioGraphRunner.create(
            db_path=db_path,
            studio_runner=studio_runner,
            studio_db=studio_db,
        )
        return cls(runner=runner, relay=relay)

    async def execute(
        self,
        intent: str,
        bundle_id: str | None = None,
        auto_ship: bool = False,
        target_repo: str = "learhy/boundary",
        relay: SignalRelay | None = None,
    ) -> ExecutionResult:
        """Execute an intent through the full Studio LangGraph pipeline.

        This is the main entry point for Hermes as meta-orchestrator.

        Args:
            intent: The user's intent (e.g., "Add rate limiter middleware").
            bundle_id: Optional bundle ID (auto-generated if None).
            auto_ship: If True, skip the human approval interrupt entirely.
            target_repo: Repository to target (default: learhy/boundary).
            relay: Per-invocation relay override. If both instance and
                   invocation relay are None, interrupts raise an error.

        Returns:
            ExecutionResult with final state, PR URL, and error info.
        """
        import ulid

        if bundle_id is None:
            bundle_id = f"bundle-{ulid.ULID()}"

        # 1. Decompose intent
        logger.info(f"[{bundle_id}] Decomposing intent: {intent[:100]}")
        decomposed = self.decompose_intent(
            intent=intent,
            bundle_id=bundle_id,
            target_repo=target_repo,
            auto_ship=auto_ship,
        )

        # 2. Launch graph
        active_relay = relay or self.relay
        config = self.runner.config_for(bundle_id)
        initial = decomposed.to_initial_state()

        logger.info(f"[{bundle_id}] Launching graph, auto_ship={auto_ship}")

        try:
            from langgraph.errors import GraphInterrupt
            final_state = await self.runner.graph.ainvoke(initial, config)

            # Graph completed without interrupt
            return ExecutionResult(
                bundle_id=bundle_id,
                success=True,
                state=final_state,
                pr_url=final_state.get("pr_url", ""),
                commit_sha=final_state.get("commit_sha", ""),
            )

        except GraphInterrupt:
            # Human-in-the-loop interrupt caught by LangGraph
            return await self._handle_interrupt(
                bundle_id=bundle_id,
                config=config,
                state=initial,  # Start from initial for resume
                relay=active_relay,
            )

        except Exception as e:
            # Unhandled error
            error_str = str(e)
            logger.error(f"[{bundle_id}] Graph execution failed: {error_str}")
            return ExecutionResult(
                bundle_id=bundle_id,
                success=False,
                state=initial,
                error=error_str,
            )

    async def _handle_interrupt(
        self,
        bundle_id: str,
        config: dict,
        state: StudioGraphState,
        relay: SignalRelay | None,
    ) -> ExecutionResult:
        """Handle a LangGraph interrupt — relay to human if configured."""
        from langgraph.types import Command

        question = state.get("proposal", {}).get("title", state.get("bundle_input", "Unknown proposal"))
        tier = state.get("approval_tier", "full_review")

        if relay is None:
            return ExecutionResult(
                bundle_id=bundle_id,
                success=False,
                state=state,
                error=f"No relay configured for interrupt: {question}",
                was_interrupted=True,
            )

        # Relay to human
        message = (
            f"🔔 Studio bundle needs approval\n"
            f"ID: {bundle_id}\n"
            f"Tier: {tier}\n"
            f"Proposal: {question}\n"
            f"Reply: approve | reject: <reason> | modify: <instructions>"
        )
        human_response = await relay(message)

        # Parse response
        parsed = self._parse_approval_response(human_response)

        # Resume the graph
        logger.info(f"[{bundle_id}] Resuming graph with response: {parsed}")
        try:
            resume_result = await self.runner.graph.ainvoke(
                Command(resume=parsed),
                config,
            )
            return ExecutionResult(
                bundle_id=bundle_id,
                success=parsed.get("approved", False),
                state=resume_result,
                pr_url=resume_result.get("pr_url", ""),
                commit_sha=resume_result.get("commit_sha", ""),
                was_interrupted=True,
                human_decision=parsed.get("decision", ""),
            )
        except Exception as e:
            logger.error(f"[{bundle_id}] Resume failed: {e}")
            return ExecutionResult(
                bundle_id=bundle_id,
                success=False,
                state=state,
                error=str(e),
                was_interrupted=True,
                human_decision=parsed.get("decision", ""),
            )

    @staticmethod
    def _parse_approval_response(response: str) -> dict[str, Any]:
        """Parse a human approval response into the dict expected by interrupt().

        Formats:
            "approve" → {"approved": True, "decision": "approved", "reason": ""}
            "reject: too risky" → {"approved": False, "decision": "rejected", "reason": "too risky"}
            "modify: add tests first" → {"approved": True, "decision": "modify", "reason": "add tests first"}
        """
        text = response.strip().lower()
        result: dict[str, Any] = {}

        if text.startswith("reject"):
            result["approved"] = False
            result["decision"] = "rejected"
            # Extract reason after "reject:" or "reject "
            parts = text.split(":", 1) if ":" in text else text.split(None, 1)
            result["reason"] = parts[1].strip() if len(parts) > 1 else ""
        elif text.startswith("modify"):
            result["approved"] = True  # Modify = approve with changes
            result["decision"] = "modify"
            parts = text.split(":", 1) if ":" in text else text.split(None, 1)
            result["reason"] = parts[1].strip() if len(parts) > 1 else ""
        else:  # Default: approve
            result["approved"] = True
            result["decision"] = "approved"
            result["reason"] = ""

        return result

    def decompose_intent(
        self,
        intent: str,
        bundle_id: str,
        target_repo: str = "learhy/boundary",
        auto_ship: bool = False,
    ) -> DecomposedIntent:
        """Decompose a human intent into a DecomposedIntent.

        This is a rule-based decomposition. In production, Hermes' AI
        does the decomposition. This method provides reasonable defaults
        that can be overridden by an AI caller.

        Args:
            intent: The user's intent in plain language.
            bundle_id: Unique bundle identifier.
            target_repo: Repository to target.
            auto_ship: Whether to auto-approve.
        """
        # Determine complexity based on keywords
        low_complexity = {"comment", "readme", "typo", "fix typo", "doc", "docs"}
        high_complexity = {"refactor", "redesign", "migrate", "rewrite", "api", "auth",
                          "billing", "secret", "encrypt", "rate limit", "ratelimit",
                          "middleware"}
        high_risk = {"auth", "billing", "secret", "encrypt", "payment", "pii",
                    "rate limit", "ratelimit", "middleware"}

        intent_lower = intent.lower()

        # Score complexity
        if any(kw in intent_lower for kw in low_complexity):
            complexity = 2
        elif any(kw in intent_lower for kw in high_complexity):
            complexity = 6
        else:
            complexity = 4

        # Score risk
        if any(kw in intent_lower for kw in high_risk):
            risk = 4
        else:
            risk = 2

        # Determine tier
        if complexity <= 3 and risk <= 2:
            tier = "auto"
            auto_ship = True
        elif complexity <= 6 and risk <= 5:
            tier = "full_review"
            auto_ship = auto_ship  # Honor explicit flag
        else:
            tier = "full_review_cooldown"
            auto_ship = False

        # Build proposal
        proposal = {
            "complexity_score": complexity,
            "risk_score": risk,
            "target": target_repo,
            "requirements_summary": intent,
            "implementation_plan": f"Implement: {intent}",
        }

        # Build task DAG
        task_dag = {
            "nodes": [
                {"id": "clone", "kind": "worker", "description": f"Clone {target_repo}"},
                {"id": "research", "kind": "worker", "description": "Research codebase"},
                {"id": "implement", "kind": "worker", "description": intent},
                {"id": "test", "kind": "worker", "description": "Run tests"},
                {"id": "pr", "kind": "worker", "description": "Create PR"},
            ]
        }

        # Tags
        tags: list[str] = []
        for kw, tag in [("auth", "auth"), ("billing", "billing"), ("secret", "secrets"),
                        ("rate limit", "rate-limiting"), ("ratelimit", "rate-limiting"),
                        ("middleware", "middleware")]:
            if kw in intent_lower:
                tags.append(tag)

        return DecomposedIntent(
            bundle_id=bundle_id,
            bundle_input=intent,
            target_repo=target_repo,
            proposal=proposal,
            task_dag=task_dag,
            auto_ship=auto_ship,
            approval_tier=tier,
            tags=tags,
        )

    async def monitor_stream(
        self,
        bundle_id: str,
        auto_ship: bool = False,
        on_node: Callable[[str, dict], None] | None = None,
        relay: SignalRelay | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Monitor a running graph via the checkpoint stream.

        Yields state after each node completes. Useful for building
        progress dashboards or logging.

        Args:
            bundle_id: The bundle/thread ID to monitor.
            auto_ship: If True, run to completion. False for interrupt.
            on_node: Optional callback invoked after each node completes.
            relay: Optional relay for interrupts.
        """
        config = self.runner.config_for(bundle_id)
        active_relay = relay or self.relay

        async for event in self.runner.graph.astream(None, config):
            node_name = list(event.keys())[0] if event else "unknown"
            node_state = event.get(node_name, {})

            logger.debug(f"[{bundle_id}] Stream event: {node_name}")
            if on_node:
                on_node(node_name, node_state)

            yield {node_name: node_state}

    async def close(self) -> None:
        """Close the orchestrator and underlying runner."""
        await self.runner.close()

    # ── Intent decomposition (AI-powered) ──────────────────────────────────────
    #
    # In production, these methods are called by Hermes' AI to provide
    # the full decomposition. The rule-based fallback above handles
    # simple cases when no AI is available.

    def set_decomposition(self, decomposed: DecomposedIntent) -> None:
        """Set a pre-decomposed intent (from AI)."""
        self._decomposed = decomposed

    def get_decomposition(self) -> DecomposedIntent | None:
        """Get the current decomposition if set."""
        return getattr(self, "_decomposed", None)
