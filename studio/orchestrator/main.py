"""Orchestrator entry point: wires all components and starts the event loop.

Single Unix domain socket serves both worker connections (persistent,
token-authenticated) and CLI/admin requests (one-shot JSON-RPC).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .github import GitHubClient

from .db import Database, create_database, DatabaseVersionError
from .models import BundleState
from .state_machine import BundleStateMachine
from .rpc import (
    RpcDispatcher,
    RpcHandlers,
    ConnectionManager,
    WorkerBinding,
    create_rpc_system,
    _make_error,
    _make_result,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    CAPABILITY_DENIED,
    INTERNAL_ERROR,
)
from .runner import LocalBwrapWorkerRunner, RemoteSSHWorkerRunner, K8sJobWorkerRunner, DockerWorkerRunner, RunnerSelector, DockerWorkerHandle
from . import tls as tls_helpers
# LangGraph replaces executor.py, scheduler.py, reconciler.py (Phase 5)
# Execution is now handled by Hermes → LangGraph adapter
from .models import Settings, OrchestratorSettings, ApprovalTier
from .approval import (
    evaluate_approval_matrix,
    cooldown_seconds,
    MandatoryReviewTrigger,
)
from .artifact import SecretStore
from .ops import OpsTooling
from .notify import Notifier
from .review import ReviewScheduler

logger = logging.getLogger(__name__)


class Orchestrator:
    """Top-level application that owns every subsystem."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.db: Database | None = None
        self.sm: BundleStateMachine | None = None
        self.dispatcher: RpcDispatcher | None = None
        self.handlers: RpcHandlers | None = None
        self.conn_mgr: ConnectionManager | None = None
        # Phase 5: LangGraph handles execution. Studio keeps runner and isolation.
        self.runner: "Any | None" = None
        # executor, scheduler, reconciler replaced by LangGraph adapter
        self._server: asyncio.AbstractServer | None = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self._http_server: "uvicorn.Server | None" = None
        self._poll_task: asyncio.Task | None = None
        self._http_task: asyncio.Task | None = None
        self.github_client: "GitHubClient | None" = None
        self._processed_comment_ids: set[int] = set()
        self._bundle_last_polled: dict[str, datetime] = {}
        self.ops: OpsTooling | None = None
        self._ops_task: asyncio.Task | None = None
        self._fleet_health_task: asyncio.Task | None = None
        self._k8s_watch_task: asyncio.Task | None = None
        self._ssh_runner: "RemoteSSHWorkerRunner | None" = None
        self._k8s_runner: "K8sJobWorkerRunner | None" = None
        self._docker_runner: "DockerWorkerRunner | None" = None
        self._secret_store: "SecretStore | None" = None
        self._review_scheduler: ReviewScheduler | None = None
        self._review_task: asyncio.Task | None = None
        self._running = False
        # Stale code detection
        self._startup_code_hash: str = ""
        self._code_stale: bool = False
        self._code_check_task: asyncio.Task | None = None

    async def _on_bundler_report(self, bundle_id: str, proposal: dict) -> None:
        """Callback from RpcHandlers when a bundler worker sends final_report."""
        await self.sm.transition_complete_bundler_planning(bundle_id, proposal)
        logger.info("Bundler planning complete for bundle %s: C=%s R=%s target=%s",
                     bundle_id, proposal.get("complexity_score"),
                     proposal.get("risk_score"), proposal.get("target"))

    # ── Stale code detection ──────────────────────────────────────────────

    @staticmethod
    def _compute_code_hash() -> str:
        """Hash all .py files under the studio/ package directory.

        Returns a hex digest that changes when any source file is modified,
        added, or removed. Walks deterministically (sorted paths) so the
        hash is stable for identical trees.
        """
        import hashlib as _hashlib
        studio_root = Path(__file__).resolve().parent.parent
        h = _hashlib.sha256()
        py_files = sorted(studio_root.rglob("*.py"))
        for fp in py_files:
            try:
                h.update(fp.read_bytes())
            except Exception:
                h.update(fp.name.encode())
        return h.hexdigest()

    async def _check_code_hash(self) -> None:
        """Compare current code hash to startup hash. Flag if stale."""
        current = self._compute_code_hash()
        if current != self._startup_code_hash:
            if not self._code_stale:
                self._code_stale = True
                logger.warning(
                    "Code change detected -- orchestrator running stale code. "
                    "Restart required. (startup_hash=%s, current_hash=%s)",
                    self._startup_code_hash[:12], current[:12],
                )
                await self._ntfy_alert(
                    "Orchestrator code stale",
                    f"Source files changed on disk but orchestrator has not been "
                    f"restarted. Workers may see old behavior. Restart required.",
                    priority=4,
                )

    async def _code_check_loop(self) -> None:
        """Background task: re-check code hash every 60 seconds."""
        while self._running:
            try:
                await self._check_code_hash()
            except Exception as exc:
                logger.debug("Code hash check failed: %s", exc)
            await asyncio.sleep(60)

    async def _ntfy_alert(self, title: str, message: str, priority: int = 4) -> None:
        """Send a push notification via ntfy if configured."""
        ntfy_url = self.settings.orchestrator.ntfy_url
        if not ntfy_url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                await client.post(
                    ntfy_url,
                    json={
                        "topic": ntfy_url.rsplit("/", 1)[-1],
                        "title": title,
                        "message": message,
                        "priority": priority,
                        "tags": ["warning"],
                    },
                )
        except Exception as exc:
            logger.debug("ntfy alert failed: %s", exc)
        # Phase 5: LangGraph handles dispatch. Studio isolates only.
        # Bundles are now managed by LangGraph, not the old DagExecutor.

    async def _on_bundler_failure(self, bundle_id: str, reason: str) -> None:
        """Callback from RpcHandlers when a bundler worker reports failure."""
        await self.sm.transition_bundler_failed(bundle_id, reason)
        logger.warning("Bundler failed for bundle %s: %s", bundle_id, reason)

    async def _on_review_complete(self, bundle_id: str, role: str, findings: list) -> None:
        """Callback from RpcHandlers when a review track worker completes successfully."""
        logger.info("Review track %s complete for bundle %s: %s findings",
                     role, bundle_id, len(findings))
        # Phase 5: LangGraph manages DAG edges, not the old DagExecutor.

    async def _on_review_blocking(self, bundle_id: str, blocking_reason: str) -> None:
        """Callback from RpcHandlers when a review track reports a blocking issue.

        Does NOT transition state — the approval matrix evaluator decides the tier
        based on all findings. A blocking finding will prevent AUTO tier but the
        bundle stays in_review for PM approval.
        """
        logger.warning("Review track blocking issue for bundle %s: %s", bundle_id, blocking_reason)

    async def _on_review_aggregator_complete(self, bundle_id: str, merged: dict) -> None:
        """Callback from DagExecutor when the review aggregator completes.

        Bundle 2.4: calls the approval matrix evaluator stub (always returns approved).
        Bundle 2.5: replaces with the real matrix evaluator.
        """
        # Normalize aggregator output: COLLECT strategy produces a list of
        # per-reviewer dicts; evaluator expects a dict keyed by role name.
        if isinstance(merged, list):
            findings_by_role: dict = {}
            for item in merged:
                if isinstance(item, dict):
                    role = item.get("role", "unknown")
                    findings_by_role[role] = item.get("findings", item)
                else:
                    findings_by_role.setdefault("unknown", []).append(item)
            merged = findings_by_role

        logger.info("Review tracks complete for bundle %s; evaluating approval matrix", bundle_id)
        await self._evaluate_approval_matrix(bundle_id, merged)

    async def _evaluate_approval_matrix(self, bundle_id: str, merged_findings: dict) -> None:
        """Evaluate the approval matrix with the real deterministic evaluator."""
        # Phase 5: Artifact store publishing now via LangGraph adapter.
        # Review summary is recorded through the LangGraph checkpoint stream.

        # Fetch bundle proposal data
        row = await self.db.fetch_one(
            "SELECT proposal_json, complexity_score, risk_score, tier, state FROM bundles WHERE id = ?",
            (bundle_id,),
        )
        if row is None:
            logger.error("Bundle %s not found for approval matrix evaluation", bundle_id)
            return

        # Guard: if bundle is already past review (e.g. review-aggregator dispatched
        # again as part of execution DAG), skip the approval transition.
        if row["state"] not in (BundleState.IN_REVIEW,):
            logger.debug("Bundle %s is already %s; skipping approval matrix re-evaluation", bundle_id, row["state"])
            return

        proposal_json = json.loads(row["proposal_json"] or "{}")
        bundler_proposal_raw = proposal_json.get("proposal", {})

        # Build a lightweight BundleProposal for the evaluator
        from .models import BundleProposal
        proposal = BundleProposal(
            complexity_score=row["complexity_score"] or 0,
            risk_score=row["risk_score"] or 0,
            estimated_loc=bundler_proposal_raw.get("estimated_loc", 0),
            estimated_duration_seconds=bundler_proposal_raw.get("estimated_duration_seconds", 0),
            estimated_worker_count=bundler_proposal_raw.get("estimated_worker_count", 0),
            estimated_tokens=bundler_proposal_raw.get("estimated_tokens", 0),
            target=bundler_proposal_raw.get("target", "control-plane"),
            concerns=bundler_proposal_raw.get("concerns", []),
            requirements_summary=bundler_proposal_raw.get("requirements_summary", ""),
            rfc_summary=bundler_proposal_raw.get("rfc_summary", ""),
            implementation_plan=bundler_proposal_raw.get("implementation_plan", ""),
            irreversible=bundler_proposal_raw.get("irreversible", False),
            tags=bundler_proposal_raw.get("tags", []),
            self_escalation_tier=bundler_proposal_raw.get("self_escalation_tier"),
            artifact_type=bundler_proposal_raw.get("artifact_type", "mixed"),
            verification_strategy=bundler_proposal_raw.get("verification_strategy"),
        )

        # Parse mandatory-review triggers from settings
        triggers = []
        for t in self.settings.approval.mandatory_review_triggers:
            triggers.append(MandatoryReviewTrigger(
                name=t.get("name", ""),
                description=t.get("description", ""),
                path_patterns=t.get("path_patterns", []),
                tag_matches=t.get("tag_matches", []),
                min_files_deleted=t.get("min_files_deleted"),
                target_new_repo=t.get("target_new_repo", False),
            ))

        decision = evaluate_approval_matrix(
            proposal=proposal,
            findings=merged_findings,
            triggers=triggers,
            bundle_tags=proposal.tags,
            self_escalation_tier=proposal.self_escalation_tier,
            settings=self.settings.approval,
        )

        tier_str = decision.tier.value
        logger.info("Approval matrix for bundle %s: tier=%s auto_ship=%s reason=%s",
                     bundle_id, tier_str, decision.auto_ship, decision.reason)

        # Write tier & decision into the database
        now = self.sm.now()
        await self.db.execute(
            "UPDATE bundles SET tier = ? WHERE id = ?",
            (tier_str, bundle_id),
        )
        await self.db.execute(
            "INSERT INTO approval_decisions (bundle_id, decision, surface, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bundle_id, tier_str, "system", "approval-matrix", now),
        )
        await self.db.conn.commit()

        # Tier-based surface behavior
        if decision.tier == ApprovalTier.FULL_REVIEW_COOLDOWN:
            # Set cooldown_until based on reversibility
            duration = cooldown_seconds(
                proposal.irreversible,
                self.settings.approval.cooldown_hours_reversible,
                self.settings.approval.cooldown_hours_irreversible,
            )
            cooldown_until = now + duration
            await self.db.execute(
                "UPDATE bundles SET cooldown_until = ? WHERE id = ?",
                (cooldown_until, bundle_id),
            )
            await self.db.conn.commit()
            logger.info("Cooldown set for bundle %s until %s (%ss)",
                        bundle_id, cooldown_until, duration)
            await self.sm._github_post_mirror(
                bundle_id,
                f"Approval matrix: {tier_str}. Cooldown until <t:{cooldown_until}>. Reason: {decision.reason}",
            )
        elif decision.tier == ApprovalTier.AUTO:
            # Auto-approve: fire transition 4 automatically
            await self.sm.transition_4_approve_from_review(bundle_id, "approval-matrix")
            await self.sm.transition_6_start_execution(bundle_id)
            # Phase 5: LangGraph handles execution. Bundle state transition is sufficient.
            await self.sm._github_post_mirror(
                bundle_id,
                f"Auto-approved (tier: {tier_str}, reason: {decision.reason})",
            )
        elif decision.tier == ApprovalTier.AUTO_NOTIFY:
            # Auto-notify: fire transition 4 automatically, post notification
            await self.sm.transition_4_approve_from_review(bundle_id, "approval-matrix")
            await self.sm.transition_6_start_execution(bundle_id)
            # Phase 5: LangGraph handles execution.
            await self.sm._github_post_mirror(
                bundle_id,
                f"Auto-approved with notification (tier: {tier_str}, reason: {decision.reason})",
            )
        else:
            # SUMMARY, FULL_REVIEW: notify PM, wait for explicit decision
            await self.sm._github_post_mirror(
                bundle_id,
                f"Approval matrix: {tier_str}. Awaiting reviewer decision. Reason: {decision.reason}",
            )

    # ── Post-execution QA callbacks ────────────────────────────────────────

    async def _on_bundle_verifying(self, bundle_id: str) -> None:
        """Phase 5: LangGraph handles QA verification via its qa_verification node."""
        # QA worker spawning is now managed by the LangGraph adapter.
        pass

    async def _on_qa_pass(self, bundle_id: str, verification_report: dict) -> None:
        """QA verification passed: fire Transition 17 (VERIFYING -> COMPLETE)."""
        outcome = {"status": "shipped", "verification": verification_report}
        await self.sm.transition_17_complete(bundle_id, outcome)
        await self._record_calibration(bundle_id)
        logger.info("QA verification passed for bundle %s", bundle_id)

    async def _on_qa_fail(self, bundle_id: str, reason: str, verification_report: dict) -> None:
        """QA verification failed: fire Transition 19 (VERIFYING -> FAILED)."""
        await self.sm.transition_19_fail_verification(
            bundle_id,
            reason or "Verification failed; see report for details",
        )
        # Store the verification report in the bundle outcome
        await self.db.execute(
            "UPDATE bundles SET outcome_json = ? WHERE id = ?",
            (json.dumps({
                "status": "failed_verification",
                "rationale": reason,
                "verification": verification_report,
            }), bundle_id),
        )
        await self.db.conn.commit()
        await self._record_calibration(bundle_id)
        logger.warning("QA verification failed for bundle %s: %s", bundle_id, reason)

    # ── Inject context callback (Bundle 5.1) ────────────────────────────────

    async def _on_inject_context(self, worker_id: str, injection_id: str,
                                  context_type: str, content: str,
                                  question_id: str | None) -> None:
        """Send inject_context to a worker and await acknowledgement."""
        try:
            result = await self.conn_mgr.call_worker(
                worker_id,
                "worker.inject_context",
                {
                    "injection_id": injection_id,
                    "type": context_type,
                    "content": content,
                    "question_id": question_id,
                },
                timeout=30.0,
            )
            if result is None:
                logger.warning("inject_context timeout for worker %s (injection_id=%s)",
                               worker_id, injection_id)
            else:
                logger.info("inject_context acknowledged by worker %s (injection_id=%s)",
                            worker_id, injection_id)
        except ValueError:
            logger.warning("Cannot send inject_context: worker %s not connected", worker_id)

    # ── Bundle complete callback (Bundle 5.3) ───────────────────────────

    async def _on_bundle_complete(self, bundle_id: str) -> None:
        """Post final report and review quality feedback comment to GitHub."""
        if self.github_client is None:
            return

        row = await self.db.fetch_one(
            "SELECT github_issue_number, proposal_json, outcome_json FROM bundles WHERE id = ?",
            (bundle_id,),
        )
        if row is None or row["github_issue_number"] is None:
            return

        try:
            outcome = json.loads(row["outcome_json"] or "{}")
        except json.JSONDecodeError:
            outcome = {}
        try:
            proposal = json.loads(row["proposal_json"] or "{}")
        except json.JSONDecodeError:
            proposal = {}

        from .escalation import format_final_report_comment
        body = format_final_report_comment(bundle_id, outcome, proposal)
        await self.github_client.post_comment(row["github_issue_number"], body)
        logger.info("Final report posted to GitHub for bundle %s", bundle_id)

        # ── Bundle 5.4: PM review quality feedback ─────────────────────────

        int_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_interventions WHERE bundle_id = ?",
            (bundle_id,),
        )
        interventions_count = int_row["cnt"] if int_row else 0

        feedback_threshold = self.settings.review.feedback_threshold_interventions
        if interventions_count >= feedback_threshold:
            feedback_body = (
                f"## Review quality feedback\n\n"
                f"This bundle had {interventions_count} mid-flight interventions. Were they helpful?\n\n"
                f"- `/review-good` — interventions were appropriate and helpful\n"
                f"- `/review-noisy` — too many interventions, worker was on track\n"
                f"- `/review-missed` — important issues weren't caught"
            )
            await self.github_client.post_comment(row["github_issue_number"], feedback_body)
            logger.info("Review quality feedback posted for bundle %s (%d interventions)",
                        bundle_id, interventions_count)

    # ── Checkpoint callback (Bundle 5.2, updated 5.3) ────────────────────

    async def _on_worker_checkpoint(self, worker_id: str, bundle_id: str,
                                    node_id: str, checkpoint_data: dict) -> None:
        """Post-checkpoint: trigger review AND post to GitHub every 3rd or if concerns."""
        # Trigger review via ReviewScheduler
        if self._review_scheduler is not None:
            await self._review_scheduler.trigger_review(
                worker_id, bundle_id, node_id, "post_checkpoint",
            )

        # Post to GitHub: every 3rd checkpoint or when concerns non-empty
        concerns = checkpoint_data.get("concerns", [])
        if self.github_client is None:
            return

        # Count checkpoints for this worker to determine "every 3rd"
        count_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_checkpoints WHERE worker_id = ?",
            (worker_id,),
        )
        checkpoint_count = count_row["cnt"] if count_row else 0

        if checkpoint_count % 3 == 0 or concerns:
            bundle_row = await self.db.fetch_one(
                "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
            )
            if bundle_row and bundle_row["github_issue_number"]:
                from .escalation import format_checkpoint_comment
                body = format_checkpoint_comment(
                    worker_id,
                    checkpoint_data.get("phase_completed", ""),
                    checkpoint_data.get("phase_starting", ""),
                    checkpoint_data.get("summary", ""),
                    concerns,
                )
                await self.github_client.post_comment(
                    bundle_row["github_issue_number"], body,
                )
                logger.info("Checkpoint comment posted for worker %s (#%d)",
                           worker_id, checkpoint_count)

    # ── Calibration loop ───────────────────────────────────────────────────

    async def _record_calibration(self, bundle_id: str) -> None:
        """Record estimated vs actual on all axes after bundle reaches terminal state."""
        row = await self.db.fetch_one(
            "SELECT proposal_json, outcome_json FROM bundles WHERE id = ?", (bundle_id,)
        )
        if row is None:
            return

        try:
            proposal = json.loads(row["proposal_json"] or "{}")
        except json.JSONDecodeError:
            proposal = {}
        try:
            outcome = json.loads(row["outcome_json"] or "{}")
        except json.JSONDecodeError:
            outcome = {}

        # Estimates come from the bundler proposal block
        bundler_proposal = proposal.get("proposal", {})
        estimated_loc = bundler_proposal.get("estimated_loc", 0)
        estimated_duration = bundler_proposal.get("estimated_duration_seconds", 0)
        estimated_workers = bundler_proposal.get("estimated_worker_count", 0)
        estimated_tokens = bundler_proposal.get("estimated_tokens", 0)

        # Actuals from execution
        actual_loc = outcome.get("calibration", {}).get("actual_loc", 0)
        actual_duration = outcome.get("calibration", {}).get("actual_duration_seconds", 0)
        actual_workers = outcome.get("calibration", {}).get("actual_worker_count", 0)
        actual_tokens = outcome.get("calibration", {}).get("actual_tokens", 0)

        # Aggregate tokens from heartbeat tracking (Bundle 5.4)
        tokens_row = await self.db.fetch_one(
            "SELECT SUM(tokens_used) as total_tokens FROM workers WHERE bundle_id = ?",
            (bundle_id,),
        )
        heartbeat_tokens = tokens_row["total_tokens"] if tokens_row and tokens_row["total_tokens"] else 0
        if heartbeat_tokens > 0:
            actual_tokens = heartbeat_tokens

        # ── Bundle 5.4: review quality dimensions ──────────────────────────

        # Intervention count
        int_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_interventions WHERE bundle_id = ?",
            (bundle_id,),
        )
        interventions_count = int_row["cnt"] if int_row else 0

        # Questions stats
        q_asked_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_questions WHERE bundle_id = ?",
            (bundle_id,),
        )
        questions_asked = q_asked_row["cnt"] if q_asked_row else 0

        q_llm_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_questions "
            "WHERE bundle_id = ? AND status = ? AND answered_by = ?",
            (bundle_id, "answered", "llm"),
        )
        questions_llm_answered = q_llm_row["cnt"] if q_llm_row else 0

        q_esc_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_questions WHERE bundle_id = ? AND status = ?",
            (bundle_id, "escalated"),
        )
        questions_escalated = q_esc_row["cnt"] if q_esc_row else 0

        # Checkpoints count
        cp_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_checkpoints WHERE bundle_id = ?",
            (bundle_id,),
        )
        checkpoints_count = cp_row["cnt"] if cp_row else 0

        # Escalation response time (avg seconds from escalated_at to answered_at)
        resp_row = await self.db.fetch_one(
            "SELECT AVG(answered_at - escalated_at) as avg_resp FROM worker_questions "
            "WHERE bundle_id = ? AND status = ? AND escalated_at IS NOT NULL AND answered_at IS NOT NULL",
            (bundle_id, "answered"),
        )
        escalation_response_time_seconds = (
            int(resp_row["avg_resp"]) if resp_row and resp_row["avg_resp"] is not None else 0
        )

        # interventions_correct from PM feedback
        fb_row = await self.db.fetch_one(
            "SELECT feedback_type FROM review_calibration WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 1",
            (bundle_id,),
        )
        if fb_row:
            if fb_row["feedback_type"] == "good":
                interventions_correct = interventions_count
            else:
                interventions_correct = 0
        else:
            interventions_correct = 0

        # ── Bundle 6.4: code quality dimensions ──────────────────────────

        # Developer verification attempts from dag_nodes output_json
        dev_rows = await self.db.fetch_all(
            "SELECT output_json FROM dag_nodes WHERE bundle_id = ? AND kind = 'worker'",
            (bundle_id,),
        )
        developer_verification_attempts = 0
        first_attempt_pass = False
        for r in dev_rows or []:
            try:
                output = json.loads(r["output_json"] or "{}")
                attempts = output.get("attempts", 1)
                if isinstance(attempts, int) and attempts > developer_verification_attempts:
                    developer_verification_attempts = attempts
                # A worker passed on first attempt if attempts == 1 and outcome was success
                if isinstance(attempts, int) and attempts == 1 and output.get("outcome") == "success":
                    first_attempt_pass = True
            except (json.JSONDecodeError, TypeError):
                pass
        # If no dev workers ran, don't claim first_attempt_pass
        if developer_verification_attempts == 0:
            first_attempt_pass = False

        # QA criterion scores from outcome_json
        verification = outcome.get("verification", {})
        qa_criterion_scores = verification.get("criterion_scores", [])
        qa_criterion_pass_rate = None
        if qa_criterion_scores:
            passing = sum(1 for s in qa_criterion_scores if s.get("pass_fail"))
            qa_criterion_pass_rate = round(passing / len(qa_criterion_scores), 2)

        # Verification strategy accuracy: strategy was provided AND usable
        verification_strategy = bundler_proposal.get("verification_strategy")
        artifact_type = bundler_proposal.get("artifact_type", "")
        verification_strategy_provided = bool(verification_strategy)
        verification_passed = outcome.get("status") == "shipped"
        verification_strategy_accurate = verification_strategy_provided and verification_passed

        # Failure category distribution from verification failures
        failure_categories: dict[str, int] = {}
        dev_failures = verification.get("failures", [])
        if not dev_failures:
            # Try from criteria_results
            criteria_results = verification.get("criteria_results", [])
            for cr in criteria_results:
                if not cr.get("passed"):
                    evidence = cr.get("evidence", "")
                    from studio.orchestrator.artifacts import categorize_failure
                    cat = categorize_failure(evidence, "")
                    failure_categories[cat] = failure_categories.get(cat, 0) + 1

        # Compute divergence: >50% on any axis triggers post-mortem flag
        def _pct_divergence(estimated: int, actual: int) -> float | None:
            if estimated == 0:
                return None
            return abs(actual - estimated) / estimated

        diverged: list[str] = []
        for axis_name, est, act in [
            ("loc", estimated_loc, actual_loc),
            ("duration_seconds", estimated_duration, actual_duration),
            ("worker_count", estimated_workers, actual_workers),
            ("tokens", estimated_tokens, actual_tokens),
        ]:
            pct = _pct_divergence(est, act)
            if pct is not None and pct > 0.5:
                diverged.append(axis_name)

        entry = {
            "bundle_id": bundle_id,
            "recorded_at": self.sm.now(),
            "estimated_loc": estimated_loc,
            "actual_loc": actual_loc,
            "estimated_duration_seconds": estimated_duration,
            "actual_duration_seconds": actual_duration,
            "estimated_worker_count": estimated_workers,
            "actual_worker_count": actual_workers,
            "estimated_tokens": estimated_tokens,
            "actual_tokens": actual_tokens,
            "divergence_threshold_exceeded": diverged,
            "interventions_count": interventions_count,
            "interventions_correct": interventions_correct,
            "questions_asked": questions_asked,
            "questions_llm_answered": questions_llm_answered,
            "questions_escalated": questions_escalated,
            "escalation_response_time_seconds": escalation_response_time_seconds,
            "checkpoints_count": checkpoints_count,
            "developer_verification_attempts": developer_verification_attempts,
            "first_attempt_pass": first_attempt_pass,
            "verification_strategy_provided": verification_strategy_provided,
            "verification_strategy_accurate": verification_strategy_accurate,
            "qa_criterion_scores": qa_criterion_scores,
            "qa_criterion_pass_rate": qa_criterion_pass_rate,
            "artifact_type": artifact_type,
            "failure_categories": failure_categories,
        }

        # Write to memory/calibration/scoring-outcomes.jsonl
        memory_root = self.settings.orchestrator.memory_root
        cal_path = Path(memory_root) / "calibration" / "scoring-outcomes.jsonl"
        try:
            cal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cal_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            logger.info("Calibration recorded for bundle %s, diverged: %s", bundle_id, diverged)
        except Exception as exc:
            logger.warning("Failed to write calibration for %s: %s", bundle_id, exc)

        # Post-mortem prompt when any axis diverged >50%
        if diverged:
            postmortem_path = Path(memory_root) / "post-mortems" / f"{bundle_id}.json"
            try:
                postmortem_path.parent.mkdir(parents=True, exist_ok=True)
                postmortem_data = {
                    "bundle_id": bundle_id,
                    "trigger": "divergence_threshold_exceeded",
                    "diverged_axes": diverged,
                    "calibration": entry,
                    "proposal_summary": bundler_proposal.get("requirements_summary", ""),
                    "outcome_status": outcome.get("status", "unknown"),
                }
                with open(postmortem_path, "w") as f:
                    json.dump(postmortem_data, f, indent=2)
                logger.info("Post-mortem written for bundle %s: %s", bundle_id, diverged)
            except Exception as exc:
                logger.warning("Failed to write post-mortem for %s: %s", bundle_id, exc)

        # Bundle 6.4: Todoist escalation when thresholds exceeded
        todoist_attempts_threshold = developer_verification_attempts >= 4
        todoist_qa_threshold = (
            qa_criterion_pass_rate is not None and qa_criterion_pass_rate < 0.7
        )
        if todoist_attempts_threshold or todoist_qa_threshold:
            try:
                from .todoist import create_review_task
                bundle_idea = bundler_proposal.get("requirements_summary", bundle_id)
                await create_review_task(
                    bundle_id=bundle_id,
                    bundle_idea=bundle_idea,
                    attempts=developer_verification_attempts,
                    qa_pass_rate=qa_criterion_pass_rate,
                    artifact_type=artifact_type,
                )
            except Exception as exc:
                logger.warning("Failed to create Todoist task for %s: %s", bundle_id, exc)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all subsystems, recover state, and begin serving."""
        cfg = self.settings.orchestrator

        # 0. Compute code hash at startup for stale-code detection
        self._startup_code_hash = self._compute_code_hash()
        logger.info("Startup code hash: %s", self._startup_code_hash[:16])

        # 1. Database
        self.db = await create_database(cfg.db_path)

        # 2. State machine (kernel mode — Phase 1 approves/rejects directly)
        self.sm = BundleStateMachine(self.db, kernel_mode=True)
        self.sm.set_on_bundle_complete(self._on_bundle_complete)

        # 2.5. GitHub client (non-blocking — failures logged, transitions proceed)
        if self.settings.github.enabled:
            from .github import GitHubClient
            self.github_client = GitHubClient(self.settings.github)
            await self.github_client.initialize()
            self.sm.set_github_client(self.github_client)
            logger.info("GitHub client initialized for %s/%s",
                        self.settings.github.owner, self.settings.github.repo)
        else:
            self.github_client = None

        # 3. RPC system
        self.dispatcher, self.handlers, self.conn_mgr = create_rpc_system(
            self.db, cfg.socket_path, self.sm
        )

        # Wire bundler final_report callback: when bundler completes, merge
        # proposal + DAG into the bundle and transition PROPOSED -> IN_REVIEW.
        self.handlers.set_on_bundler_report(self._on_bundler_report)
        self.handlers.set_on_bundler_failure(self._on_bundler_failure)

        # Wire review track callbacks
        self.handlers.set_on_review_complete(self._on_review_complete)
        self.handlers.set_on_review_blocking(self._on_review_blocking)

        # Wire post-execution QA callbacks
        self.handlers.set_on_qa_pass(self._on_qa_pass)
        self.handlers.set_on_qa_fail(self._on_qa_fail)

        # Wire inject_context callback (Bundle 5.1)
        self.handlers.set_on_inject_context(self._on_inject_context)

        # Wire checkpoint callback for post-checkpoint reviews (Bundle 5.2)
        self.handlers.set_on_checkpoint(self._on_worker_checkpoint)

        # Bundle 5.3: give handlers access to conn_mgr and github_client for escalation
        self.handlers.set_conn_mgr(self.conn_mgr)
        self.handlers.set_github_client(self.github_client)

        # 3.5. Secret store (hybrid file+env, Bundle 3.4)
        self._secret_store = SecretStore(
            self.settings.secrets_config,
            memory_root=cfg.memory_root,
        )
        self.handlers.set_secret_store(self._secret_store)
        logger.info("Secret store initialized (memory_root=%s)", cfg.memory_root)

        # 4. Worker runner (use noop for testing if bwrap unavailable)
        if os.environ.get("STUDIO_TEST_MODE") == "1":
            from .runner import NoopWorkerRunner
            self.runner = NoopWorkerRunner(
                self.db,
                token_expiry_minutes=self.settings.ops.worker_token_expiry_minutes,
            )
            logger.info("Test mode: using NoopWorkerRunner")
        else:
            # Bundle 4.4: instantiate all enabled runners, wrap in RunnerSelector
            common = dict(
                egress_proxy=self.settings.egress_proxy,
                token_expiry_minutes=self.settings.ops.worker_token_expiry_minutes,
                ca_cert_path=self.settings.remote_workers.tls_ca_cert_path if self.settings.remote_workers.enabled else "",
                ca_key_path=self.settings.remote_workers.tls_ca_key_path if self.settings.remote_workers.enabled else "",
            )
            local_runner = LocalBwrapWorkerRunner(
                self.db, cfg.socket_path, **common,
            )
            ssh_runner: RemoteSSHWorkerRunner | None = None
            k8s_runner: K8sJobWorkerRunner | None = None
            docker_runner: DockerWorkerRunner | None = None

            if self.settings.remote_fleet.enabled:
                ssh_runner = RemoteSSHWorkerRunner(
                    self.db, self.settings.remote_fleet, **common,
                )
                logger.info("RemoteSSHWorkerRunner enabled (%d hosts)",
                            len(self.settings.remote_fleet.hosts))
            if self.settings.k8s_runner.enabled:
                k8s_runner = K8sJobWorkerRunner(
                    self.db, self.settings.k8s_runner, **common,
                )
                logger.info("K8sJobWorkerRunner enabled (ns=%s)",
                            self.settings.k8s_runner.namespace)
            if self.settings.docker_runner.enabled:
                docker_runner = DockerWorkerRunner(
                    self.db, self.settings.docker_runner, **common,
                )
                logger.info("DockerWorkerRunner enabled (image=%s)",
                            self.settings.docker_runner.worker_image)

            self.runner = RunnerSelector(
                self.db,
                self.settings.runner_selector,
                local=local_runner,
                remote_ssh=ssh_runner,
                k8s=k8s_runner,
                docker=docker_runner,
            )
            logger.info("RunnerSelector: %s", ", ".join(self.runner.runner_names))

            # Store individual runner refs for fleet health / k8s watch / CLI handlers
            self._ssh_runner = ssh_runner
            self._k8s_runner = k8s_runner
            self._docker_runner = docker_runner

            # Check bwrap availability at startup
            try:
                self._bwrap_available = await local_runner._check_bwrap()
            except (TypeError, AttributeError):
                # _check_bwrap may not be awaitable in test contexts
                self._bwrap_available = True

            # Check rootfs freshness at startup (Phase 7.3)
            if self.settings.firecracker.enabled:
                from studio.orchestrator.firecracker import check_rootfs_freshness
                rootfs_check = check_rootfs_freshness(self.settings.firecracker.rootfs_path)
                if not rootfs_check["fresh"]:
                    logger.warning(rootfs_check["warning"])

            if self._bwrap_available:
                logger.info("Sandbox: bubblewrap (active)")
            else:
                logger.warning("Sandbox: none (WARNING: workers running unsandboxed)")

        # Phase 5: LangGraph replaces executor, scheduler, reconciler.
        # The isolation layer (runner, capability, RPC, approval) remains.
        # Execution is managed by the Hermes → LangGraph adapter.

        # 5.1. Start review scheduler (Bundle 5.2) — this is STUDIO's review,
        #      not LangGraph's. ReviewScheduler triggers review workers independently.
        if self.settings.review.enabled:
            self._review_scheduler = ReviewScheduler(
                self.db, self.settings.review, self.handlers, self.conn_mgr,
                github_client=self.github_client,
            )
            await self._review_scheduler.start()
            logger.info("ReviewScheduler started")

        # 9.5. Start HTTP server + GitHub polling (if enabled)
        if self.settings.github.enabled:
            self._http_task = asyncio.create_task(self._run_http_server())
            self._poll_task = asyncio.create_task(self._poll_github_issues())
            logger.info("HTTP server + GitHub polling started")

        # 9.6. Initialize ops tooling (Bundle 3.3)
        post_comment = None
        if self.github_client is not None:
            async def _post(issue_number: int, body: str) -> None:
                await self.github_client.post_comment(issue_number, body)
            post_comment = _post

        notifier = Notifier(
            log_path=Path(self.settings.orchestrator.memory_root) / "notifications" / "log.jsonl",
            post_comment=post_comment,
        )
        self.ops = OpsTooling(self.db.conn, self.settings.ops, notifier)
        self._ops_task = asyncio.create_task(self._run_ops_loop())
        logger.info("Ops tooling started (stall threshold=%dh, recall=%dh)",
                     self.settings.ops.stall_threshold_hours,
                     self.settings.ops.recall_window_hours)

        # 9.6.5. Stale code detection (poll every 60s)
        self._code_check_task = asyncio.create_task(self._code_check_loop())
        logger.info("Stale code detection started")

        # 9.7. Fleet health monitoring (Bundle 4.2)
        if self._ssh_runner is not None:
            self._fleet_health_task = asyncio.create_task(self._run_fleet_health_loop(self._ssh_runner))
            logger.info("Fleet health monitoring started (%d hosts)", len(self.settings.remote_fleet.hosts))

        # 9.8. K8s Pod event watching (Bundle 4.3)
        if self._k8s_runner is not None:
            await self._k8s_runner.start_watch()
            logger.info("K8s Pod event watching started (ns=%s)", self.settings.k8s_runner.namespace)

        # 10. Bind Unix socket (always — workers + CLI + MCP)
        socket_path = cfg.socket_path
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=socket_path
        )
        os.chmod(socket_path, 0o660)
        logger.info("Orchestrator listening on %s", socket_path)

        # 10.5. Start TCP/TLS listener for remote workers with mutual TLS (Bundle 4.1)
        if self.settings.remote_workers.enabled:
            rw = self.settings.remote_workers
            # Generate CA at startup (idempotent)
            tls_helpers.generate_ca(rw.tls_ca_cert_path, rw.tls_ca_key_path)
            tls_ctx = tls_helpers.create_server_tls_context(
                rw.tls_ca_cert_path, rw.tls_server_cert_path, rw.tls_server_key_path
            )
            addr = self.settings.remote_workers.listen_addr
            host, _, port_str = addr.partition(":")
            port = int(port_str)

            self._tcp_server = await asyncio.start_server(
                self._handle_connection,
                host=host,
                port=port,
                ssl=tls_ctx,
            )
            logger.info("Orchestrator TCP/TLS listener on %s", addr)

            # Record remote workers enabled in audit trail
            await self.db.execute(
                "INSERT OR REPLACE INTO settings_metadata (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("remote_workers_enabled", "1", int(time.time())),
            )
            await self.db.conn.commit()

        # Record k8s runner enabled state (Bundle 4.3)
        if self.settings.k8s_runner.enabled:
            await self.db.execute(
                "INSERT OR REPLACE INTO settings_metadata (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("k8s_runner_enabled", "1", int(time.time())),
            )
            await self.db.conn.commit()

        # Record runner_selector enabled state (Bundle 4.4)
        await self.db.execute(
            "INSERT OR REPLACE INTO settings_metadata (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            ("runner_selector_enabled", "1", int(time.time())),
        )
        await self.db.conn.commit()

        # Record docker runner enabled state (Bundle 4.5)
        if self.settings.docker_runner.enabled:
            await self.db.execute(
                "INSERT OR REPLACE INTO settings_metadata (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("docker_runner_enabled", "1", int(time.time())),
            )
            await self.db.conn.commit()

        self._running = True

    async def stop(self) -> None:
        """Graceful shutdown: stop accepting, drain loops, close DB."""
        self._running = False
        logger.info("Shutting down...")

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._ops_task:
            self._ops_task.cancel()
            try:
                await self._ops_task
            except asyncio.CancelledError:
                pass

        if self._fleet_health_task:
            self._fleet_health_task.cancel()
            try:
                await self._fleet_health_task
            except asyncio.CancelledError:
                pass

        if self._code_check_task:
            self._code_check_task.cancel()
            try:
                await self._code_check_task
            except asyncio.CancelledError:
                pass

        if self._k8s_runner is not None:
            await self._k8s_runner.close()

        if self._http_server:
            self._http_server.should_exit = True
            if self._http_task:
                try:
                    await self._http_task
                except asyncio.CancelledError:
                    pass

        if self.github_client:
            await self.github_client.close()

        if self._review_scheduler:
            await self._review_scheduler.stop()

        # Phase 5: No scheduler to stop — LangGraph handles its own lifecycle.

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()

        # Close lingering worker connections
        if self.conn_mgr:
            for binding in list(self.conn_mgr._by_worker_id.values()):
                try:
                    binding.writer.close()
                except Exception:
                    pass

        if self.db:
            await self.db.close()

        logger.info("Orchestrator stopped")

    # ── GitHub HTTP server + polling ──────────────────────────────────────

    async def _run_http_server(self) -> None:
        """Run a Starlette HTTP listener for /health and /github/webhook."""
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Route

            async def health(request) -> JSONResponse:
                return JSONResponse({"status": "ok"})

            async def webhook(request) -> JSONResponse:
                # Validate HMAC-SHA256 signature if webhook secret is configured
                webhook_secret = self.settings.github.webhook_secret
                if webhook_secret:
                    sig_header = request.headers.get("X-Hub-Signature-256", "")
                    if not sig_header.startswith("sha256="):
                        return JSONResponse({"error": "unsigned"}, status_code=401)
                    body_bytes = await request.body()
                    expected = "sha256=" + hmac.new(
                        webhook_secret.encode(), body_bytes, hashlib.sha256
                    ).hexdigest()
                    if not hmac.compare_digest(sig_header, expected):
                        return JSONResponse({"error": "invalid signature"}, status_code=401)
                try:
                    body = await request.json() if not webhook_secret else json.loads(body_bytes)
                    event = request.headers.get("X-GitHub-Event", "")
                    if event == "issue_comment":
                        action = body.get("action", "")
                        comment = body.get("comment", {})
                        issue = body.get("issue", {})
                        sender = body.get("sender", {}).get("login", "")
                        if action == "created" and sender != "studio-agents[bot]":
                            comment_id = comment.get("id", 0)
                            if comment_id not in self._processed_comment_ids:
                                issue_number = issue.get("number", 0)
                                bundle_id = await self._resolve_bundle_by_issue(issue_number)
                                if bundle_id:
                                    await self._process_comment(bundle_id, comment)
                                    self._processed_comment_ids.add(comment_id)
                except Exception:
                    pass
                return JSONResponse({"received": True})

            routes = [
                Route("/health", health, methods=["GET"]),
                Route("/github/webhook", webhook, methods=["POST"]),
            ]
            app = Starlette(routes=routes)

            cfg = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.settings.orchestrator.http_port,
                log_level="warning",
            )
            self._http_server = uvicorn.Server(cfg)
            await self._http_server.serve()
        except Exception as exc:
            logger.warning("HTTP server failed: %s", exc)

    async def _poll_github_issues(self) -> None:
        """Poll open GitHub issues for new slash-command comments every poll_interval_seconds."""
        interval = self.settings.github.poll_interval_seconds
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.warning("GitHub poll error: %s", exc)
            await asyncio.sleep(interval)

    async def _run_ops_loop(self) -> None:
        """Periodic ops checks: stall detection, escalation, acting-soon (Bundle 3.3)."""
        while self._running:
            try:
                await self.ops.check_stalled_bundles()
                await self.ops.check_escalation_ladder()
                await self.ops.check_acting_soon()
            except Exception as exc:
                logger.warning("Ops loop error: %s", exc)
            await asyncio.sleep(60)

    async def _run_fleet_health_loop(self, runner: "RemoteSSHWorkerRunner") -> None:
        """Ping fleet hosts every 60s, mark unhealthy ones as degraded (Bundle 4.2)."""
        while self._running:
            try:
                await runner.ping_hosts()
            except Exception as exc:
                logger.warning("Fleet health loop error: %s", exc)
            await asyncio.sleep(60)

    async def _poll_once(self) -> None:
        if self.github_client is None:
            return
        rows = await self.db.fetch_all(
            "SELECT id, github_issue_number FROM bundles "
            "WHERE github_issue_number IS NOT NULL AND state IN (?, ?, ?)",
            (BundleState.IN_REVIEW, BundleState.PAUSED, BundleState.COMPLETE),
        )
        for row in rows:
            bundle_id = row["id"]
            issue_number = row["github_issue_number"]
            since = self._bundle_last_polled.get(bundle_id)
            comments = await self.github_client.get_comments_since(issue_number, since)
            for comment in comments:
                comment_id = comment.get("id", 0)
                if comment_id not in self._processed_comment_ids:
                    await self._process_comment(bundle_id, comment)
                    self._processed_comment_ids.add(comment_id)
            self._bundle_last_polled[bundle_id] = datetime.now(timezone.utc)

    async def _resolve_bundle_by_issue(self, issue_number: int) -> str | None:
        row = await self.db.fetch_one(
            "SELECT id FROM bundles WHERE github_issue_number = ?", (issue_number,)
        )
        return row["id"] if row else None

    async def _process_comment(self, bundle_id: str, comment: dict) -> None:
        body = comment.get("body", "").strip()
        user = comment.get("user", {}).get("login", "github-user")
        actor = f"github:{user}"

        if not body:
            return

        # Parse slash commands
        if re.match(r"^/approve\s*$", body, re.IGNORECASE):
            await self.sm.transition_4_approve_from_review(bundle_id, actor)
            await self._acknowledge_comment(bundle_id, user, "approve")
        elif (m := re.match(r"^/reject\s+(.+)$", body, re.IGNORECASE | re.DOTALL)):
            reason = m.group(1).strip()
            await self.sm.transition_reject_from_review(bundle_id, actor, reason)
            await self._acknowledge_comment(bundle_id, user, "reject")
        elif (m := re.match(r"^/modify\s+(.+)$", body, re.IGNORECASE | re.DOTALL)):
            instructions = m.group(1).strip()
            await self.sm.transition_3_return_to_proposed(bundle_id, instructions)
            await self._acknowledge_comment(bundle_id, user, "modify")
        elif (m := re.match(r"^/answer:(\S+)\s+(.+)$", body, re.IGNORECASE | re.DOTALL)):
            question_id = m.group(1).strip()
            answer_text = m.group(2).strip()
            await self._handle_answer_command(bundle_id, question_id, answer_text, actor)
        elif (m := re.match(r"^/resume:(\S+)(?:\s+(.+))?$", body, re.IGNORECASE | re.DOTALL)):
            worker_id = m.group(1).strip()
            context = m.group(2).strip() if m.group(2) else ""
            await self._handle_resume_command(bundle_id, worker_id, context, actor)
        elif re.match(r"^/review-good\s*$", body, re.IGNORECASE):
            await self._handle_review_feedback(bundle_id, "good", actor)
        elif re.match(r"^/review-noisy\s*$", body, re.IGNORECASE):
            await self._handle_review_feedback(bundle_id, "noisy", actor)
        elif re.match(r"^/review-missed\s*$", body, re.IGNORECASE):
            await self._handle_review_feedback(bundle_id, "missed", actor)

    async def _handle_answer_command(self, bundle_id: str, question_id: str,
                                      answer_text: str, actor: str) -> None:
        """Handle /answer:<qid> <text> — PM answering an escalated worker question."""
        q_row = await self.db.fetch_one(
            "SELECT worker_id, bundle_id FROM worker_questions WHERE question_id = ?",
            (question_id,),
        )
        if q_row is None:
            logger.warning("Answer for unknown question %s by %s", question_id, actor)
            return

        worker_id = q_row["worker_id"]
        now = int(time.time())

        # Update question record
        await self.db.execute(
            "UPDATE worker_questions SET status = ?, answer = ?, answered_at = ?, "
            "answered_by = ? WHERE question_id = ?",
            ("answered", answer_text, now, actor, question_id),
        )
        await self.db.conn.commit()

        # Find pending intervention for this question
        int_row = await self.db.fetch_one(
            "SELECT intervention_id FROM worker_interventions "
            "WHERE worker_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (worker_id, "pending"),
        )
        intervention_id = int_row["intervention_id"] if int_row else ""

        # Resolve escalation
        from .escalation import resolve_escalation
        await resolve_escalation(
            self.db, self.handlers, self.conn_mgr, self.github_client,
            intervention_id, worker_id, bundle_id, answer_text, actor,
        )

    async def _handle_resume_command(self, bundle_id: str, worker_id: str,
                                      context: str, actor: str) -> None:
        """Handle /resume:<wid> [context] — PM resuming a paused worker."""
        # Find pending intervention
        int_row = await self.db.fetch_one(
            "SELECT intervention_id FROM worker_interventions "
            "WHERE worker_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (worker_id, "pending"),
        )
        if int_row is None:
            logger.warning("Resume for worker %s with no pending intervention by %s", worker_id, actor)
            return

        intervention_id = int_row["intervention_id"]
        response_text = context or "resumed by PM"

        # If no context provided, dismiss the intervention
        if not context:
            await self.db.execute(
                "UPDATE worker_interventions SET status = ? WHERE intervention_id = ?",
                ("dismissed", intervention_id),
            )
            await self.db.conn.commit()

        from .escalation import resolve_escalation
        await resolve_escalation(
            self.db, self.handlers, self.conn_mgr, self.github_client,
            intervention_id, worker_id, bundle_id, response_text, actor,
        )

    async def _handle_review_feedback(self, bundle_id: str, feedback_type: str,
                                        actor: str) -> None:
        """Handle /review-good, /review-noisy, /review-missed (Bundle 5.4)."""
        now = int(time.time())

        await self.db.execute(
            "INSERT INTO review_calibration (bundle_id, feedback_type, actor, created_at) "
            "VALUES (?, ?, ?, ?)",
            (bundle_id, feedback_type, actor, now),
        )
        await self.db.conn.commit()

        # Update bundle outcome with interventions_correct per resolution #7
        row = await self.db.fetch_one(
            "SELECT outcome_json FROM bundles WHERE id = ?", (bundle_id,)
        )
        outcome = {}
        if row and row["outcome_json"]:
            try:
                outcome = json.loads(row["outcome_json"])
            except json.JSONDecodeError:
                outcome = {}

        int_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM worker_interventions WHERE bundle_id = ?",
            (bundle_id,),
        )
        interventions_count = int_row["cnt"] if int_row else 0

        if feedback_type == "good":
            interventions_correct = interventions_count
        elif feedback_type == "missed":
            interventions_correct = 0
        else:  # noisy
            interventions_correct = 0

        # Store in outcome for calibration
        outcome["review_feedback"] = {
            "feedback_type": feedback_type,
            "interventions_correct": interventions_correct,
            "actor": actor,
            "created_at": now,
        }
        await self.db.execute(
            "UPDATE bundles SET outcome_json = ? WHERE id = ?",
            (json.dumps(outcome), bundle_id),
        )
        await self.db.conn.commit()

        logger.info("Review feedback '%s' recorded for bundle %s by %s",
                    feedback_type, bundle_id, actor)

        # Post acknowledgement
        if self.github_client:
            bundle_row = await self.db.fetch_one(
                "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
            )
            if bundle_row and bundle_row["github_issue_number"]:
                await self.github_client.post_comment(
                    bundle_row["github_issue_number"],
                    f"@{actor.split(':',1)[-1]} Thanks for the review quality feedback (`{feedback_type}`).",
                )

    async def _acknowledge_comment(self, bundle_id: str, user: str, command: str) -> None:
        if self.github_client is None:
            return
        row = await self.db.fetch_one(
            "SELECT github_issue_number FROM bundles WHERE id = ?", (bundle_id,)
        )
        if row is None or row["github_issue_number"] is None:
            return
        ack = f"@{user} `/ {command}` acknowledged."
        await self.github_client.post_comment(row["github_issue_number"], ack)

    # ── Connection dispatch ────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Route a new connection based on its first message.

        - "auth"  → persistent worker session
        - "studio.*" → one-shot CLI request
        """
        peername = writer.get_extra_info("peername")  # non-None for TCP connections
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line:
                return

            try:
                body = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                writer.write(
                    (json.dumps(_make_error(PARSE_ERROR, "Parse error")) + "\n").encode()
                )
                await writer.drain()
                return

            method = body.get("method", "")

            if method == "auth":
                await self._serve_worker(reader, writer, body, peername=peername)
            elif method.startswith("studio."):
                await self._serve_cli(writer, body)
            else:
                writer.write(
                    (
                        json.dumps(
                            _make_error(
                                INVALID_REQUEST,
                                "First message must be auth or studio.* method",
                                req_id=body.get("id"),
                            )
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ── Security audit helper ───────────────────────────────────────────

    async def _audit_security(self, event_type: str, subject_type: str,
                               subject_id: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, subject_type, subject_id, json.dumps(payload), int(time.time())),
        )
        await self.db.conn.commit()

    # ── Worker session ─────────────────────────────────────────────────────

    async def _serve_worker(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        auth_body: dict,
        peername: tuple | None = None,
    ) -> None:
        """Authenticate a worker, then pump RPC messages until disconnect."""
        source_ip = f"{peername[0]}:{peername[1]}" if peername else "local"
        auth_params = auth_body.get("params", {})
        token = auth_params.get("token", "")
        req_id = auth_body.get("id")

        if not token:
            writer.write(
                (
                    json.dumps(
                        _make_error(
                            INVALID_REQUEST,
                            "First message must be auth with token",
                            req_id=req_id,
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            return

        row = await self.db.fetch_one(
            "SELECT id, bundle_id, node_id, token, token_expires_at, manifest_json FROM workers WHERE token = ?",
            (token,),
        )
        if row is None:
            await self._audit_security("worker_auth_failure", "worker", "",
                                      {"reason": "invalid_token", "token_prefix": token[:8],
                                       "source_ip": source_ip})
            writer.write(
                (
                    json.dumps(
                        _make_error(
                            CAPABILITY_DENIED,
                            "Invalid or expired worker token",
                            req_id=req_id,
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            return

        # Check token expiry
        token_expires_at = row["token_expires_at"]
        if token_expires_at is not None and int(time.time()) > token_expires_at:
            await self._audit_security("worker_auth_failure", "worker", row["id"],
                                      {"reason": "token_expired", "expires_at": token_expires_at,
                                       "source_ip": source_ip})
            writer.write(
                (
                    json.dumps(
                        _make_error(
                            CAPABILITY_DENIED,
                            "Worker token expired",
                            req_id=req_id,
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            return

        worker_id = row["id"]
        bundle_id = row["bundle_id"]
        node_id = row["node_id"]

        # For TCP connections, validate mTLS client cert CN matches worker_id
        if peername:
            ssl_obj = writer.get_extra_info("ssl_object")
            if ssl_obj is None:
                await self._audit_security("worker_auth_failure", "worker", worker_id,
                                          {"reason": "no_tls_for_tcp", "source_ip": source_ip})
                writer.write(
                    (json.dumps(_make_error(CAPABILITY_DENIED, "mTLS required for TCP connections", req_id=req_id)) + "\n").encode()
                )
                await writer.drain()
                return

            peer_cert = ssl_obj.getpeercert(binary_form=False)
            if peer_cert is None:
                await self._audit_security("worker_auth_failure", "worker", worker_id,
                                          {"reason": "no_client_cert", "source_ip": source_ip})
                writer.write(
                    (json.dumps(_make_error(CAPABILITY_DENIED, "Client certificate required", req_id=req_id)) + "\n").encode()
                )
                await writer.drain()
                return

            # Extract CN from peer cert subject
            cert_cn = ""
            for field in peer_cert.get("subject", ()):
                for attr in field:
                    if attr[0] == "commonName":
                        cert_cn = attr[1]
                        break

            if cert_cn != worker_id:
                await self._audit_security("worker_auth_failure", "worker", worker_id,
                                          {"reason": "cert_cn_mismatch", "cert_cn": cert_cn, "source_ip": source_ip})
                writer.write(
                    (json.dumps(_make_error(CAPABILITY_DENIED, "Certificate CN does not match worker identity", req_id=req_id)) + "\n").encode()
                )
                await writer.drain()
                return

            logger.info("mTLS CN verified: %s", cert_cn)

        rpc_methods: list[str] = ["worker.*"]
        if row["manifest_json"]:
            try:
                mf = json.loads(row["manifest_json"])
                rpc_methods = mf.get("grants", {}).get("rpc", {}).get("methods", ["worker.*"])
            except Exception:
                pass

        binding = WorkerBinding(
            worker_id=worker_id,
            bundle_id=bundle_id,
            node_id=node_id,
            rpc_methods=rpc_methods,
            reader=reader,
            writer=writer,
        )

        self.conn_mgr._bindings[f"{bundle_id}:{node_id}"] = binding
        self.conn_mgr._by_worker_id[worker_id] = binding

        writer.write(
            (
                json.dumps(
                    _make_result({"bound": True, "worker_id": worker_id}, req_id)
                )
                + "\n"
            ).encode()
        )
        await writer.drain()

        # Log successful auth with source IP for TCP connections
        if peername:
            await self._audit_security(
                "worker_auth_success", "worker", worker_id,
                {"bundle_id": bundle_id, "node_id": node_id, "source_ip": source_ip},
            )
            logger.info("Worker %s authenticated from %s (bundle %s, node %s)",
                        worker_id, source_ip, bundle_id, node_id)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self.dispatcher.dispatch(binding, line)
                if response is not None:
                    writer.write(response)
                    await writer.drain()
        except Exception:
            pass
        finally:
            self.conn_mgr._bindings.pop(f"{bundle_id}:{node_id}", None)
            self.conn_mgr._by_worker_id.pop(worker_id, None)

    # ── CLI request ────────────────────────────────────────────────────────

    async def _serve_cli(self, writer: asyncio.StreamWriter, body: dict) -> None:
        """Handle a one-shot studio.* JSON-RPC request."""
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        handler = _CLI_HANDLERS.get(method)
        if handler is None:
            resp = _make_error(METHOD_NOT_FOUND, f"Method not found: {method}", req_id=req_id)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            return

        try:
            result = await handler(self, params)
            resp = _make_result(result if result is not None else {}, req_id)
        except Exception as exc:
            resp = _make_error(INTERNAL_ERROR, str(exc), req_id=req_id)

        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()


# ── CLI handler implementations ────────────────────────────────────────────────

async def _cli_submit(app: Orchestrator, params: dict) -> dict:
    submission = params.get("submission", {})
    bundle_input = submission.get("bundle_input", {})
    task_dag = submission.get("task_dag", {})

    from ulid import ULID
    bundle_id = str(ULID())

    # ── Bundle-input-only path: no pre-built DAG, spawn bundler worker ──
    if not task_dag or not task_dag.get("nodes"):
        await app.sm.transition_1_submit_idea(bundle_id, bundle_input)
        await _spawn_bundler(app, bundle_id, bundle_input)
        return {
            "bundle_id": bundle_id,
            "mode": "planning",
            "message": "Bundle created in PROPOSED state, bundler agent is planning",
        }

    # ── Existing kernel-direct path: pre-built DAG present ──
    repo = bundle_input.get("target_repo", "control-plane")

    dag_nodes = []
    for n in task_dag.get("nodes", []):
        dag_nodes.append({
            "node_id": n.get("id", "task-1"),
            "kind": n.get("kind", "worker"),
            "spec": n.get("spec", {}),
        })

    dag_edges = []
    for e in task_dag.get("edges", []):
        dag_edges.append({
            "from_node_id": e.get("from", ""),
            "to_node_id": e.get("to", ""),
            "condition": e.get("condition", {"kind": "on_success"}),
        })

    await app.sm.transition_1_submit(bundle_id, repo, submission, dag_nodes, dag_edges)
    return {"bundle_id": bundle_id}


async def _synthesize_test_bundler_proposal(app: Orchestrator, bundle_id: str, bundle_input: dict) -> None:
    """In test mode, produce a synthetic bundler proposal without calling an LLM."""
    now = int(time.time())
    idea = bundle_input.get("idea", "unspecified")

    worker_id = f"bundler_{bundle_id}"
    await app.db.execute(
        "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (worker_id, bundle_id, "bundler", "test-mode-noop", "{}", "complete", now),
    )
    await app.db.conn.commit()

    # Synthetic bundler output (matches the real bundler's JSON output format)
    bundler_output = {
        "complexity_score": 2,
        "risk_score": 1,
        "complexity_factors": {
            "loc": 2, "components_touched": 1, "worker_tasks": 1,
            "cross_component_coordination": 0, "new_abstractions": 0,
        },
        "risk_factors": {
            "security_sensitive_paths": 0, "data_handling_paths": 0,
            "public_interfaces": 1, "reversibility": 1,
            "production_proximity": 0, "net_new_dependencies": 0,
        },
        "estimated_loc": 50,
        "estimated_duration_seconds": 60,
        "estimated_worker_count": 1,
        "estimated_tokens": 500,
        "target": "control-plane",
        "target_rationale": "Test mode synthetic proposal",
        "concerns": ["Test mode — no real planning performed"],
        "requirements_summary": f"Implement: {idea}",
        "rfc_summary": "Minimal Flask app with single route",
        "implementation_plan": "Create app.py with Flask and single GET / route returning JSON",
        "task_dag": {
            "nodes": [{
                "id": "implement-idea",
                "kind": "worker",
                "spec": {
                    "objective": idea,
                    "success_criteria": [{"kind": "tests_pass"}],
                },
            }],
            "edges": [],
        },
    }

    # Simulate the same flow as a real bundler worker calling final_report
    try:
        await app._on_bundler_report(bundle_id, bundler_output)
        logger.info("Test mode: synthesized bundler proposal for bundle %s", bundle_id)
    except Exception:
        logger.exception("Test mode: bundler synthesis failed for bundle %s", bundle_id)
        raise


def _drain_subprocess(process: asyncio.subprocess.Process, name: str) -> None:
    """Create background tasks to drain subprocess stdout/stderr pipes to the logger.

    Prevents pipe buffer deadlock. All important communication goes through the
    Unix socket — these pipes are only for debugging output.
    """
    async def _read_stream(stream: asyncio.StreamReader | None, stream_name: str) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                logger.info("[%s %s] %s", name, stream_name, text)

    asyncio.create_task(_read_stream(process.stdout, "stdout"))
    asyncio.create_task(_read_stream(process.stderr, "stderr"))


def _get_artifact_type_hint(bundle_input: dict) -> str:
    """Detect artifact type from idea for bundler task spec hint."""
    idea = bundle_input.get("idea", "")
    if not idea:
        return ""
    from .artifacts import detect_artifact_type_from_idea
    t = detect_artifact_type_from_idea(idea)
    return t.value


async def _spawn_bundler(app: Orchestrator, bundle_id: str, bundle_input: dict) -> None:
    """Spawn a bundler worker as a standalone process (not part of a DAG)."""
    if os.environ.get("STUDIO_TEST_MODE") == "1":
        # In test mode, produce a synthetic proposal immediately
        await _synthesize_test_bundler_proposal(app, bundle_id, bundle_input)
        return

    worker_id = f"bundler_{bundle_id}"
    token = secrets.token_hex(32)
    now = int(time.time())

    manifest_json = json.dumps({
        "schema_version": "1.0",
        "subject": {"kind": "bundle", "id": bundle_id},
        "grants": {
            "filesystem": {"reads": [], "writes": []},
            "network": {"egress": ["*:443"]},
            "process": {"exec": []},
            "rpc": {"methods": ["worker.*"]},
            "resources": {},
        },
        "metadata": {"rationale": "bundler worker needs outbound HTTPS for Ollama Cloud API"},
    })

    await app.db.execute(
        "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (worker_id, bundle_id, "bundler", token, manifest_json, "pending", now),
    )
    await app.db.conn.commit()

    worker_env = {
        **os.environ,
        "STUDIO_WORKER_TOKEN": token,
        "STUDIO_SOCKET_PATH": app.settings.orchestrator.socket_path,
        "STUDIO_WORKER_ID": worker_id,
        "STUDIO_BUNDLE_ID": bundle_id,
        "STUDIO_NODE_ID": "bundler",
        "STUDIO_TASK_SPEC": json.dumps({
            "idea": bundle_input.get("idea", ""),
            "bundle_input": bundle_input,
            "artifact_type_hint": _get_artifact_type_hint(bundle_input),
        }),
        "OLLAMA_CLOUD_BASE_URL": app.settings.ollama_cloud.base_url,
    }

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "studio.workers.bundler",
        env=worker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _drain_subprocess(process, worker_id)

    # Track for cleanup
    if not hasattr(app, '_bundler_processes'):
        app._bundler_processes = {}
    app._bundler_processes[worker_id] = process
    logger.info("Bundler worker spawned: %s for bundle %s", worker_id, bundle_id)


async def _spawn_qa_worker(app: Orchestrator, bundle_id: str) -> None:
    """Spawn a post-execution QA verification worker as a standalone process."""
    if os.environ.get("STUDIO_TEST_MODE") == "1":
        await app._on_qa_pass(bundle_id, {"test_mode": True})
        return

    worker_id = f"qa_{bundle_id}"
    token = secrets.token_hex(32)
    now = int(time.time())

    # Fetch the verification plan artifact from bundle proposal
    row = await app.db.fetch_one(
        "SELECT proposal_json FROM bundles WHERE id = ?", (bundle_id,)
    )
    verification_plan = {}
    bundle_input = {}
    if row:
        proposal = json.loads(row["proposal_json"] or "{}")
        bundle_input = proposal.get("bundle_input", {})
        # The verification plan from pre-execution review is inlined in proposal
        bundler_proposal = proposal.get("proposal", {})
        verification_plan = bundler_proposal.get("verification_plan", {})

    manifest_json = json.dumps({
        "schema_version": "1.0",
        "subject": {"kind": "bundle", "id": bundle_id},
        "grants": {
            "filesystem": {"reads": [], "writes": []},
            "network": {"egress": ["*:443"]},
            "process": {"exec": []},
            "rpc": {"methods": ["worker.*"]},
            "resources": {},
        },
        "metadata": {"rationale": "QA verification worker needs outbound HTTPS for Ollama Cloud API"},
    })

    await app.db.execute(
        "INSERT INTO workers (id, bundle_id, node_id, token, manifest_json, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (worker_id, bundle_id, "qa-verification", token, manifest_json, "pending", now),
    )
    await app.db.conn.commit()

    worker_env = {
        **os.environ,
        "STUDIO_WORKER_TOKEN": token,
        "STUDIO_SOCKET_PATH": app.settings.orchestrator.socket_path,
        "STUDIO_WORKER_ID": worker_id,
        "STUDIO_BUNDLE_ID": bundle_id,
        "STUDIO_NODE_ID": "qa-verification",
        "STUDIO_TASK_SPEC": json.dumps({
            "bundle_id": bundle_id,
            "ollama_base_url": app.settings.ollama_cloud.base_url,
            "model": "minimax-m2.7:cloud",
            "verification_plan": verification_plan,
            "bundle_branch": f"bundle/{bundle_id}",
            "repo_path": os.getcwd(),
            "auto_pass": True,
        }),
        "OLLAMA_CLOUD_BASE_URL": app.settings.ollama_cloud.base_url,
    }

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "studio.workers.qa",
        env=worker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _drain_subprocess(process, worker_id)

    if not hasattr(app, '_qa_processes'):
        app._qa_processes = {}
    app._qa_processes[worker_id] = process
    logger.info("QA worker spawned: %s for bundle %s", worker_id, bundle_id)


async def _cli_calibration_report(app: Orchestrator, params: dict) -> dict:
    """Print calibration report from memory/calibration/."""
    memory_root = app.settings.orchestrator.memory_root
    cal_path = Path(memory_root) / "calibration" / "scoring-outcomes.jsonl"

    entries: list[dict] = []
    if cal_path.exists():
        for line in cal_path.read_text().strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not entries:
        return {"entries": [], "message": "No calibration data recorded yet."}

    # Summarize: per-axis avg divergence
    total = len(entries)
    diverged_count = sum(1 for e in entries if e.get("divergence_threshold_exceeded"))
    recent = []
    for e in entries[-10:]:
        recent.append({
            "bundle_id": e.get("bundle_id", "unknown"),
            "estimated": {
                "loc": e.get("estimated_loc"),
                "duration_seconds": e.get("estimated_duration_seconds"),
                "workers": e.get("estimated_worker_count"),
                "tokens": e.get("estimated_tokens"),
            },
            "actual": {
                "loc": e.get("actual_loc"),
                "duration_seconds": e.get("actual_duration_seconds"),
                "workers": e.get("actual_worker_count"),
                "tokens": e.get("actual_tokens"),
            },
        })

    # ── Bundle 5.4: Review quality section ─────────────────────────────────

    total_interventions = sum(e.get("interventions_count", 0) for e in entries)
    total_bundles_with_interventions = sum(1 for e in entries if e.get("interventions_count", 0) > 0)
    total_questions = sum(e.get("questions_asked", 0) for e in entries)
    total_llm_answered = sum(e.get("questions_llm_answered", 0) for e in entries)
    total_escalated = sum(e.get("questions_escalated", 0) for e in entries)
    resp_times = [e["escalation_response_time_seconds"] for e in entries
                  if e.get("escalation_response_time_seconds", 0) > 0]

    # Query review_calibration for feedback stats
    fb_rows = await app.db.fetch_all(
        "SELECT feedback_type, COUNT(*) as cnt FROM review_calibration GROUP BY feedback_type"
    )
    fb_counts = {r["feedback_type"]: r["cnt"] for r in fb_rows} if fb_rows else {}
    good_count = fb_counts.get("good", 0)
    noisy_count = fb_counts.get("noisy", 0)
    missed_count = fb_counts.get("missed", 0)
    total_feedback = good_count + noisy_count + missed_count

    review_quality = {
        "total_interventions": total_interventions,
        "total_bundles_with_interventions": total_bundles_with_interventions,
        "intervention_rate": round(total_interventions / total, 2) if total > 0 else 0,
        "llm_answer_rate": round(total_llm_answered / total_questions * 100) if total_questions > 0 else 0,
        "avg_escalation_response_minutes": round(sum(resp_times) / len(resp_times) / 60, 1) if resp_times else 0,
        "total_feedback": total_feedback,
        "good_count": good_count,
        "noisy_count": noisy_count,
        "missed_count": missed_count,
        "accuracy_rate": round(good_count / total_feedback * 100) if total_feedback > 0 else None,
        "noisy_rate": round(noisy_count / total_feedback, 2) if total_feedback > 0 else 0,
        "missed_rate": round(missed_count / total_feedback, 2) if total_feedback > 0 else 0,
    }

    # ── Bundle 6.4: Code quality metrics ─────────────────────────────────

    # Filter entries that have verification data (skip documentation / skip_verification)
    verified = [e for e in entries
                if e.get("developer_verification_attempts", 0) > 0
                and e.get("artifact_type") not in ("documentation", None)]

    first_attempt_count = sum(1 for e in verified if e.get("first_attempt_pass"))
    total_verified = len(verified)
    first_attempt_pass_rate = (
        round(first_attempt_count / total_verified * 100) if total_verified > 0 else None
    )

    avg_fix_attempts = (
        round(sum(e.get("developer_verification_attempts", 1) for e in verified) / total_verified, 1)
        if total_verified > 0 else None
    )

    # QA criterion pass rate across all entries that have it
    qa_rates = [e["qa_criterion_pass_rate"] for e in entries
                if e.get("qa_criterion_pass_rate") is not None]
    avg_qa_criterion_pass_rate = (
        round(sum(qa_rates) / len(qa_rates) * 100) if qa_rates else None
    )

    # Failure category distribution
    all_categories: dict[str, int] = {}
    for e in entries:
        for cat, cnt in e.get("failure_categories", {}).items():
            all_categories[cat] = all_categories.get(cat, 0) + cnt
    total_failures = sum(all_categories.values())
    most_common_category = None
    most_common_pct = 0
    if all_categories:
        top_cat = max(all_categories, key=lambda k: all_categories[k])
        most_common_category = top_cat
        most_common_pct = round(all_categories[top_cat] / total_failures * 100)

    # Spec clarity score: 100 - (multi_attempt / total * 100)
    spec_clarity_score = None
    if total_verified >= 5:
        multi_attempt = sum(1 for e in verified if not e.get("first_attempt_pass"))
        spec_clarity_score = round(100 - (multi_attempt / total_verified * 100))

    # Recommendations from thresholds
    recommendations: list[str] = []
    if all_categories.get("missing_dependencies", 0) / max(total_failures, 1) > 0.3:
        recommendations.append(
            '"Missing dependencies" failures suggest bundler should include '
            "explicit dependency lists in task specs"
        )
    if first_attempt_pass_rate is not None and first_attempt_pass_rate < 60:
        recommendations.append(
            f"First-attempt pass rate is {first_attempt_pass_rate}% (target: >80%). "
            "Review spec quality and acceptance criteria clarity."
        )
    if avg_fix_attempts is not None and avg_fix_attempts > 3:
        recommendations.append(
            f"Average fix attempts is {avg_fix_attempts} (target: <2.0). "
            "Review bundles with 4+ attempts for systematic issues."
        )

    code_quality = {
        "first_attempt_pass_rate": first_attempt_pass_rate,
        "avg_fix_attempts": avg_fix_attempts,
        "qa_criterion_pass_rate": avg_qa_criterion_pass_rate,
        "most_common_failure_category": most_common_category,
        "most_common_failure_pct": most_common_pct,
        "spec_clarity_score": spec_clarity_score,
        "total_verified": total_verified,
        "all_categories": all_categories,
        "recommendations": recommendations,
    }

    return {
        "total_entries": total,
        "entries_with_divergence": diverged_count,
        "recent": recent,
        "review_quality": review_quality,
        "code_quality": code_quality,
    }


async def _cli_approve(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")

    # Use the correct transition based on current state
    row = await app.db.fetch_one("SELECT state FROM bundles WHERE id = ?", (bundle_id,))
    if row is None:
        return {"error": f"Bundle {bundle_id} not found"}
    current_state = row["state"]

    if current_state == BundleState.IN_REVIEW:
        await app.sm.transition_4_approve_from_review(bundle_id, "cli")
    else:
        await app.sm.transition_1a_approve(bundle_id, "cli")

    # Transition 6: start execution
    await app.sm.transition_6_start_execution(bundle_id)
    # Phase 5: LangGraph handles execution dispatch.
    return {"approved": True}


async def _cli_reject(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    reason = params.get("reason", "rejected via CLI")
    await app.sm.transition_1b_reject(bundle_id, "cli", reason)
    return {"rejected": True}


async def _cli_list(app: Orchestrator, params: dict) -> dict:
    state = params.get("state")
    tier = params.get("tier")
    where = []
    vals: list[str] = []

    if state:
        where.append("state = ?")
        vals.append(state)
    else:
        where.append("state NOT IN (?, ?, ?, ?, ?)")
        vals.extend(("complete", "failed", "rejected", "parked", "aborted"))

    if tier:
        where.append("tier = ?")
        vals.append(tier)

    query = f"SELECT id, state, created_at, proposal_json, tier, repo FROM bundles WHERE {' AND '.join(where)}"
    rows = await app.db.fetch_all(query, tuple(vals))

    bundles = []
    for r in rows:
        secs = app.sm.now() - (r["created_at"] or 0)
        age = _format_age(secs)
        proposal = json.loads(r["proposal_json"] or "{}")
        p = proposal.get("proposal", {})
        bundles.append({
            "id": r["id"],
            "state": r["state"],
            "tier": r["tier"],
            "age": age,
            "idea": proposal.get("bundle_input", {}).get("idea", ""),
            "complexity_score": p.get("complexity_score"),
            "risk_score": p.get("risk_score"),
            "repo": r["repo"],
        })
    return {"bundles": bundles}


async def _cli_show(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    row = await app.db.fetch_one(
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    )
    if row is None:
        raise ValueError(f"Bundle {bundle_id} not found")

    proposal = json.loads(row["proposal_json"] or "{}")
    nodes = await app.db.fetch_all(
        "SELECT * FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
    )
    edges = await app.db.fetch_all(
        "SELECT * FROM dag_edges WHERE bundle_id = ?", (bundle_id,)
    )
    audit_entries = await app.db.fetch_all(
        "SELECT * FROM audit_log WHERE subject_id = ? ORDER BY created_at DESC LIMIT 5",
        (bundle_id,)
    )
    artifacts = await app.db.fetch_all(
        "SELECT namespace, name, version, content_type, size_bytes, hash, published_at "
        "FROM artifact_metadata WHERE bundle_id = ?",
        (bundle_id,)
    )

    return {
        "bundle": dict(row),
        "proposal": proposal,
        "nodes": [dict(n) for n in nodes],
        "edges": [dict(e) for e in edges],
        "audit_entries": [dict(a) for a in audit_entries],
        "artifacts": [dict(a) for a in artifacts],
    }


async def _cli_show_worker(app: Orchestrator, params: dict) -> dict:
    worker_id = params.get("worker_id", "")
    row = await app.db.fetch_one(
        "SELECT * FROM workers WHERE id = ?",
        (worker_id,),
    )
    if row is None:
        raise ValueError(f"Worker {worker_id} not found")

    node = await app.db.fetch_one(
        "SELECT * FROM dag_nodes WHERE id = ?",
        (f"{row['bundle_id']}:{row['node_id']}",),
    )

    cap_checks = await app.db.fetch_all(
        "SELECT result FROM capability_checks WHERE worker_id = ?",
        (worker_id,),
    )
    allowed = sum(1 for c in cap_checks if c["result"] == "allowed")
    denied = sum(1 for c in cap_checks if c["result"] == "denied")

    return {
        "worker": dict(row),
        "node": dict(node) if node else None,
        "cap_checks": {"allowed": allowed, "denied": denied},
    }


async def _cli_kill(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    workers = await app.db.fetch_all(
        "SELECT id FROM workers WHERE bundle_id = ? AND state = ?",
        (bundle_id, "running"),
    )
    # Phase 5: LangGraph manages worker lifecycle. Mark as failed via state machine.
    for w in workers:
        await app.db.execute(
            "UPDATE workers SET state = ?, exit_reason = ?, ended_at = ? WHERE id = ?",
            ("failed", "killed via CLI", int(time.time()), w["id"]),
        )

    await app.sm.transition_25_fail_execution(bundle_id, "killed via CLI")
    return {"workers_killed": len(workers)}


async def _cli_status(app: Orchestrator, params: dict) -> dict:
    try:
        uptime = time.time() - app.ops._start_time if hasattr(app.ops, '_start_time') else 0
        workers = await app.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM workers WHERE state = 'running'"
        )
        worker_count = workers["cnt"] if workers else 0
        ready_nodes = await app.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM dag_nodes WHERE state = 'ready'"
        )
        queue_depth = ready_nodes["cnt"] if ready_nodes else 0
    except Exception as e:
        return {"error": f"Database error: {e}", "db_ok": False}

    listeners = [f"unix:{app.settings.orchestrator.socket_path}"]
    if app.settings.remote_workers.enabled:
        listeners.append(f"tcp:{app.settings.remote_workers.listen_addr}")

    result = {
        "uptime": uptime,
        "worker_count": worker_count,
        "queue_depth": queue_depth,
        "listeners": listeners,
    }
    if app._code_stale:
        result["warning"] = "Code updated since last restart. Restart required."
    if hasattr(app, '_bwrap_available'):
        result["sandbox"] = "bubblewrap (active)" if app._bwrap_available else "none (WARNING: workers running unsandboxed)"
    return result


async def _cli_version(app: Orchestrator, params: dict) -> dict:
    installed_hash = Orchestrator._compute_code_hash()
    return {
        "installed_code_hash": installed_hash[:16],
        "running_code_hash": app._startup_code_hash[:16],
        "running_stale": installed_hash != app._startup_code_hash,
        "running": True,
    }


async def _cli_recall(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    if not bundle_id:
        return {"error": "bundle_id is required"}

    return {
        "eligible": False,
        "reason": (
            f"studio recall is not yet implemented. "
            f"To revert a bundle, submit a new bundle with idea: "
            f"'Revert bundle {bundle_id}: <original idea>'"
        ),
    }


async def _cli_health(app: Orchestrator, params: dict) -> dict:
    snap = await app.ops.get_health()
    return {
        "orchestrator_ok": snap.orchestrator_ok,
        "db_ok": snap.db_ok,
        "uptime_seconds": snap.uptime_seconds,
        "total_bundles": snap.total_bundles,
        "active_bundles": snap.active_bundles,
        "stalled_bundles": snap.stalled_bundles,
        "by_state": snap.by_state,
        "by_tier": snap.by_tier,
        "calibration": snap.calibration,
        "recent_errors": snap.recent_errors,
    }


async def _cli_audit(app: Orchestrator, params: dict) -> dict:
    """Report capability grants, usage, and over-granting for a bundle (Bundle 3.4)."""
    bundle_id = params.get("bundle_id", "")
    if not bundle_id:
        return {"error": "bundle_id is required"}

    # Fetch bundle proposal with capability manifest
    bundle_row = await app.db.fetch_one(
        "SELECT proposal_json, state FROM bundles WHERE id = ?", (bundle_id,)
    )
    if bundle_row is None:
        return {"error": f"Bundle {bundle_id} not found"}

    proposal = json.loads(bundle_row["proposal_json"] or "{}")
    cap_raw = proposal.get("capability_manifest", {})

    # Collect granted capabilities
    grants_summary: dict[str, list[str]] = {}
    if cap_raw:
        grants = cap_raw.get("grants", {})
        fs = grants.get("filesystem", {})
        if fs.get("reads"):
            grants_summary["filesystem.reads"] = [r["path"] for r in fs["reads"]]
        if fs.get("writes"):
            grants_summary["filesystem.writes"] = [w["path"] for w in fs["writes"]]
        net = grants.get("network", {})
        if net.get("egress"):
            grants_summary["network.egress"] = [
                f"{e.get('destination','')}:{e.get('ports','*')}" for e in net["egress"]
            ]
        proc = grants.get("process", {})
        if proc.get("exec"):
            grants_summary["process.exec"] = [e["binary"] for e in proc["exec"]]
        secrets_g = grants.get("secrets", [])
        if secrets_g:
            grants_summary["secrets"] = [s["name"] for s in secrets_g]
        rpc_g = grants.get("rpc", {})
        if rpc_g.get("methods"):
            grants_summary["rpc.methods"] = rpc_g["methods"]

    # Fetch capability checks for this bundle
    checks = await app.db.fetch_all(
        "SELECT requested_op, result FROM capability_checks WHERE bundle_id = ?",
        (bundle_id,),
    )
    used_ops = set()
    denied_ops: list[str] = []
    for c in checks:
        if c["result"] == "allowed":
            used_ops.add(c["requested_op"])
        else:
            denied_ops.append(c["requested_op"])

    # Fetch secret accesses for this bundle
    secret_rows = await app.db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE event_type = ? AND subject_type = ? "
        "AND json_extract(payload_json, '$.bundle_id') = ?",
        ("secret_access", "secret", bundle_id),
    )
    used_secrets: list[str] = []
    for sr in secret_rows:
        try:
            p = json.loads(sr["payload_json"] or "{}")
            if p.get("secret_name"):
                used_secrets.append(p["secret_name"])
        except Exception:
            pass

    # Classify grants: used vs unused
    used_grants: list[str] = []
    unused_grants: list[str] = []
    for category, items in grants_summary.items():
        for item in items:
            label = f"{category}:{item}"
            if category == "secrets" and item in used_secrets:
                used_grants.append(label)
            elif any(item in op or op in item for op in used_ops):
                used_grants.append(label)
            else:
                unused_grants.append(label)

    # Over-granted: grants that were never used
    over_granted = unused_grants if unused_grants else []

    return {
        "bundle_id": bundle_id,
        "state": bundle_row["state"],
        "granted": grants_summary,
        "used_grants": used_grants,
        "unused_grants": unused_grants,
        "over_granted": over_granted,
        "denied_operations": denied_ops,
        "used_secrets": used_secrets,
    }


async def _cli_rotate_secret(app: Orchestrator, params: dict) -> dict:
    """Rotate a secret: invalidate old, provision new, audit log (Bundle 3.4)."""
    name = params.get("name", "")
    if not name:
        return {"error": "name is required"}

    if app._secret_store is None:
        return {"error": "Secret store not configured"}

    # Check which workers previously fetched this secret
    old_access_rows = await app.db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE event_type = ? AND subject_id = ?",
        ("secret_access", name),
    )
    affected_workers: list[str] = []
    for row in old_access_rows:
        try:
            p = json.loads(row["payload_json"] or "{}")
            wid = p.get("worker_id", "")
            if wid and wid not in affected_workers:
                affected_workers.append(wid)
        except Exception:
            pass

    new_value, error = app._secret_store.rotate(name)
    if error:
        return {"error": error}

    # Audit log the rotation
    now = int(time.time())
    await app.db.execute(
        "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("secret_rotated", "secret", name,
         json.dumps({"affected_workers": affected_workers, "rotated_at": now}),
         now),
    )
    await app.db.conn.commit()

    return {
        "secret": name,
        "rotated": True,
        "affected_workers": affected_workers,
        "message": f"Secret '{name}' rotated. {len(affected_workers)} worker(s) previously accessed old value.",
    }


async def _cli_fleet_status(app: Orchestrator, params: dict) -> dict:
    """Show status of all fleet hosts (Bundle 4.2)."""
    ssh_runner = app._ssh_runner
    if ssh_runner is None:
        return {"error": "Remote fleet is not enabled. Set remote_fleet.enabled=true in settings.json."}
    statuses = await ssh_runner.ping_hosts()
    hosts_detail = []
    for host in app.settings.remote_fleet.hosts:
        sem = ssh_runner._host_semaphores.get(host.name)
        capacity_used = host.max_concurrent_workers
        if sem:
            # Approximate active count from semaphore
            capacity_used = host.max_concurrent_workers - (sem._value if hasattr(sem, '_value') else 0)
        hosts_detail.append({
            "name": host.name,
            "addr": host.addr,
            "status": statuses.get(host.name, "unknown"),
            "active_workers": capacity_used,
            "max_workers": host.max_concurrent_workers,
            "last_ping": ssh_runner._host_last_ping.get(host.name, 0),
        })
    return {"hosts": hosts_detail}


async def _cli_fleet_add(app: Orchestrator, params: dict) -> dict:
    """Add a host to the fleet registry (Bundle 4.2)."""
    name = params.get("name", "")
    addr = params.get("addr", "")
    if not name or not addr:
        return {"error": "name and addr are required"}

    from .models import FleetHost
    new_host = FleetHost(
        name=name, addr=addr,
        ssh_user=params.get("ssh_user", "studio"),
        ssh_key_path=params.get("ssh_key_path", ""),
        capabilities=params.get("capabilities", []),
        max_concurrent_workers=params.get("max_concurrent_workers", 4),
        arch=params.get("arch", "x86_64"),
        worktree_mode=params.get("worktree_mode", "clone"),
    )

    # Check for duplicate name
    for h in app.settings.remote_fleet.hosts:
        if h.name == name:
            return {"error": f"Host '{name}' already exists. Use fleet-remove first."}

    app.settings.remote_fleet.hosts.append(new_host)

    # Add semaphore and health tracking if runner is active
    if app._ssh_runner is not None:
        app._ssh_runner._host_semaphores[name] = asyncio.Semaphore(new_host.max_concurrent_workers)
        app._ssh_runner._host_health[name] = True

    # Persist to settings.json
    _persist_fleet_settings(app.settings)

    return {"added": True, "name": name, "addr": addr}


async def _cli_fleet_remove(app: Orchestrator, params: dict) -> dict:
    """Remove a host from the fleet registry (Bundle 4.2)."""
    name = params.get("name", "")
    if not name:
        return {"error": "name is required"}

    # Remove from settings
    before = len(app.settings.remote_fleet.hosts)
    app.settings.remote_fleet.hosts = [
        h for h in app.settings.remote_fleet.hosts if h.name != name
    ]
    if len(app.settings.remote_fleet.hosts) == before:
        return {"error": f"Host '{name}' not found in fleet registry."}

    # Remove from runner tracking
    if app._ssh_runner is not None:
        app._ssh_runner._host_semaphores.pop(name, None)
        app._ssh_runner._host_health.pop(name, None)
        app._ssh_runner._host_last_ping.pop(name, None)

    # Persist to settings.json
    _persist_fleet_settings(app.settings)

    return {"removed": True, "name": name}


async def _cli_k8s_status(app: Orchestrator, params: dict) -> dict:
    """Show active Kubernetes Jobs in studio-workers namespace (Bundle 4.3)."""
    if app._k8s_runner is None:
        return {"error": "K8s runner is not enabled. Set k8s_runner.enabled=true in settings.json."}
    try:
        api_client = await app._k8s_runner._ensure_client()
        batch_v1 = api_client.BatchV1Api
        jobs = await batch_v1.list_namespaced_job(
            namespace=app.settings.k8s_runner.namespace,
            label_selector="studio/worker-id",
        )
        job_list = []
        for job in jobs.items:
            job_list.append({
                "name": job.metadata.name,
                "namespace": job.metadata.namespace,
                "bundle_id": job.metadata.labels.get("studio/bundle-id", ""),
                "worker_id": job.metadata.labels.get("studio/worker-id", ""),
                "active": job.status.active or 0,
                "succeeded": job.status.succeeded or 0,
                "failed": job.status.failed or 0,
                "age": app.sm.now() - int(job.metadata.creation_timestamp.timestamp()) if job.metadata.creation_timestamp else 0,
            })
        return {"jobs": job_list, "namespace": app.settings.k8s_runner.namespace}
    except Exception as exc:
        return {"error": f"Failed to list k8s Jobs: {exc}"}


async def _cli_docker_status(app: Orchestrator, params: dict) -> dict:
    """Show running Docker worker containers and resource usage (Bundle 4.5)."""
    if app._docker_runner is None:
        return {"error": "Docker runner is not enabled. Set docker_runner.enabled=true in settings.json."}
    import json as _json
    try:
        client = app._docker_runner._get_client()
        containers = await asyncio.to_thread(
            client.containers.list,
            filters={"label": "studio/runner=docker"},
        )
        worker_list = []
        for c in containers:
            labels = c.labels or {}
            worker_list.append({
                "container_id": c.short_id,
                "name": c.name,
                "bundle_id": labels.get("studio/bundle-id", ""),
                "worker_id": labels.get("studio/worker-id", ""),
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else "",
                "created": c.attrs.get("Created", ""),
            })
        return {"containers": worker_list, "count": len(worker_list)}
    except Exception as exc:
        return {"error": f"Failed to list Docker containers: {exc}"}


async def _cli_docker_images(app: Orchestrator, params: dict) -> dict:
    """Show worker and proxy Docker images (Bundle 4.5)."""
    if app._docker_runner is None:
        return {"error": "Docker runner is not enabled. Set docker_runner.enabled=true in settings.json."}
    try:
        client = app._docker_runner._get_client()
        images = await asyncio.to_thread(
            client.images.list,
            name=app.settings.docker_runner.worker_image.split(":")[0],
        )
        image_list = []
        for img in images:
            tags = img.tags if img.tags else ["<none>"]
            image_list.append({
                "id": img.short_id,
                "tags": tags,
                "created": img.attrs.get("Created", ""),
                "size": img.attrs.get("Size", 0),
            })
        return {"images": image_list, "expected_worker": app.settings.docker_runner.worker_image,
                "expected_proxy": app.settings.docker_runner.proxy_image}
    except Exception as exc:
        return {"error": f"Failed to list Docker images: {exc}"}


async def _cli_vm_pool_resize(app: Orchestrator, params: dict) -> dict:
    """Resize the Firecracker VM pool at runtime (Phase 7.3)."""
    new_size = params.get("size")
    if new_size is None or not isinstance(new_size, int) or new_size < 0:
        return {"error": "size must be a non-negative integer"}

    if not hasattr(app, "_vm_pool") or app._vm_pool is None:
        return {"error": "VM pool is not running. Enable firecracker.enabled in settings."}

    try:
        result = await app._vm_pool.resize(new_size)
    except Exception as exc:
        return {"error": str(exc)}

    # Persist new size to settings
    try:
        app.settings.firecracker.pool_size = new_size
    except Exception:
        pass

    return result


async def _cli_check_rootfs(app: Orchestrator, params: dict) -> dict:
    """Check if the worker rootfs is out of date (Phase 7.3)."""
    from studio.orchestrator.firecracker import check_rootfs_freshness

    rootfs_path = params.get("rootfs_path", app.settings.firecracker.rootfs_path)
    return check_rootfs_freshness(rootfs_path)


async def _cli_vm_status(app: Orchestrator, params: dict) -> dict:
    """Show Firecracker VM pool status (Phase 7.1)."""
    from studio.orchestrator.firecracker import check_firecracker_available

    fc_settings = app.settings.firecracker
    check = check_firecracker_available(
        kernel_path=fc_settings.kernel_path,
        firecracker_binary="firecracker",
    )

    result: dict[str, Any] = {
        "available": check["available"],
        "reason": check.get("reason", ""),
        "kvm": check.get("kvm", False),
        "kernel": check.get("kernel", False),
        "binary": check.get("binary", False),
        "enabled": fc_settings.enabled,
        "pool_size": fc_settings.pool_size,
        "sandbox": "firecracker (active)" if (fc_settings.enabled and check["available"]) else "bubblewrap",
    }

    if hasattr(app, "_vm_pool") and app._vm_pool is not None:
        result["available_vms"] = app._vm_pool._available.qsize()
        result["total_spawned"] = len(app._vm_pool._all_vms)
    else:
        result["available_vms"] = 0
        result["total_spawned"] = 0

    return result


async def _cli_review_worker(app: Orchestrator, params: dict) -> dict:
    """PM-initiated review of a specific worker (Bundle 5.2)."""
    worker_id = params.get("worker_id", "")
    if not worker_id:
        return {"error": "worker_id is required"}

    if app._review_scheduler is None:
        return {"error": "Review scheduler is not enabled. Set review.enabled=true in settings.json."}

    row = await app.db.fetch_one(
        "SELECT bundle_id, node_id FROM workers WHERE id = ? AND state = ?",
        (worker_id, "running"),
    )
    if row is None:
        return {"error": f"Worker {worker_id} not found or not running"}

    verdict = await app._review_scheduler.trigger_review(
        worker_id, row["bundle_id"], row["node_id"], "pm_initiated",
    )
    return {"reviewed": True, "worker_id": worker_id, "verdict": verdict}


async def _cli_answer_question(app: Orchestrator, params: dict) -> dict:
    """Answer a pending worker question (Bundle 5.3)."""
    question_id = params.get("question_id", "")
    answer = params.get("answer", "")
    if not question_id or not answer:
        return {"error": "question_id and answer are required"}

    q_row = await app.db.fetch_one(
        "SELECT worker_id, bundle_id FROM worker_questions WHERE question_id = ? AND status = ?",
        (question_id, "escalated"),
    )
    if q_row is None:
        return {"error": f"Question {question_id} not found or not escalated"}

    await app._handle_answer_command(q_row["bundle_id"], question_id, answer, "cli")
    return {"answered": True, "question_id": question_id}


async def _cli_resume_worker(app: Orchestrator, params: dict) -> dict:
    """Resume a paused worker (Bundle 5.3)."""
    worker_id = params.get("worker_id", "")
    context = params.get("context", "")
    if not worker_id:
        return {"error": "worker_id is required"}

    row = await app.db.fetch_one(
        "SELECT bundle_id FROM workers WHERE id = ? AND state = ?",
        (worker_id, "paused"),
    )
    if row is None:
        return {"error": f"Worker {worker_id} not found or not paused"}

    await app._handle_resume_command(row["bundle_id"], worker_id, context, "cli")
    return {"resumed": True, "worker_id": worker_id}


async def _cli_pending_escalations(app: Orchestrator, params: dict) -> dict:
    """List all pending PM escalations (Bundle 5.3)."""
    rows = await app.db.fetch_all(
        "SELECT wi.*, w.bundle_id as w_bundle_id, w.node_id "
        "FROM worker_interventions wi "
        "JOIN workers w ON wi.worker_id = w.id "
        "WHERE wi.status = ? "
        "ORDER BY wi.created_at DESC",
        ("pending",),
    )
    escalations = []
    for r in rows:
        rd = dict(r)
        escalations.append({
            "intervention_id": rd["intervention_id"],
            "worker_id": rd["worker_id"],
            "bundle_id": rd["w_bundle_id"],
            "node_id": rd.get("node_id", ""),
            "type": rd["type"],
            "content": rd["content"][:200],
            "triggered_by": rd["triggered_by"],
            "created_at": rd["created_at"],
        })
    return {"escalations": escalations, "count": len(escalations)}


def _persist_fleet_settings(settings: Settings) -> None:
    """Write current fleet settings back to settings.json."""
    import json as _json
    config_path = os.environ.get(
        "STUDIO_CONFIG_PATH",
        os.path.join(os.path.dirname(settings.orchestrator.db_path), "settings.json"),
    )
    # Find the actual config file
    if not os.path.exists(config_path):
        config_path = "/etc/studio/settings.json"
    if not os.path.exists(config_path):
        logger.warning("Cannot persist fleet settings: settings.json not found at %s", config_path)
        return

    try:
        with open(config_path) as f:
            data = _json.load(f)
    except Exception:
        data = {}

    data.setdefault("remote_fleet", {})
    data["remote_fleet"]["hosts"] = [h.model_dump() for h in settings.remote_fleet.hosts]

    with open(config_path, "w") as f:
        _json.dump(data, f, indent=2)
    logger.info("Fleet settings persisted to %s", config_path)


_CLI_HANDLERS = {
    "studio.submit": _cli_submit,
    "studio.approve": _cli_approve,
    "studio.reject": _cli_reject,
    "studio.list": _cli_list,
    "studio.show": _cli_show,
    "studio.show_worker": _cli_show_worker,
    "studio.kill": _cli_kill,
    "studio.status": _cli_status,
    "studio.version": _cli_version,
    "studio.calibration_report": _cli_calibration_report,
    "studio.recall": _cli_recall,
    "studio.health": _cli_health,
    "studio.audit": _cli_audit,
    "studio.rotate_secret": _cli_rotate_secret,
    "studio.fleet_status": _cli_fleet_status,
    "studio.fleet_add": _cli_fleet_add,
    "studio.fleet_remove": _cli_fleet_remove,
    "studio.k8s_status": _cli_k8s_status,
    "studio.docker_status": _cli_docker_status,
    "studio.docker_images": _cli_docker_images,
    "studio.vm_status": _cli_vm_status,
    "studio.vm_pool_resize": _cli_vm_pool_resize,
    "studio.check_rootfs": _cli_check_rootfs,
    "studio.review_worker": _cli_review_worker,
    "studio.answer_question": _cli_answer_question,
    "studio.resume_worker": _cli_resume_worker,
    "studio.pending_escalations": _cli_pending_escalations,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load settings from file if present (auto-detects path)
    from .settings import get_settings_path
    settings_path = get_settings_path()
    if settings_path:
        logging.getLogger(__name__).info("Loading settings from %s", settings_path)
        with open(settings_path) as f:
            file_settings = json.loads(f.read())
        settings = Settings(**file_settings)
    else:
        logging.getLogger(__name__).info("No settings.json found — using defaults")
        settings = Settings()

    # Allow environment variable overrides for testing.
    # Priority: env vars > settings.json > defaults
    if os.environ.get("STUDIO_ORCH_DB_PATH"):
        settings.orchestrator.db_path = os.environ["STUDIO_ORCH_DB_PATH"]
    socket_path = (
        os.environ.get("STUDIO_SOCKET_PATH")
        or os.environ.get("STUDIO_ORCH_SOCKET_PATH")
    )
    if socket_path:
        settings.orchestrator.socket_path = socket_path
    if os.environ.get("OLLAMA_CLOUD_BASE_URL"):
        settings.ollama_cloud.base_url = os.environ["OLLAMA_CLOUD_BASE_URL"]
    else:
        os.environ["OLLAMA_CLOUD_BASE_URL"] = settings.ollama_cloud.base_url

    app = Orchestrator(settings)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        await app.start()

        stop_event = asyncio.Event()

        def _on_signal(signum, frame):
            logger.info("Received signal %s", signum)
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, _on_signal, signal.SIGTERM, None)
        loop.add_signal_handler(signal.SIGINT, _on_signal, signal.SIGINT, None)

        await stop_event.wait()
        await app.stop()

    try:
        loop.run_until_complete(_run())
    except DatabaseVersionError as exc:
        logging.critical(
            "Database schema version %d is ahead of code version %d. "
            "Upgrade the orchestrator.",
            exc.stored, exc.code,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
