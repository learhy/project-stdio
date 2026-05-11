"""Scheduler: periodic dispatch loop and heartbeat timeout checker.

Wraps the executor's dispatch and heartbeat monitoring in async loops.
Phase 1 uses FIFO scheduling by ready_at timestamp (handled in executor).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import Database


class Scheduler:
    """Periodic scheduler for worker dispatch and liveness checks."""

    def __init__(
        self,
        db: "Database",
        executor: Any,  # DagExecutor
        dispatch_interval: float = 1.0,
        heartbeat_check_interval: float = 10.0,
    ) -> None:
        self.db = db
        self.executor = executor
        self.dispatch_interval = dispatch_interval
        self.heartbeat_check_interval = heartbeat_check_interval
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start the periodic dispatch and heartbeat check loops."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._dispatch_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]

    async def stop(self) -> None:
        """Stop all periodic loops."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _dispatch_loop(self) -> None:
        """Periodically try to dispatch ready nodes from all active bundles."""
        while self._running:
            try:
                # Process artifact events to unblock nodes waiting on inputs
                await self.executor.process_artifact_events()
                for bundle_id in list(self.executor._active_bundles):
                    await self.executor._dispatch_ready(bundle_id)
                    # Also check bundle completion each tick so bundles whose
                    # nodes were completed externally (e.g. NoopWorkerRunner)
                    # can transition to VERIFYING / COMPLETE.
                    await self.executor._check_bundle_completion(bundle_id)
            except Exception:
                pass
            await asyncio.sleep(self.dispatch_interval)

    async def _heartbeat_loop(self) -> None:
        """Periodically check for wedged workers (heartbeat timeout)."""
        while self._running:
            try:
                await self.executor.check_heartbeat_timeouts()
            except Exception:
                pass
            await asyncio.sleep(self.heartbeat_check_interval)
