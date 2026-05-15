"""ReviewScheduler: background task that evaluates worker state and triggers
LLM-based reviews to determine if intervention is needed (Bundle 5.2).

Runs alongside the Scheduler, checking all IN_PROGRESS workers every 60s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import Database
    from .rpc import RpcHandlers, ConnectionManager
    from .models import ReviewSettings

logger = logging.getLogger(__name__)


class ReviewScheduler:
    """Periodic review scheduler for mid-flight worker quality management."""

    def __init__(
        self,
        db: "Database",
        review_settings: "ReviewSettings",
        handlers: "RpcHandlers",
        conn_mgr: "ConnectionManager",
    ) -> None:
        self.db = db
        self.settings = review_settings
        self.handlers = handlers
        self.conn_mgr = conn_mgr
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._review_loop())
        logger.info("ReviewScheduler started (interval=%dmin, divergence=%.1fx, silence=%dmin)",
                    self.settings.interval_minutes, self.settings.time_divergence_threshold,
                    self.settings.checkpoint_silence_minutes)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def trigger_review(self, worker_id: str, bundle_id: str, node_id: str,
                             reason: str) -> dict | None:
        """PM-initiated or post-checkpoint review of a specific worker."""
        return await self._review_worker(worker_id, bundle_id, node_id, reason)

    # ── Main loop ───────────────────────────────────────────────────────────

    async def _review_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("ReviewScheduler tick error")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        if not self.settings.enabled:
            return

        now = int(time.time())
        rows = await self.db.fetch_all(
            "SELECT w.id, w.bundle_id, w.node_id, w.last_heartbeat, w.created_at, "
            "w.started_at, w.last_reviewed_at, w.questions_asked, "
            "b.proposal_json "
            "FROM workers w JOIN bundles b ON w.bundle_id = b.id "
            "WHERE w.state = ?",
            ("running",),
        )

        for row in rows:
            await self._evaluate_triggers(dict(row), now)

    async def _evaluate_triggers(self, worker: dict, now: int) -> None:
        worker_id = worker["id"]
        bundle_id = worker["bundle_id"]
        node_id = worker["node_id"]
        last_reviewed = worker.get("last_reviewed_at") or 0
        started_at = worker.get("started_at") or worker.get("created_at") or 0
        last_heartbeat = worker.get("last_heartbeat") or 0

        # Dedup: skip if reviewed recently
        if now - last_reviewed < self.settings.min_interval_seconds:
            return

        trigger_reason: str | None = None

        # 1. Time trigger: every interval_minutes, skip if running < 5 min
        run_time_minutes = (now - started_at) / 60
        if run_time_minutes >= 5 and run_time_minutes >= self.settings.interval_minutes:
            trigger_reason = "time_trigger"

        # 2. Wall time divergence trigger
        if trigger_reason is None:
            try:
                proposal = json.loads(worker.get("proposal_json", "{}"))
                bundler = proposal.get("proposal", {})
                estimated_duration = bundler.get("estimated_duration_seconds", 0)
                if estimated_duration > 0:
                    elapsed = now - started_at
                    if elapsed > estimated_duration * self.settings.time_divergence_threshold:
                        trigger_reason = "anomaly:wall_time_divergence"
            except (json.JSONDecodeError, TypeError):
                pass

        # 3. Checkpoint silence trigger
        if trigger_reason is None:
            try:
                proposal = json.loads(worker.get("proposal_json", "{}"))
                bundler = proposal.get("proposal", {})
                estimated_duration = bundler.get("estimated_duration_seconds", 0)
                if estimated_duration > 600:  # task estimated > 10 min
                    cp_row = await self.db.fetch_one(
                        "SELECT MAX(created_at) as last_cp FROM worker_checkpoints WHERE worker_id = ?",
                        (worker_id,),
                    )
                    last_cp = (cp_row["last_cp"] if cp_row and cp_row["last_cp"] else 0) if cp_row else 0
                    silence_minutes = (now - (last_cp or started_at)) / 60
                    if silence_minutes >= self.settings.checkpoint_silence_minutes:
                        trigger_reason = "anomaly:checkpoint_silence"
            except (json.JSONDecodeError, TypeError):
                pass

        if trigger_reason:
            await self._review_worker(worker_id, bundle_id, node_id, trigger_reason)

    # ── Review execution ────────────────────────────────────────────────────

    async def _review_worker(self, worker_id: str, bundle_id: str, node_id: str,
                             reason: str) -> dict | None:
        now = int(time.time())

        # Record review trigger
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("review.triggered", "worker", worker_id,
             json.dumps({"reason": reason, "bundle_id": bundle_id, "node_id": node_id}), now),
        )
        await self.db.conn.commit()

        # Collect context
        context = await self._collect_review_context(worker_id, bundle_id, node_id)
        if context is None:
            return None

        # Call LLM
        verdict = await self._call_review_llm(context)
        if verdict is None:
            return None

        # Handle verdict
        await self._handle_verdict(worker_id, bundle_id, node_id, verdict, reason)

        # Update last_reviewed_at for dedup
        await self.db.execute(
            "UPDATE workers SET last_reviewed_at = ? WHERE id = ?",
            (now, worker_id),
        )
        await self.db.conn.commit()

        return verdict

    async def _collect_review_context(self, worker_id: str, bundle_id: str,
                                      node_id: str) -> dict | None:
        """Gather task spec, proposal, checkpoints, and trajectory data."""
        worker_row = await self.db.fetch_one(
            "SELECT w.*, b.proposal_json FROM workers w "
            "JOIN bundles b ON w.bundle_id = b.id WHERE w.id = ?",
            (worker_id,),
        )
        if worker_row is None:
            return None

        worker = dict(worker_row)
        now = int(time.time())

        try:
            proposal = json.loads(worker.get("proposal_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            proposal = {}

        bundler = proposal.get("proposal", {})

        # Last 3 checkpoints
        cp_rows = await self.db.fetch_all(
            "SELECT phase_completed, phase_starting, summary, concerns_json, created_at "
            "FROM worker_checkpoints WHERE worker_id = ? ORDER BY created_at DESC LIMIT 3",
            (worker_id,),
        )
        checkpoints = []
        for cp in cp_rows:
            cpd = dict(cp)
            try:
                cpd["concerns"] = json.loads(cpd.get("concerns_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                cpd["concerns"] = []
            checkpoints.append(cpd)

        started_at = worker.get("started_at") or worker.get("created_at") or now
        elapsed = now - started_at
        estimated_duration = bundler.get("estimated_duration_seconds", 0)

        return {
            "worker_id": worker_id,
            "bundle_id": bundle_id,
            "node_id": node_id,
            "objective": worker.get("task_spec", ""),
            "acceptance_criteria": bundler.get("requirements_summary", ""),
            "implementation_plan": bundler.get("implementation_plan", ""),
            "complexity_score": bundler.get("complexity_score", 0),
            "risk_score": bundler.get("risk_score", 0),
            "estimated_duration_seconds": estimated_duration,
            "estimated_tokens": bundler.get("estimated_tokens", 0),
            "elapsed_seconds": elapsed,
            "checkpoints": checkpoints,
            "concerns": bundler.get("concerns", []),
        }

    async def _call_review_llm(self, context: dict) -> dict | None:
        """Call Ollama Cloud LLM with structured review prompt."""
        prompt = (
            "You are a coding agent supervisor evaluating a worker's progress. "
            "Review the context and return a JSON verdict.\n\n"
            f"Task: {context.get('objective', '')[:500]}\n"
            f"Acceptance criteria: {context.get('acceptance_criteria', '')[:500]}\n"
            f"Implementation plan: {context.get('implementation_plan', '')[:500]}\n"
            f"Complexity score: {context.get('complexity_score', 0)}\n"
            f"Risk score: {context.get('risk_score', 0)}\n"
            f"Estimated duration: {context.get('estimated_duration_seconds', 0)}s\n"
            f"Elapsed time: {context.get('elapsed_seconds', 0)}s\n"
            f"Checkpoints: {json.dumps(context.get('checkpoints', []))[:1000]}\n"
            f"Concerns: {json.dumps(context.get('concerns', []))[:500]}\n\n"
            "Return ONLY a JSON object with these fields:\n"
            '{"verdict": "on_track|request_clarification|request_artifact|request_redirect|escalate_to_human", '
            '"confidence": "high|medium|low", '
            '"rationale": "2-3 sentences", '
            '"action": {"type": "inject_context|ask_artifact|none|escalate", '
            '"content": "message if inject_context", '
            '"artifact_path": "path if ask_artifact", '
            '"escalation_reason": "reason if escalate"}}'
        )

        try:
            result = await asyncio.to_thread(self._ollama_call, prompt)
            if result is None:
                return None

            # Strip markdown code fences if present
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:]) if len(lines) > 1 else result
            if result.endswith("```"):
                result = result[:-3].strip()

            return json.loads(result)
        except (json.JSONDecodeError, Exception):
            logger.warning("Failed to parse review LLM response")
            return {
                "verdict": "on_track",
                "confidence": "low",
                "rationale": "Failed to parse LLM response; defaulting to on_track",
                "action": {"type": "none"},
            }

    def _ollama_call(self, prompt: str) -> str | None:
        api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
        if not api_key:
            return None
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://ollama.com/api/chat",
                data=json.dumps({
                    "model": self.settings.model or "llama3.2",
                    "messages": [{"role": "user", "content": prompt}],
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
                return body.get("message", {}).get("content", "")
        except Exception:
            return None

    async def _handle_verdict(self, worker_id: str, bundle_id: str, node_id: str,
                              verdict: dict, trigger_reason: str) -> None:
        """Store verdict and execute action. Intervention actions deferred to Bundle 5.3."""
        now = int(time.time())
        verdict_type = verdict.get("verdict", "on_track")

        # Store in audit log
        await self.db.execute(
            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("review.verdict", "worker", worker_id,
             json.dumps({"verdict": verdict, "trigger": trigger_reason,
                        "bundle_id": bundle_id, "node_id": node_id}), now),
        )
        await self.db.conn.commit()

        logger.info("Review verdict for worker %s: %s (confidence=%s, trigger=%s)",
                    worker_id, verdict_type, verdict.get("confidence", "unknown"), trigger_reason)

        action = verdict.get("action", {})
        action_type = action.get("type", "none")

        if verdict_type == "on_track":
            return

        if action_type == "inject_context" and action.get("content"):
            if self.handlers._on_inject_context:
                import ulid
                injection_id = str(ulid.ULID())
                await self.handlers._on_inject_context(
                    worker_id, injection_id, "feedback", action["content"], None,
                )

        elif action_type == "ask_artifact" and action.get("artifact_path"):
            artifact_path = action["artifact_path"]
            try:
                result = await self.conn_mgr.call_worker(
                    worker_id, "worker.show_artifact",
                    {"path": artifact_path}, timeout=15.0,
                )
                if result and "result" in result:
                    artifact_content = result["result"].get("content", "")
                    if artifact_content and "error" not in result["result"]:
                        await self.db.execute(
                            "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            ("review.artifact_fetched", "worker", worker_id,
                             json.dumps({"path": artifact_path, "size": len(artifact_content)}), now),
                        )
                        await self.db.conn.commit()
            except Exception:
                pass

        elif action_type == "escalate":
            await self.db.execute(
                "INSERT INTO audit_log (event_type, subject_type, subject_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("review.escalated_to_human", "worker", worker_id,
                 json.dumps({"reason": action.get("escalation_reason", ""),
                            "verdict": verdict, "bundle_id": bundle_id}), now),
            )
            await self.db.conn.commit()
