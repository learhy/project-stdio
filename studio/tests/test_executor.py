"""Tests for executor.py — DAG executor, node lifecycle, dispatch, heartbeat monitoring."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.executor import LinearDagExecutor
from studio.orchestrator.models import NodeState, WorkerState, BundleState


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
    sm.transition_9_to_verifying = AsyncMock()
    sm.transition_17_complete = AsyncMock()
    sm.transition_25_fail_execution = AsyncMock()
    return sm

@pytest.fixture
def runner_mock():
    runner = MagicMock()
    runner.spawn_worker = AsyncMock()
    runner.kill_worker = AsyncMock()
    return runner

@pytest.fixture
def rpc_handlers_mock():
    h = MagicMock()
    h.set_on_final_report = MagicMock()
    h.set_on_heartbeat = MagicMock()
    return h

@pytest.fixture
def conn_mgr_mock():
    return MagicMock()

@pytest.fixture
def executor(db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
    return LinearDagExecutor(
        db=db_mock,
        sm=sm_mock,
        runner=runner_mock,
        rpc_handlers=rpc_handlers_mock,
        conn_mgr=conn_mgr_mock,
        global_concurrency=4,
        heartbeat_timeout_multiplier=2.0,
    )


class TestLinearDagExecutorInit:
    def test_wires_callbacks(self, executor, rpc_handlers_mock):
        rpc_handlers_mock.set_on_final_report.assert_called_once()
        rpc_handlers_mock.set_on_heartbeat.assert_called_once()

    def test_default_values(self, executor):
        assert executor.global_concurrency == 4
        assert executor.heartbeat_timeout_multiplier == 2.0
        assert executor._running_workers == {}
        assert executor._active_bundles == set()


class TestStartBundle:
    @pytest.mark.asyncio
    async def test_marks_entry_nodes_ready(self, executor, db_mock):
        bundle_id = "b1"
        nodes = [
            {"id": "b1:n1", "node_id": "n1"},
            {"id": "b1:n2", "node_id": "n2"},
        ]
        db_mock.fetch_all.return_value = nodes
        # start_bundle: n1 no edge, n2 has edge | then _dispatch_ready: _count_running_workers
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            None,     # n1: no incoming edge -> entry
            {"id": 1},  # n2: has incoming edge -> NOT entry
            {"cnt": 0},  # _count_running_workers in _dispatch_ready
            {"proposal_json": "{}"},  # bundle proposal
        ]
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            nodes,  # start_bundle: all nodes
            [],     # _dispatch_ready: ready nodes (none ready yet, or any)
            [],     # _dispatch_ready: extra if needed
        ]

        await executor.start_bundle(bundle_id)

        # n1 should be marked ready
        ready_calls = [
            c for c in db_mock.execute.call_args_list
            if "UPDATE dag_nodes SET state = ?" in str(c[0][0])
            and NodeState.READY in str(c[0][1])
        ]
        assert len(ready_calls) >= 1

    @pytest.mark.asyncio
    async def test_adds_bundle_to_active_set(self, executor, db_mock):
        db_mock.fetch_all.return_value = []
        db_mock.fetch_one.return_value = None
        await executor.start_bundle("b1")
        assert "b1" in executor._active_bundles


class TestStopBundle:
    @pytest.mark.asyncio
    async def test_kills_running_workers(self, executor, db_mock):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        executor._running_workers["w1"] = mock_proc

        db_mock.fetch_all.return_value = [{"id": "w1"}]

        await executor.stop_bundle("b1")
        executor.runner.kill_worker.assert_called_once_with(mock_proc)
        assert "b1" not in executor._active_bundles

    @pytest.mark.asyncio
    async def test_discards_from_active(self, executor, db_mock):
        executor._active_bundles.add("b1")
        db_mock.fetch_all.return_value = []
        await executor.stop_bundle("b1")
        assert "b1" not in executor._active_bundles


class TestOnFinalReport:
    @pytest.mark.asyncio
    async def test_success_advances_chain(self, executor, db_mock, sm_mock):
        executor._active_bundles.add("b1")
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"to_node_id": "n2"}],  # _advance_chain: outgoing edges
            [],  # _check_bundle_completion -> _compute_bundle_status (no exit nodes via edges join)
            [{"state": NodeState.COMPLETED}],  # fallback: all nodes state check
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # _dispatch_ready: running count
        ]

        await executor._on_final_report("b1", "n1", "w1", {
            "outcome": "success", "node_state": NodeState.COMPLETED
        })

        # Should have updated edges and marked successor ready
        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE" in str(c[0][0])]
        assert len(update_calls) >= 2  # edge fired + successor ready

    @pytest.mark.asyncio
    async def test_failure_fails_bundle(self, executor, db_mock, sm_mock):
        executor._active_bundles.add("b1")
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # skip_downstream: outgoing edges from failed node
            [],  # stop_bundle: running workers
        ]

        await executor._on_final_report("b1", "n1", "w1", {
            "outcome": "failure", "node_state": NodeState.FAILED
        })

        # Should have called fail_execution
        sm_mock.transition_25_fail_execution.assert_called()


class TestAdvanceChain:
    @pytest.mark.asyncio
    async def test_fires_edge_and_marks_successor_ready(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"to_node_id": "n2"}],  # _advance_chain: outgoing edges
            [],  # _check_bundle_completion -> _compute_bundle_status (exit nodes join)
            [{"state": NodeState.COMPLETED}],  # _compute_bundle_status fallback: all nodes
            [],  # _check_bundle_completion: failed nodes query
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # _dispatch_ready: running count
        ]

        await executor._advance_chain("b1", "n1")

        edge_update = db_mock.execute.call_args_list[0]
        assert "dag_edges" in str(edge_update[0][0])
        assert "fired = 1" in str(edge_update[0][0])

        node_update = db_mock.execute.call_args_list[1]
        assert "dag_nodes" in str(node_update[0][0])
        assert NodeState.READY in node_update[0][1]

    @pytest.mark.asyncio
    async def test_no_successor_no_edges(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # _advance_chain: no outgoing edges
            [],  # _check_bundle_completion -> _compute_bundle_status
            [{"state": NodeState.COMPLETED}],  # all nodes fallback
            [],  # _check_bundle_completion: failed nodes query
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # _dispatch_ready: running count
        ]
        # Should not crash
        await executor._advance_chain("b1", "n_last")


class TestFailBundle:
    @pytest.mark.asyncio
    async def test_kills_workers_and_transitions(self, executor, db_mock, sm_mock):
        executor._active_bundles.add("b1")
        executor._running_workers["w1"] = MagicMock()

        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # skip_downstream: no edges
            [],  # stop_bundle: running workers
        ]

        await executor._fail_bundle("b1", "n1", "w1", "test failure")

        sm_mock.transition_25_fail_execution.assert_called_once_with("b1", "test failure")
        assert "b1" not in executor._active_bundles


class TestSkipDownstream:
    @pytest.mark.asyncio
    async def test_skips_all_downstream(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"to_node_id": "n2"}],   # from n1
            [],                         # from n2
        ]

        await executor._skip_downstream("b1", "n1")

        # n2 should be marked skipped
        skip_calls = [c for c in db_mock.execute.call_args_list
                      if NodeState.SKIPPED in str(c[0][1])]
        assert len(skip_calls) >= 1


class TestDispatchReady:
    @pytest.mark.asyncio
    async def test_dispatches_ready_nodes(self, executor, db_mock, runner_mock):
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # running count
            {"proposal_json": "{}"},  # bundle proposal
        ]
        db_mock.fetch_all.return_value = [{
            "id": "b1:n1", "node_id": "n1",
            "spec_json": json.dumps({"spec": {"objective": "test"}}),
        }]

        result = MagicMock()
        result.worker_id = "w_b1_n1"
        result.process = MagicMock()
        runner_mock.spawn_worker.return_value = result

        dispatched = await executor._dispatch_ready("b1")
        assert dispatched == 1
        runner_mock.spawn_worker.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_dispatch_when_at_cap(self, executor, db_mock):
        db_mock.fetch_one.return_value = {"cnt": 4}  # already at cap
        dispatched = await executor._dispatch_ready("b1")
        assert dispatched == 0

    @pytest.mark.asyncio
    async def test_no_ready_nodes(self, executor, db_mock):
        db_mock.fetch_one.return_value = {"cnt": 0}
        db_mock.fetch_all.return_value = []
        dispatched = await executor._dispatch_ready("b1")
        assert dispatched == 0


class TestComputeBundleStatus:
    @pytest.mark.asyncio
    async def test_all_terminal(self, executor, db_mock):
        db_mock.fetch_all.return_value = [
            {"state": NodeState.COMPLETED},
            {"state": NodeState.COMPLETED},
        ]
        status = await executor._compute_bundle_status("b1")
        assert status == "all_terminal"

    @pytest.mark.asyncio
    async def test_in_progress(self, executor, db_mock):
        db_mock.fetch_all.return_value = [
            {"state": NodeState.COMPLETED},
            {"state": NodeState.RUNNING},
        ]
        status = await executor._compute_bundle_status("b1")
        assert status == "in_progress"

    @pytest.mark.asyncio
    async def test_empty(self, executor, db_mock):
        db_mock.fetch_all.return_value = []
        status = await executor._compute_bundle_status("b1")
        assert status == "empty"


class TestCheckHeartbeatTimeouts:
    @pytest.mark.asyncio
    async def test_no_timeouts(self, executor, db_mock):
        db_mock.fetch_all.return_value = [{
            "id": "w1", "bundle_id": "b1", "node_id": "n1",
            "last_heartbeat": executor.now(),  # just now
            "manifest_json": json.dumps({"grants": {"resources": {"wall_time_limit": 3600}}}),
        }]
        timed_out = await executor.check_heartbeat_timeouts()
        assert timed_out == []

    @pytest.mark.asyncio
    async def test_worker_timed_out(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{  # check_heartbeat_timeouts: running workers query
                "id": "w1", "bundle_id": "b1", "node_id": "n1",
                "last_heartbeat": executor.now() - 10000,
                "manifest_json": json.dumps({"grants": {"resources": {"wall_time_limit": 1}}}),
            }],
            [],  # _skip_downstream: no downstream edges
            [],  # _skip_downstream: second BFS level
            [],  # stop_bundle -> running workers
        ]
        db_mock.fetch_one = AsyncMock(return_value=None)
        timed_out = await executor.check_heartbeat_timeouts()
        assert "w1" in timed_out

    @pytest.mark.asyncio
    async def test_missing_heartbeat_skipped(self, executor, db_mock):
        db_mock.fetch_all.return_value = [{
            "id": "w1", "bundle_id": "b1", "node_id": "n1",
            "last_heartbeat": None,
            "manifest_json": "{}",
        }]
        timed_out = await executor.check_heartbeat_timeouts()
        assert timed_out == []


class TestGetNodeState:
    @pytest.mark.asyncio
    async def test_returns_state(self, executor, db_mock):
        db_mock.fetch_one.return_value = {"state": NodeState.RUNNING}
        state = await executor.get_node_state("b1", "n1")
        assert state == NodeState.RUNNING

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, executor, db_mock):
        db_mock.fetch_one.return_value = None
        state = await executor.get_node_state("b1", "nx")
        assert state is None


class TestCheckBundleCompletion:
    @pytest.mark.asyncio
    async def test_all_complete_transitions_to_complete(self, executor, db_mock, sm_mock):
        db_mock.fetch_all.return_value = []  # no failed nodes
        with patch.object(executor, '_compute_bundle_status', new=AsyncMock(return_value="all_terminal")):
            await executor._check_bundle_completion("b1")
            sm_mock.transition_9_to_verifying.assert_called_once()
            sm_mock.transition_17_complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_nodes_fail_bundle(self, executor, db_mock, sm_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "b1:n1", "state": NodeState.FAILED}],  # failed nodes query
            [],  # _skip_downstream: no edges
            [],  # stop_bundle: running workers
        ]
        with patch.object(executor, '_compute_bundle_status', new=AsyncMock(return_value="all_terminal")):
            await executor._check_bundle_completion("b1")
            sm_mock.transition_25_fail_execution.assert_called()

    @pytest.mark.asyncio
    async def test_still_in_progress_does_nothing(self, executor, db_mock, sm_mock):
        with patch.object(executor, '_compute_bundle_status', new=AsyncMock(return_value="in_progress")):
            await executor._check_bundle_completion("b1")
            sm_mock.transition_9_to_verifying.assert_not_called()
