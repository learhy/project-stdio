"""Orchestrator entry point: wires all components and starts the event loop.

Single Unix domain socket serves both worker connections (persistent,
token-authenticated) and CLI/admin requests (one-shot JSON-RPC).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .github import GitHubClient

from .db import Database, create_database
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
from .runner import LocalBwrapWorkerRunner
from .executor import DagExecutor
from .scheduler import Scheduler
from .reconciler import Reconciler
from .models import Settings, OrchestratorSettings, ApprovalTier
from .approval import (
    evaluate_approval_matrix,
    cooldown_seconds,
    MandatoryReviewTrigger,
)

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
        self.runner: LocalBwrapWorkerRunner | None = None
        self.executor: DagExecutor | None = None
        self.scheduler: Scheduler | None = None
        self.reconciler: Reconciler | None = None
        self._server: asyncio.AbstractServer | None = None
        self._http_server: "uvicorn.Server | None" = None
        self._poll_task: asyncio.Task | None = None
        self._http_task: asyncio.Task | None = None
        self.github_client: "GitHubClient | None" = None
        self._processed_comment_ids: set[int] = set()
        self._last_poll_time: datetime | None = None
        self._running = False

    async def _on_bundler_report(self, bundle_id: str, proposal: dict) -> None:
        """Callback from RpcHandlers when a bundler worker sends final_report."""
        await self.sm.transition_complete_bundler_planning(bundle_id, proposal)
        logger.info("Bundler planning complete for bundle %s: C=%s R=%s target=%s",
                     bundle_id, proposal.get("complexity_score"),
                     proposal.get("risk_score"), proposal.get("target"))

    async def _on_bundler_failure(self, bundle_id: str, reason: str) -> None:
        """Callback from RpcHandlers when a bundler worker reports failure."""
        await self.sm.transition_bundler_failed(bundle_id, reason)
        logger.warning("Bundler failed for bundle %s: %s", bundle_id, reason)

    async def _on_review_complete(self, bundle_id: str, role: str, findings: list) -> None:
        """Callback from RpcHandlers when a review track worker completes successfully."""
        logger.info("Review track %s complete for bundle %s: %s findings",
                     role, bundle_id, len(findings))

    async def _on_review_blocking(self, bundle_id: str, blocking_reason: str) -> None:
        """Callback from RpcHandlers when a review track reports a blocking issue."""
        await self.sm.transition_3_return_to_proposed(bundle_id, blocking_reason)
        logger.warning("Review track blocking issue for bundle %s: %s", bundle_id, blocking_reason)

    async def _on_review_aggregator_complete(self, bundle_id: str, merged: dict) -> None:
        """Callback from DagExecutor when the review aggregator completes.

        Bundle 2.4: calls the approval matrix evaluator stub (always returns approved).
        Bundle 2.5: replaces with the real matrix evaluator.
        """
        logger.info("Review tracks complete for bundle %s; evaluating approval matrix stub", bundle_id)
        await self._evaluate_approval_matrix(bundle_id, merged)

    async def _evaluate_approval_matrix(self, bundle_id: str, merged_findings: dict) -> None:
        """Evaluate the approval matrix with the real deterministic evaluator."""
        # Publish merged review-summary artifact
        try:
            if self.executor and self.executor._artifact_store:
                await self.executor._artifact_store.publish(
                    namespace="bundle",
                    name="review-summary",
                    version=bundle_id,
                    content_type="application/json",
                    data=json.dumps(merged_findings).encode("utf-8"),
                    bundle_id=bundle_id,
                )
        except Exception as exc:
            logger.warning("Failed to publish review-summary artifact: %s", exc)

        # Fetch bundle proposal data
        row = await self.db.fetch_one(
            "SELECT proposal_json, complexity_score, risk_score, tier FROM bundles WHERE id = ?",
            (bundle_id,),
        )
        if row is None:
            logger.error("Bundle %s not found for approval matrix evaluation", bundle_id)
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
            await self.sm._github_post_mirror(
                bundle_id,
                f"Auto-approved (tier: {tier_str}, reason: {decision.reason})",
            )
        elif decision.tier == ApprovalTier.AUTO_NOTIFY:
            # Auto-notify: fire transition 4 automatically, post notification
            await self.sm.transition_4_approve_from_review(bundle_id, "approval-matrix")
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
        """Callback from DagExecutor when bundle enters VERIFYING state. Spawns QA worker."""
        await self._spawn_qa_worker(bundle_id)

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

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all subsystems, recover state, and begin serving."""
        cfg = self.settings.orchestrator

        # 1. Database
        self.db = await create_database(cfg.db_path)

        # 2. State machine (kernel mode — Phase 1 approves/rejects directly)
        self.sm = BundleStateMachine(self.db, kernel_mode=True)

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

        # 4. Worker runner (use noop for testing if bwrap unavailable)
        if os.environ.get("STUDIO_TEST_MODE") == "1":
            from .runner import NoopWorkerRunner
            self.runner = NoopWorkerRunner(self.db)
            logger.info("Test mode: using NoopWorkerRunner")
        else:
            self.runner = LocalBwrapWorkerRunner(
                self.db,
                cfg.socket_path,
                egress_proxy=self.settings.egress_proxy,
            )

        # 5. Executor
        self.executor = DagExecutor(
            self.db,
            self.sm,
            self.runner,
            self.handlers,
            self.conn_mgr,
            global_concurrency=self.settings.worker.global_concurrency,
            heartbeat_timeout_multiplier=self.settings.worker.heartbeat_timeout_multiplier,
        )
        self.executor._on_review_aggregator_complete = self._on_review_aggregator_complete
        self.executor._on_bundle_verifying = self._on_bundle_verifying

        # 6. Scheduler
        self.scheduler = Scheduler(
            self.db,
            self.executor,
            dispatch_interval=1.0,
            heartbeat_check_interval=float(
                self.settings.worker.heartbeat_max_interval_minutes * 60
            ),
        )

        # 7. Reconciler
        self.reconciler = Reconciler(self.db, self.sm, self.executor)

        # 8. Crash recovery (idempotent)
        counts = await self.reconciler.reconcile()
        logger.info("Reconciliation complete: %s", counts)

        # 9. Start periodic loops
        await self.scheduler.start()
        logger.info("Scheduler started")

        # 9.5. Start HTTP server + GitHub polling (if enabled)
        if self.settings.github.enabled:
            self._http_task = asyncio.create_task(self._run_http_server())
            self._poll_task = asyncio.create_task(self._poll_github_issues())
            logger.info("HTTP server + GitHub polling started")

        # 10. Bind socket (single socket for workers + CLI)
        socket_path = cfg.socket_path
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=socket_path
        )
        os.chmod(socket_path, 0o660)
        self._running = True
        logger.info("Orchestrator listening on %s", socket_path)

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

        if self._http_server:
            self._http_server.should_exit = True
            if self._http_task:
                try:
                    await self._http_task
                except asyncio.CancelledError:
                    pass

        if self.github_client:
            await self.github_client.close()

        if self.scheduler:
            await self.scheduler.stop()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

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
                try:
                    body = await request.json()
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

    async def _poll_once(self) -> None:
        if self.github_client is None:
            return
        rows = await self.db.fetch_all(
            "SELECT id, github_issue_number FROM bundles WHERE github_issue_number IS NOT NULL AND state IN (?, ?)",
            (BundleState.IN_REVIEW, BundleState.PROPOSED),
        )
        for row in rows:
            bundle_id = row["id"]
            issue_number = row["github_issue_number"]
            comments = await self.github_client.get_comments_since(issue_number, self._last_poll_time)
            for comment in comments:
                comment_id = comment.get("id", 0)
                if comment_id not in self._processed_comment_ids:
                    await self._process_comment(bundle_id, comment)
                    self._processed_comment_ids.add(comment_id)
        self._last_poll_time = datetime.now(timezone.utc)

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
                await self._serve_worker(reader, writer, body)
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

    # ── Worker session ─────────────────────────────────────────────────────

    async def _serve_worker(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        auth_body: dict,
    ) -> None:
        """Authenticate a worker, then pump RPC messages until disconnect."""
        token = auth_body.get("token", "")
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
            "SELECT id, bundle_id, node_id, token, manifest_json FROM workers WHERE token = ?",
            (token,),
        )
        if row is None:
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

        worker_id = row["id"]
        bundle_id = row["bundle_id"]
        node_id = row["node_id"]

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


async def _spawn_bundler(app: Orchestrator, bundle_id: str, bundle_input: dict) -> None:
    """Spawn a bundler worker as a standalone process (not part of a DAG)."""
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
        }),
        "OLLAMA_CLOUD_BASE_URL": app.settings.ollama_cloud.base_url,
    }

    process = await asyncio.create_subprocess_exec(
        "studio-bundler",
        env=worker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Track for cleanup
    if not hasattr(app, '_bundler_processes'):
        app._bundler_processes = {}
    app._bundler_processes[worker_id] = process
    logger.info("Bundler worker spawned: %s for bundle %s", worker_id, bundle_id)


async def _spawn_qa_worker(app: Orchestrator, bundle_id: str) -> None:
    """Spawn a post-execution QA verification worker as a standalone process."""
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
            "verification_plan": verification_plan,
            "bundle_branch": f"bundle/{bundle_id}",
            "repo_path": os.getcwd(),
        }),
        "OLLAMA_CLOUD_BASE_URL": app.settings.ollama_cloud.base_url,
    }

    process = await asyncio.create_subprocess_exec(
        "studio-qa",
        env=worker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

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
    return {
        "total_entries": total,
        "entries_with_divergence": diverged_count,
        "recent": entries[-10:],
    }


async def _cli_approve(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    await app.sm.transition_1a_approve(bundle_id, "cli")

    # Transition 6: start execution
    await app.sm.transition_6_start_execution(bundle_id)
    await app.executor.start_bundle(bundle_id)

    return {"approved": True}


async def _cli_reject(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    reason = params.get("reason", "rejected via CLI")
    await app.sm.transition_1b_reject(bundle_id, "cli", reason)
    return {"rejected": True}


async def _cli_list(app: Orchestrator, params: dict) -> dict:
    state = params.get("state")
    if state:
        rows = await app.db.fetch_all(
            "SELECT id, state, created_at, proposal_json FROM bundles WHERE state = ?",
            (state,),
        )
    else:
        rows = await app.db.fetch_all(
            "SELECT id, state, created_at, proposal_json FROM bundles WHERE state NOT IN (?, ?, ?, ?, ?)",
            ("complete", "failed", "rejected", "parked", "aborted"),
        )

    bundles = []
    for r in rows:
        secs = app.sm.now() - (r["created_at"] or 0)
        age = _format_age(secs)
        proposal = json.loads(r["proposal_json"] or "{}")
        bundles.append({
            "id": r["id"],
            "state": r["state"],
            "age": age,
            "idea": proposal.get("bundle_input", {}).get("idea", ""),
        })
    return {"bundles": bundles}


async def _cli_show(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    row = await app.db.fetch_one(
        "SELECT id, state, proposal_json FROM bundles WHERE id = ?", (bundle_id,)
    )
    if row is None:
        raise ValueError(f"Bundle {bundle_id} not found")

    proposal = json.loads(row["proposal_json"] or "{}")
    nodes = await app.db.fetch_all(
        "SELECT id, node_id, kind, state FROM dag_nodes WHERE bundle_id = ?", (bundle_id,)
    )

    return {
        "bundle_id": row["id"],
        "state": row["state"],
        "idea": proposal.get("bundle_input", {}).get("idea", ""),
        "nodes": [dict(n) for n in nodes],
    }


async def _cli_show_worker(app: Orchestrator, params: dict) -> dict:
    worker_id = params.get("worker_id", "")
    row = await app.db.fetch_one(
        "SELECT id, bundle_id, node_id, state, current_phase, last_heartbeat FROM workers WHERE id = ?",
        (worker_id,),
    )
    if row is None:
        raise ValueError(f"Worker {worker_id} not found")

    heartbeat_ago = ""
    if row["last_heartbeat"]:
        secs = app.sm.now() - row["last_heartbeat"]
        heartbeat_ago = _format_age(secs)

    logs = await app.db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE subject_id = ? AND event_type LIKE 'worker.log.%' ORDER BY id DESC LIMIT 20",
        (worker_id,),
    )

    recent_logs = []
    for l in logs:
        try:
            payload = json.loads(l["payload_json"] or "{}")
            recent_logs.append({"level": "info", "message": payload.get("message", "")})
        except Exception:
            pass

    return {
        "worker_id": row["id"],
        "bundle_id": row["bundle_id"],
        "state": row["state"],
        "phase": row["current_phase"] or "unknown",
        "last_heartbeat_ago": heartbeat_ago,
        "recent_logs": recent_logs,
    }


async def _cli_kill(app: Orchestrator, params: dict) -> dict:
    bundle_id = params.get("bundle_id", "")
    workers = await app.db.fetch_all(
        "SELECT id FROM workers WHERE bundle_id = ? AND state = ?",
        (bundle_id, "running"),
    )
    for w in workers:
        proc = app.executor._running_workers.pop(w["id"], None)
        if proc and proc.returncode is None:
            await app.runner.kill_worker(proc, w["id"])

    await app.sm.transition_25_fail_execution(bundle_id, "killed via CLI")
    return {"workers_killed": len(workers)}


async def _cli_status(app: Orchestrator, params: dict) -> dict:
    bundles = await app.db.fetch_all(
        "SELECT id, state, proposal_json FROM bundles WHERE state NOT IN (?, ?, ?, ?, ?)",
        ("complete", "failed", "rejected", "parked", "aborted"),
    )
    return {
        "uptime": 0,  # Phase 1: not tracking precise uptime
        "bundles": [
            {
                "id": b["id"],
                "state": b["state"],
                "idea": json.loads(b["proposal_json"] or "{}").get("bundle_input", {}).get("idea", ""),
            }
            for b in bundles
        ],
    }


_CLI_HANDLERS = {
    "studio.submit": _cli_submit,
    "studio.approve": _cli_approve,
    "studio.reject": _cli_reject,
    "studio.list": _cli_list,
    "studio.show": _cli_show,
    "studio.show_worker": _cli_show_worker,
    "studio.kill": _cli_kill,
    "studio.status": _cli_status,
    "studio.calibration_report": _cli_calibration_report,
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

    settings = Settings()
    # Allow environment variable overrides for testing
    if os.environ.get("STUDIO_ORCH_DB_PATH"):
        settings.orchestrator.db_path = os.environ["STUDIO_ORCH_DB_PATH"]
    if os.environ.get("STUDIO_ORCH_SOCKET_PATH"):
        settings.orchestrator.socket_path = os.environ["STUDIO_ORCH_SOCKET_PATH"]

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
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
