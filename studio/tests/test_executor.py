"""Tests for executor.py — DAG executor, node lifecycle, dispatch, heartbeat monitoring."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.executor import DagExecutor
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
    return DagExecutor(
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
    async def test_success_processes_completion(self, executor, db_mock, sm_mock):
        executor._active_bundles.add("b1")
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "e1", "to_node_id": "n2", "condition_kind": "on_success", "condition_expr": None}],
            [],  # _check_bundle_completion -> _compute_bundle_status (no exit nodes via edges join)
            [{"state": NodeState.COMPLETED}],  # fallback: all nodes state check
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "worker", "aggregator_config_json": None, "state": "pending"},  # _try_make_ready: target node
            {"cnt": 1},  # _try_make_ready: fired count
            {"cnt": 1},  # _try_make_ready: total count
            {"cnt": 0},  # _dispatch_ready: running count
        ]

        await executor._on_final_report("b1", "n1", "w1", {
            "outcome": "success", "node_state": NodeState.COMPLETED
        })

        # Should have fired edge and marked successor ready
        edge_updates = [c for c in db_mock.execute.call_args_list
                        if "dag_edges" in str(c[0][0])]
        assert len(edge_updates) >= 1  # edge fired

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


class TestProcessNodeCompletion:
    @pytest.mark.asyncio
    async def test_fires_edge_and_marks_successor_ready(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"id": "e1", "to_node_id": "n2", "condition_kind": "on_success", "condition_expr": None}],
            [],  # _check_bundle_completion -> _compute_bundle_status (no exit nodes via edges join)
            [{"state": NodeState.COMPLETED}],  # _compute_bundle_status fallback: all nodes
            [],  # _check_bundle_completion: failed nodes query
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "worker", "aggregator_config_json": None, "state": "pending"},  # _try_make_ready: target node
            {"cnt": 1},  # _try_make_ready: fired count
            {"cnt": 1},  # _try_make_ready: total count
            {"cnt": 0},  # _dispatch_ready: running count
        ]

        await executor._process_node_completion("b1", "n1", NodeState.COMPLETED)

        edge_fire = [c for c in db_mock.execute.call_args_list
                     if "dag_edges" in str(c[0][0]) and "fired = 1" in str(c[0][0])]
        assert len(edge_fire) >= 1

        node_ready = [c for c in db_mock.execute.call_args_list
                      if "dag_nodes" in str(c[0][0]) and NodeState.READY in str(c[0][1])]
        assert len(node_ready) >= 1

    @pytest.mark.asyncio
    async def test_no_successor_no_edges(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [],  # _process_node_completion: no outgoing edges
            [],  # _check_bundle_completion -> _compute_bundle_status
            [{"state": NodeState.COMPLETED}],  # all nodes fallback
            [],  # _check_bundle_completion: failed nodes query
            [],  # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # _dispatch_ready: running count
        ]
        await executor._process_node_completion("b1", "n_last", NodeState.COMPLETED)


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
            "id": "b1:n1", "node_id": "n1", "kind": "worker",
            "spec_json": json.dumps({"spec": {"objective": "test"}}),
            "gate_config_json": None,
            "aggregator_config_json": None,
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


class TestEdgeConditionMatching:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_always_matches_completed(self, executor):
        assert await executor._edge_condition_matches("always", None, NodeState.COMPLETED, "b1", "n1") is True

    @pytest.mark.asyncio
    async def test_always_matches_failed(self, executor):
        assert await executor._edge_condition_matches("always", None, NodeState.FAILED, "b1", "n1") is True

    @pytest.mark.asyncio
    async def test_always_matches_skipped(self, executor):
        assert await executor._edge_condition_matches("always", None, NodeState.SKIPPED, "b1", "n1") is True

    @pytest.mark.asyncio
    async def test_always_does_not_match_running(self, executor):
        assert await executor._edge_condition_matches("always", None, NodeState.RUNNING, "b1", "n1") is False

    @pytest.mark.asyncio
    async def test_on_success_matches_completed(self, executor):
        assert await executor._edge_condition_matches("on_success", None, NodeState.COMPLETED, "b1", "n1") is True

    @pytest.mark.asyncio
    async def test_on_success_does_not_match_failed(self, executor):
        assert await executor._edge_condition_matches("on_success", None, NodeState.FAILED, "b1", "n1") is False

    @pytest.mark.asyncio
    async def test_on_failure_matches_failed(self, executor):
        assert await executor._edge_condition_matches("on_failure", None, NodeState.FAILED, "b1", "n1") is True

    @pytest.mark.asyncio
    async def test_on_failure_does_not_match_completed(self, executor):
        assert await executor._edge_condition_matches("on_failure", None, NodeState.COMPLETED, "b1", "n1") is False

    @pytest.mark.asyncio
    async def test_on_property_requires_completed_state(self, executor):
        assert await executor._edge_condition_matches("on_property", "true", NodeState.FAILED, "b1", "n1") is False

    @pytest.mark.asyncio
    async def test_on_property_no_expr_returns_false(self, executor):
        assert await executor._edge_condition_matches("on_property", None, NodeState.COMPLETED, "b1", "n1") is False


class TestTryMakeReady:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_worker_node_all_edges_fired_becomes_ready(self, executor, db_mock):
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "worker", "aggregator_config_json": None, "state": NodeState.PENDING},
            {"cnt": 2},  # fired count
            {"cnt": 2},  # total count
        ]

        await executor._try_make_ready("b1", "n2")

        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_node_already_past_pending_skips(self, executor, db_mock):
        db_mock.fetch_one.return_value = {"kind": "worker", "aggregator_config_json": None, "state": NodeState.RUNNING}

        await executor._try_make_ready("b1", "n2")

        # No UPDATE dag_nodes should be called
        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])]
        assert len(update_calls) == 0

    @pytest.mark.asyncio
    async def test_not_all_edges_fired_yet(self, executor, db_mock):
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "worker", "aggregator_config_json": None, "state": NodeState.PENDING},
            {"cnt": 1},  # fired: only 1 of 3
            {"cnt": 3},  # total
        ]

        await executor._try_make_ready("b1", "n2")

        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])]
        assert len(update_calls) == 0


class TestCheckAggregatorReady:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    def test_all_mode_needs_all(self, executor):
        target = {"aggregator_config_json": '{"join": "all"}'}
        assert executor._check_aggregator_ready(target, 2, 2) is True
        assert executor._check_aggregator_ready(target, 1, 2) is False

    def test_any_mode_needs_one(self, executor):
        target = {"aggregator_config_json": '{"join": "any"}'}
        assert executor._check_aggregator_ready(target, 1, 3) is True
        assert executor._check_aggregator_ready(target, 0, 3) is False

    def test_first_success_needs_one(self, executor):
        target = {"aggregator_config_json": '{"join": "first_success"}'}
        assert executor._check_aggregator_ready(target, 1, 3) is True

    def test_quorum_majority_default(self, executor):
        target = {"aggregator_config_json": '{"join": "quorum"}'}
        assert executor._check_aggregator_ready(target, 2, 3) is True
        assert executor._check_aggregator_ready(target, 1, 3) is False

    def test_quorum_explicit_count(self, executor):
        target = {"aggregator_config_json": '{"join": "quorum", "quorum_count": 2}'}
        assert executor._check_aggregator_ready(target, 2, 5) is True
        assert executor._check_aggregator_ready(target, 1, 5) is False

    def test_zero_fired_always_returns_false(self, executor):
        target = {"aggregator_config_json": '{"join": "any"}'}
        assert executor._check_aggregator_ready(target, 0, 3) is False


class TestGateDispatch:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_human_approval_creates_approval_request(self, executor, db_mock):
        node = {
            "id": "b1:gate-1", "node_id": "gate-1",
            "gate_config_json": '{"predicate": "human_approval", "human_prompt": "Ship it?"}',
        }
        await executor._dispatch_gate("b1", node)

        insert_calls = [c for c in db_mock.execute.call_args_list
                        if "approval_requests" in str(c[0][0])]
        assert len(insert_calls) == 1

        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])
                        and NodeState.RUNNING in str(c[0][1])]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_human_approval_node_stays_running(self, executor, db_mock):
        node = {
            "id": "b1:gate-1", "node_id": "gate-1",
            "gate_config_json": '{"predicate": "human_approval"}',
        }
        await executor._dispatch_gate("b1", node)

        # Gate node should be in RUNNING (not COMPLETED/FAILED), awaiting approval
        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])]
        running_updates = [c for c in update_calls
                           if NodeState.RUNNING in str(c[0][1])]
        assert len(running_updates) == 1

    @pytest.mark.asyncio
    async def test_rpc_query_gate_auto_passes(self, executor, db_mock):
        node = {
            "id": "b1:gate-1", "node_id": "gate-1",
            "gate_config_json": '{"predicate": "rpc_query"}',
        }
        await executor._dispatch_gate("b1", node)

        update_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])
                        and NodeState.COMPLETED in str(c[0][1])]
        assert len(update_calls) == 1


class TestApplyAggregatorOutput:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    def test_first_strategy_returns_first(self, executor):
        outputs = [{"a": 1}, {"b": 2}]
        result = executor._apply_aggregator_output(outputs, "first", None, {})
        assert result == {"a": 1}

    def test_first_empty_returns_none(self, executor):
        result = executor._apply_aggregator_output([], "first", None, {})
        assert result is None

    def test_collect_strategy_returns_all(self, executor):
        outputs = [{"a": 1}, {"b": 2}]
        result = executor._apply_aggregator_output(outputs, "collect", None, {})
        assert result == outputs

    def test_reduce_with_registered_reducer(self, executor):
        outputs = [{"answer": "yes"}, {"answer": "yes"}, {"answer": "no"}]
        result = executor._apply_aggregator_output(outputs, "reduce", "majority_vote", {"field": "answer"})
        assert result == "yes"

    def test_reduce_unknown_reducer_falls_back_to_collect(self, executor):
        outputs = [{"a": 1}]
        result = executor._apply_aggregator_output(outputs, "reduce", "nonexistent", {})
        assert result == outputs


class TestWouldCreateCycle:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    def test_no_cycle_linear_extension(self, executor):
        existing_ids = {"n1", "n2"}
        existing_edges = [{"from_node_id": "n1", "to_node_id": "n2"}]
        fragment_nodes = [type("N", (), {"id": "n3"})()]
        fragment_edges = []
        assert executor._would_create_cycle(
            existing_ids, existing_edges, fragment_nodes, fragment_edges, "n2"
        ) is False

    def test_detects_cycle(self, executor):
        existing_ids = {"n1", "n2"}
        existing_edges = [{"from_node_id": "n1", "to_node_id": "n2"}]
        # Adding n3 -> n1 would create n1 -> n2 -> n3 -> n1
        fragment_nodes = [type("N", (), {"id": "n3"})()]
        fragment_edges = [type("E", (), {"from_": "n3", "to": "n1"})()]
        assert executor._would_create_cycle(
            existing_ids, existing_edges, fragment_nodes, fragment_edges, "n2"
        ) is True

    def test_self_loop_detected(self, executor):
        existing_ids = {"n1"}
        existing_edges = []
        # n1 -> n1 via fragment
        fragment_nodes = []
        fragment_edges = [type("E", (), {"from_": "n1", "to": "n1"})()]
        assert executor._would_create_cycle(
            existing_ids, existing_edges, fragment_nodes, fragment_edges, "n1"
        ) is True


# ── Bundle 2.1 acceptance tests ──────────────────────────────────────────


class TestAcceptanceParallelBranches:
    """Verify both workers in parallel branches can be dispatched concurrently."""

    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
            global_concurrency=4,
        )

    @pytest.mark.asyncio
    async def test_parallel_ready_nodes_all_dispatched(self, executor, db_mock, runner_mock):
        """Both ready nodes in parallel branches should be dispatched."""
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"cnt": 0},  # running count
        ]
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.return_value = [
            {"id": "b1:n1", "node_id": "n1", "kind": "worker",
             "spec_json": json.dumps({"spec": {"objective": "Task A"}}),
             "gate_config_json": None, "aggregator_config_json": None},
            {"id": "b1:n2", "node_id": "n2", "kind": "worker",
             "spec_json": json.dumps({"spec": {"objective": "Task B"}}),
             "gate_config_json": None, "aggregator_config_json": None},
        ]

        runner_mock.spawn_worker = AsyncMock()
        result = MagicMock()
        result.worker_id = "w_x"
        result.process = MagicMock()
        runner_mock.spawn_worker.return_value = result

        dispatched = await executor._dispatch_ready("b1")
        assert dispatched == 2
        assert runner_mock.spawn_worker.call_count == 2


class TestAcceptanceHumanApprovalGate:
    """Verify the orchestrator halts at a human_approval gate and waits."""

    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_gate_creates_approval_request_and_does_not_complete(self, executor, db_mock):
        """Human approval gate creates request but does NOT complete the node."""
        node = {
            "id": "b1:gate-1", "node_id": "gate-1",
            "gate_config_json": json.dumps({
                "predicate": "human_approval",
                "human_prompt": "Deploy to production?",
            }),
        }

        await executor._dispatch_gate("b1", node)

        # Should create approval_requests row
        approval_inserts = [c for c in db_mock.execute.call_args_list
                            if "approval_requests" in str(c[0][0])]
        assert len(approval_inserts) == 1

        # Should NOT complete or fail the node (no COMPLETED/FAILED state)
        completed_updates = [c for c in db_mock.execute.call_args_list
                             if "UPDATE dag_nodes" in str(c[0][0])
                             and NodeState.COMPLETED in str(c[0][1])]
        failed_updates = [c for c in db_mock.execute.call_args_list
                          if "UPDATE dag_nodes" in str(c[0][0])
                          and NodeState.FAILED in str(c[0][1])]
        assert len(completed_updates) == 0
        assert len(failed_updates) == 0

        # Node should be in RUNNING (waiting for PM)
        running_updates = [c for c in db_mock.execute.call_args_list
                           if "UPDATE dag_nodes" in str(c[0][0])
                           and NodeState.RUNNING in str(c[0][1])]
        assert len(running_updates) == 1


class TestAcceptanceFirstSuccessCancellation:
    """Verify that a first_success aggregator cancels the losing worker."""

    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_first_success_cancels_siblings(self, executor, db_mock):
        """When first_success aggregator fires, still-running siblings are cancelled."""
        fired_incoming = [{"from_node_id": "n1"}]
        all_incoming = [
            {"from_node_id": "n1"},
            {"from_node_id": "n2"},
            {"from_node_id": "n3"},
        ]

        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            fired_incoming,   # _dispatch_aggregator: fired incoming
            all_incoming,     # _cancel_aggregator_siblings: all incoming
            [],               # _process_node_completion: outgoing edges from agg node
            [],               # _check_bundle_completion -> _compute_bundle_status (no exit nodes)
            [{"state": NodeState.COMPLETED}],  # fallback all nodes
            [],               # _dispatch_ready: ready nodes
        ]
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"state": NodeState.COMPLETED, "output_json": '{"result": "done"}'},  # n1: completed
            {"state": NodeState.RUNNING, "worker_id": "w_n2"},  # n2: running
            {"state": NodeState.RUNNING, "worker_id": "w_n3"},  # n3: running
            {"cnt": 0},  # _dispatch_ready: running count
        ]

        node = {
            "id": "b1:agg-1", "node_id": "agg-1",
            "aggregator_config_json": json.dumps({
                "join": "first_success",
                "output_strategy": "first",
            }),
        }

        await executor._dispatch_aggregator("b1", node)

        # n2 and n3 should be cancelled
        cancel_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])
                        and NodeState.CANCELLED in str(c[0][1])]
        assert len(cancel_calls) == 2


class TestAcceptanceFirstSuccessFailedPredecessor:
    """FIRST_SUCCESS must only fire on successful predecessors, not failed ones."""

    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_first_worker_fails_second_succeeds_aggregator_fires_on_second(self, executor, db_mock):
        """FIRST_SUCCESS ignores failed predecessor, fires when a success arrives."""
        # Simulate: n1 failed (edge fired with always), n2 succeeds (edge fires)
        # After n1 failure: _try_make_ready sees 0 successful, not all fired → not ready
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "aggregator", "aggregator_config_json": '{"join": "first_success"}', "state": NodeState.PENDING},
            {"cnt": 1},  # fired count
            {"cnt": 2},  # total count
            {"cnt": 0},  # success_fired count (n1 failed)
        ]

        await executor._try_make_ready("b1", "agg-1")

        # Aggregator should NOT be marked ready (first predecessor failed)
        ready_calls = [c for c in db_mock.execute.call_args_list
                       if "UPDATE dag_nodes" in str(c[0][0])
                       and NodeState.READY in str(c[0][1])]
        assert len(ready_calls) == 0

    @pytest.mark.asyncio
    async def test_all_predecessors_fail_aggregator_fails(self, executor, db_mock):
        """When all predecessors fail and none succeed, FIRST_SUCCESS aggregator fails."""
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"kind": "aggregator", "aggregator_config_json": '{"join": "first_success"}', "state": NodeState.PENDING},
            {"cnt": 2},  # fired count (all fired)
            {"cnt": 2},  # total count
            {"cnt": 0},  # success_fired count (all failed)
        ]

        await executor._try_make_ready("b1", "agg-1")

        # Aggregator should be marked FAILED
        failed_calls = [c for c in db_mock.execute.call_args_list
                        if "UPDATE dag_nodes" in str(c[0][0])
                        and NodeState.FAILED in str(c[0][1])]
        assert len(failed_calls) == 1


class TestAcceptanceOnPropertyEdge:
    """Verify on_property edge conditions evaluate correctly."""

    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_on_property_edge_fires_when_condition_true(self, executor, db_mock):
        """Edge with on_property condition fires when property expression matches."""
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"output_json": json.dumps({"exit_code": 0, "outputs": {"score": 0.95}})},
            {"kind": "worker", "aggregator_config_json": None, "state": "pending"},
            {"cnt": 1},
            {"cnt": 1},
            {"cnt": 0},
        ]

        edges = [
            {"id": "e1", "to_node_id": "n2",
             "condition_kind": "on_property",
             "condition_expr": "exit_code == 0"},
        ]
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            edges,  # _process_node_completion: outgoing edges
            [],  # _check_bundle_completion -> _compute_bundle_status
            [{"state": NodeState.COMPLETED}],
            [],  # _dispatch_ready
        ]

        await executor._process_node_completion("b1", "n1", NodeState.COMPLETED)

        # Edge should be fired
        fire_calls = [c for c in db_mock.execute.call_args_list
                      if "dag_edges" in str(c[0][0])
                      and "fired = 1" in str(c[0][0])]
        assert len(fire_calls) == 1

    @pytest.mark.asyncio
    async def test_on_property_edge_does_not_fire_when_condition_false(self, executor, db_mock):
        """Edge with on_property condition does NOT fire when expression is false."""
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"output_json": json.dumps({"exit_code": 1, "outputs": {"score": 0.3}})},
        ]

        edges = [
            {"id": "e1", "to_node_id": "n2",
             "condition_kind": "on_property",
             "condition_expr": "exit_code == 0 && outputs.score > 0.8"},
        ]
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            edges,  # outgoing edges
            [],
            [{"state": NodeState.COMPLETED}],
            [],
        ]

        await executor._process_node_completion("b1", "n1", NodeState.COMPLETED)

        fire_calls = [c for c in db_mock.execute.call_args_list
                      if "dag_edges" in str(c[0][0])
                      and "fired = 1" in str(c[0][0])]
        assert len(fire_calls) == 0


class TestRenderMermaid:
    @pytest.fixture
    def executor(self, db_mock, sm_mock, runner_mock, rpc_handlers_mock, conn_mgr_mock):
        return DagExecutor(
            db=db_mock, sm=sm_mock, runner=runner_mock,
            rpc_handlers=rpc_handlers_mock, conn_mgr=conn_mgr_mock,
        )

    @pytest.mark.asyncio
    async def test_render_returns_mermaid_string(self, executor, db_mock):
        db_mock.fetch_all = AsyncMock()
        db_mock.fetch_all.side_effect = [
            [{"node_id": "n1", "kind": "worker", "state": "completed",
              "spec_json": '{"objective": "test"}'}],     # nodes
            [],                                              # edges
            [],                                              # expansions
        ]

        result = await executor.render_mermaid("b1")
        assert "```mermaid" in result
        assert "n1[" in result

