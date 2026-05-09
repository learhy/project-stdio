"""Tests for scheduler.py — periodic dispatch and heartbeat check loops."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.scheduler import Scheduler


@pytest.fixture
def db_mock():
    return MagicMock()

@pytest.fixture
def executor_mock():
    ex = MagicMock()
    ex._active_bundles = {"b1", "b2"}
    ex._dispatch_ready = AsyncMock(return_value=0)
    ex.check_heartbeat_timeouts = AsyncMock(return_value=[])
    return ex

@pytest.fixture
def scheduler(db_mock, executor_mock):
    return Scheduler(
        db=db_mock,
        executor=executor_mock,
        dispatch_interval=0.01,
        heartbeat_check_interval=0.01,
    )


class TestScheduler:
    @pytest.mark.asyncio
    async def test_start_creates_tasks(self, scheduler):
        await scheduler.start()
        assert len(scheduler._tasks) == 2
        assert scheduler._running is True
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self, scheduler):
        await scheduler.start()
        await scheduler.stop()
        assert scheduler._running is False
        assert len(scheduler._tasks) == 0

    @pytest.mark.asyncio
    async def test_dispatch_loop_calls_dispatch(self, scheduler, executor_mock):
        scheduler._running = True

        # Run loop for a few cycles then stop
        loop_task = asyncio.create_task(scheduler._dispatch_loop())
        await asyncio.sleep(0.05)
        scheduler._running = False
        await loop_task

        assert executor_mock._dispatch_ready.call_count >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_loop_calls_check(self, scheduler, executor_mock):
        scheduler._running = True

        loop_task = asyncio.create_task(scheduler._heartbeat_loop())
        await asyncio.sleep(0.05)
        scheduler._running = False
        await loop_task

        assert executor_mock.check_heartbeat_timeouts.call_count >= 1

    @pytest.mark.asyncio
    async def test_dispatch_loop_handles_exceptions(self, scheduler, executor_mock):
        executor_mock._dispatch_ready = AsyncMock(side_effect=RuntimeError("db error"))
        scheduler._running = True

        loop_task = asyncio.create_task(scheduler._dispatch_loop())
        await asyncio.sleep(0.05)
        scheduler._running = False
        await loop_task
        # Should not crash

    @pytest.mark.asyncio
    async def test_heartbeat_loop_handles_exceptions(self, scheduler, executor_mock):
        executor_mock.check_heartbeat_timeouts = AsyncMock(side_effect=RuntimeError("db error"))
        scheduler._running = True

        loop_task = asyncio.create_task(scheduler._heartbeat_loop())
        await asyncio.sleep(0.05)
        scheduler._running = False
        await loop_task
        # Should not crash

    def test_default_intervals(self, db_mock, executor_mock):
        s = Scheduler(db_mock, executor_mock)
        assert s.dispatch_interval == 1.0
        assert s.heartbeat_check_interval == 10.0
