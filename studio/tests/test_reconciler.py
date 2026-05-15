"""Tests for reconciler.py — crash recovery, kill-all, re-tick."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.reconciler import Reconciler
from studio.orchestrator.models import WorkerState, NodeState, BundleState


@pytest.fixture
def db_mock():
    db = MagicMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock()
    db.fetch_all = AsyncMock()
    db.conn = MagicMock()
    db.conn.commit = AsyncMock()
    return db

@pytest.fixture
def sm_mock():
    sm = MagicMock()
    sm.transition_25_fail_execution = AsyncMock()
    return sm

@pytest.fixture
def executor_mock():
    ex = MagicMock()
    ex.start_bundle = AsyncMock()
    return ex

@pytest.fixture
def reconciler(db_mock, sm_mock, executor_mock):
    return Reconciler(db_mock, sm_mock, executor_mock)


class TestReconcilerReconcile:
    @pytest.mark.asyncio
    async def test_kills_orphan_workers(self, reconciler, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "w1", "bundle_id": "b1", "node_id": "n1"}],  # running workers
            [],  # skip_downstream edges
            [],  # verifying bundles
            [],  # in_progress bundles
        ]

        counts = await reconciler.reconcile()
        assert counts["workers_killed"] == 1
        assert counts["nodes_failed"] == 1

    @pytest.mark.asyncio
    async def test_fails_affected_bundles(self, reconciler, db_mock, sm_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "w1", "bundle_id": "b1", "node_id": "n1"}],
            [],  # skip_downstream
            [],  # verifying bundles
            [],  # in_progress bundles
        ]

        await reconciler.reconcile()
        sm_mock.transition_25_fail_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_orphans_does_nothing(self, reconciler, db_mock, sm_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # no running workers
            [],  # verifying bundles
            [],  # in_progress bundles
        ]

        counts = await reconciler.reconcile()
        assert counts["workers_killed"] == 0
        sm_mock.transition_25_fail_execution.assert_not_called()

    @pytest.mark.asyncio
    async def test_reticks_verifying_bundles(self, reconciler, db_mock, executor_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # no running workers
            [{"id": "b2"}],  # bundles in verifying
            [],  # in_progress bundles
        ]

        counts = await reconciler.reconcile()
        assert counts["bundles_recovered"] == 1
        executor_mock.start_bundle.assert_called_once_with("b2")

    @pytest.mark.asyncio
    async def test_multiple_orphan_workers(self, reconciler, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [
                {"id": "w1", "bundle_id": "b1", "node_id": "n1"},
                {"id": "w2", "bundle_id": "b1", "node_id": "n2"},
                {"id": "w3", "bundle_id": "b2", "node_id": "n1"},
            ],
            [],  # skip_downstream w1
            [],  # skip_downstream w2
            [],  # skip_downstream w3
            [],  # verifying bundles
            [],  # in_progress bundles
        ]

        counts = await reconciler.reconcile()
        assert counts["workers_killed"] == 3
        assert counts["nodes_failed"] == 3

    @pytest.mark.asyncio
    async def test_paused_workers_also_killed(self, reconciler, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "w_paused", "bundle_id": "b1", "node_id": "n1"}],
            [],
            [],
            [],  # in_progress bundles
        ]

        counts = await reconciler.reconcile()
        assert counts["workers_killed"] == 1

    @pytest.mark.asyncio
    async def test_skip_downstream_marks_as_skipped(self, reconciler, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"to_node_id": "n2"}],
            [],  # no more edges
        ]

        await reconciler._skip_downstream("b1", "n1")

        skip_calls = [c for c in db_mock.execute.call_args_list
                      if NodeState.SKIPPED in str(c[0][1])]
        assert len(skip_calls) >= 1

    @pytest.mark.asyncio
    async def test_reconcile_writes_audit_event(self, reconciler, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],
            [],
            [],  # in_progress bundles
        ]

        await reconciler.reconcile()

        audit_calls = [c for c in db_mock.execute.call_args_list
                       if "audit_log" in str(c[0][0])]
        assert len(audit_calls) >= 1
        audit_call = audit_calls[-1]
        assert "reconciler_run" in str(audit_call[0][1])

    @pytest.mark.asyncio
    async def test_reconcile_idempotent(self, reconciler, db_mock, sm_mock):
        """Running reconcile twice should produce same result."""
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "w1", "bundle_id": "b1", "node_id": "n1"}],
            [],
            [],
            [],  # in_progress bundles
        ]
        await reconciler.reconcile()
        assert sm_mock.transition_25_fail_execution.call_count == 1

        # Run again
        sm_mock.reset_mock()

        db_mock.fetch_all.side_effect = [
            [{"id": "w1", "bundle_id": "b1", "node_id": "n1"}],
            [],
            [],
            [],  # in_progress bundles
        ]
        await reconciler.reconcile()
        assert sm_mock.transition_25_fail_execution.call_count == 1
