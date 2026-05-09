"""Tests for state_machine.py — 8 transition handlers, kernel_mode gating, error behavior."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from studio.orchestrator.state_machine import (
    BundleStateMachine,
    IllegalTransitionError,
    TERMINAL_STATES,
    _check_legal,
    _LEGAL_TRANSITIONS,
)
from studio.orchestrator.models import BundleState


class TestIllegalTransitionError:
    def test_str_formatting(self):
        err = IllegalTransitionError("proposed", "complete", "wrong path")
        assert "proposed -> complete" in str(err)
        assert "wrong path" in str(err)

    def test_to_jsonrpc_error(self):
        err = IllegalTransitionError("proposed", "complete", "wrong path")
        rpc = err.to_jsonrpc_error(request_id=42)
        assert rpc["jsonrpc"] == "2.0"
        assert rpc["id"] == 42
        assert rpc["error"]["code"] == -32001
        assert rpc["error"]["message"] == "illegal_transition"
        assert rpc["error"]["data"]["current_state"] == "proposed"
        assert rpc["error"]["data"]["attempted_transition"] == "complete"

    def test_to_jsonrpc_error_no_id(self):
        err = IllegalTransitionError("proposed", "rejected", "no")
        rpc = err.to_jsonrpc_error()
        assert rpc["id"] is None


class TestCheckLegal:
    def test_legal_transition_passes(self):
        _check_legal("(none)", BundleState.PROPOSED)

    def test_illegal_transition_raises(self):
        with pytest.raises(IllegalTransitionError, match="not legal in Phase 1"):
            _check_legal(BundleState.PROPOSED, BundleState.COMPLETE)

    def test_terminal_state_raises_specific_message(self):
        with pytest.raises(IllegalTransitionError, match="terminal states"):
            _check_legal(BundleState.COMPLETE, BundleState.IN_PROGRESS)


class TestBundleStateMachine:
    """Tests for all 8 Phase 1 transition handlers."""

    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.execute_insert = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.transaction = MagicMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def sm(self, db_mock):
        return BundleStateMachine(db_mock, kernel_mode=True)

    @pytest.fixture
    def sm_no_kernel(self, db_mock):
        return BundleStateMachine(db_mock, kernel_mode=False)

    # ── Transition 1: submit ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_1_submit(self, sm, db_mock):
        dag_nodes = [{"node_id": "n1", "kind": "worker", "spec": {}}]
        dag_edges = [{"from_node_id": "n1", "to_node_id": "n2", "condition": {"kind": "on_success"}}]

        await sm.transition_1_submit("bundle-1", "control-plane", {"idea": "test"}, dag_nodes, dag_edges)

        # Verify bundle INSERT
        call_args = db_mock.execute.call_args_list
        insert_bundle = call_args[0]
        assert insert_bundle[0][0].startswith("INSERT INTO bundles")
        assert insert_bundle[0][1][0] == "bundle-1"
        assert insert_bundle[0][1][2] == BundleState.PROPOSED

        # Verify DAG node INSERT
        insert_node = call_args[1]
        assert insert_node[0][0].startswith("INSERT INTO dag_nodes")
        assert insert_node[0][1][3] == "worker"

        # Verify DAG edge INSERT
        insert_edge = call_args[2]
        assert insert_edge[0][0].startswith("INSERT INTO dag_edges")

    @pytest.mark.asyncio
    async def test_transition_1_submit_default_kind_and_spec(self, sm, db_mock):
        dag_nodes = [{"node_id": "n1"}]
        dag_edges = []

        await sm.transition_1_submit("bundle-1", "repo", {}, dag_nodes, dag_edges)

        insert_node = db_mock.execute.call_args_list[1]
        assert insert_node[0][1][3] == "worker"  # default kind
        assert "spec_json" in str(insert_node[0])  # spec column exists
        assert "'{}'" in str(insert_node[0])  # empty spec stored as JSON

    # ── Transition 1a: kernel approve ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_1a_approve(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.PROPOSED}

        await sm.transition_1a_approve("bundle-1", "admin")

        # Verify state update
        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.APPROVED in update_call[0][1]
        assert "admin" in update_call[0][1]

        # Verify approval_decisions insert
        decision_call = db_mock.execute.call_args_list[1]
        assert decision_call[0][1][1] == "approved"

    @pytest.mark.asyncio
    async def test_transition_1a_requires_kernel_mode(self, sm_no_kernel, db_mock):
        with pytest.raises(IllegalTransitionError, match="kernel_mode"):
            await sm_no_kernel.transition_1a_approve("bundle-1", "admin")

    @pytest.mark.asyncio
    async def test_transition_1a_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_1a_approve("missing-bundle", "admin")

    @pytest.mark.asyncio
    async def test_transition_1a_wrong_current_state(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}
        with pytest.raises(IllegalTransitionError):
            await sm.transition_1a_approve("bundle-1", "admin")

    # ── Transition 1b: kernel reject ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_1b_reject(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.PROPOSED}

        await sm.transition_1b_reject("bundle-1", "admin", "not needed")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.REJECTED in update_call[0][1]

        decision_call = db_mock.execute.call_args_list[1]
        assert decision_call[0][1][1] == "rejected"

    @pytest.mark.asyncio
    async def test_transition_1b_default_reason(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.PROPOSED}
        await sm.transition_1b_reject("bundle-1", "admin")
        decision_call = db_mock.execute.call_args_list[1]
        assert "rejected via CLI" in decision_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_1b_requires_kernel_mode(self, sm_no_kernel, db_mock):
        with pytest.raises(IllegalTransitionError, match="kernel_mode"):
            await sm_no_kernel.transition_1b_reject("bundle-1", "admin")

    @pytest.mark.asyncio
    async def test_transition_1b_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_1b_reject("missing-bundle", "admin")

    # ── Transition 6: execution start ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_6_start_execution(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.APPROVED}

        await sm.transition_6_start_execution("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.IN_PROGRESS in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_6_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_6_start_execution("missing-bundle")

    @pytest.mark.asyncio
    async def test_transition_6_wrong_state(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.PROPOSED}
        with pytest.raises(IllegalTransitionError):
            await sm.transition_6_start_execution("bundle-1")

    # ── Transition 9: all exit nodes terminal ─────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_9_to_verifying(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}

        await sm.transition_9_to_verifying("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.VERIFYING in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_9_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_9_to_verifying("missing-bundle")

    # ── Transition 17: verification passed ────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_17_complete(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}

        await sm.transition_17_complete("bundle-1", {"status": "shipped"})

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.COMPLETE in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_17_default_outcome(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}

        await sm.transition_17_complete("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        # Default outcome should be {"status": "shipped"}
        assert "shipped" in str(update_call[0][1])

    @pytest.mark.asyncio
    async def test_transition_17_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_17_complete("missing-bundle")

    # ── Transition 19: verification failed ────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_19_fail_verification(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}

        await sm.transition_19_fail_verification("bundle-1", "tests didn't pass")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.FAILED in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_19_empty_reason(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}

        await sm.transition_19_fail_verification("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        assert "failed_verification" in str(update_call[0][1])

    @pytest.mark.asyncio
    async def test_transition_19_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_19_fail_verification("missing-bundle")

    # ── Transition 25: execution failure ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_transition_25_fail_execution(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}

        await sm.transition_25_fail_execution("bundle-1", "DAG crash")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.FAILED in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_25_default_reason(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}

        await sm.transition_25_fail_execution("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        assert "unrecoverable DAG failure" in str(update_call[0][1])

    @pytest.mark.asyncio
    async def test_transition_25_bundle_not_found(self, sm, db_mock):
        db_mock.fetch_one.return_value = None
        with pytest.raises(IllegalTransitionError, match="not found"):
            await sm.transition_25_fail_execution("missing-bundle")

    # ── Audit logging ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_audit_is_called_on_transition(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.PROPOSED}

        await sm.transition_1a_approve("bundle-1", "admin")

        # Find the audit INSERT (last execute call)
        audit_call = db_mock.execute.call_args_list[-1]
        assert "audit_log" in audit_call[0][0]

    # ── Terminal states check ─────────────────────────────────────────────

    def test_terminal_states_are_complete_set(self):
        assert BundleState.COMPLETE in TERMINAL_STATES
        assert BundleState.PARKED in TERMINAL_STATES
        assert BundleState.FAILED in TERMINAL_STATES
        assert BundleState.REJECTED in TERMINAL_STATES
        assert BundleState.ABORTED in TERMINAL_STATES

    def test_non_terminal_not_in_set(self):
        assert BundleState.PROPOSED not in TERMINAL_STATES
        assert BundleState.IN_PROGRESS not in TERMINAL_STATES
        assert BundleState.VERIFYING not in TERMINAL_STATES
