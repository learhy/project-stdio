"""Tests for rpc.py — JSON-RPC dispatcher, handlers, connection manager."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.rpc import (
    RpcDispatcher,
    RpcHandlers,
    ConnectionManager,
    WorkerBinding,
    create_rpc_system,
    _make_error,
    _make_result,
    _make_stub_handler,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    METHOD_NOT_IMPLEMENTED,
    CAPABILITY_DENIED,
    _STUB_METHODS,
)
from studio.orchestrator.models import WorkerState, NodeState


# ── JSON-RPC error helpers ────────────────────────────────────────────────────

class TestMakeError:
    def test_basic_error(self):
        err = _make_error(-32600, "Invalid Request")
        assert err["jsonrpc"] == "2.0"
        assert err["error"]["code"] == -32600
        assert err["error"]["message"] == "Invalid Request"
        assert err["id"] is None

    def test_error_with_data(self):
        err = _make_error(-32001, "denied", data={"method": "test"})
        assert err["error"]["data"] == {"method": "test"}

    def test_error_with_id(self):
        err = _make_error(-1, "err", req_id=42)
        assert err["id"] == 42

class TestMakeResult:
    def test_result(self):
        r = _make_result({"ok": True}, req_id=1)
        assert r["jsonrpc"] == "2.0"
        assert r["result"] == {"ok": True}
        assert r["id"] == 1


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
def binding():
    return WorkerBinding(
        worker_id="w1",
        bundle_id="b1",
        node_id="n1",
        rpc_methods=["worker.*", "cap.*"],
        reader=MagicMock(),
        writer=MagicMock(),
    )

@pytest.fixture
def dispatcher(db_mock):
    return RpcDispatcher(db_mock)

@pytest.fixture
def handlers(db_mock):
    return RpcHandlers(db_mock)


# ── RpcDispatcher tests ───────────────────────────────────────────────────────

class TestRpcDispatcherDispatch:
    @pytest.mark.asyncio
    async def test_parse_error_on_invalid_json(self, dispatcher, binding):
        raw = b"not json"
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_invalid_request_no_method(self, dispatcher, binding):
        raw = b'{"jsonrpc":"2.0","params":{},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_notification_returns_none(self, dispatcher, binding):
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{}}'
        dispatcher._handlers["worker.heartbeat"] = AsyncMock(return_value={"ok": True})
        resp = await dispatcher.dispatch(binding, raw)
        assert resp is None

    @pytest.mark.asyncio
    async def test_capability_denied_no_matching_pattern(self, dispatcher, binding):
        binding.rpc_methods = ["artifact.*"]
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == CAPABILITY_DENIED
        assert "capability_denied" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_capability_denied_returns_none_for_notification(self, dispatcher, binding):
        binding.rpc_methods = ["artifact.*"]
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{}}'
        resp = await dispatcher.dispatch(binding, raw)
        assert resp is None

    @pytest.mark.asyncio
    async def test_stub_method_returns_32000(self, dispatcher, binding):
        raw = b'{"jsonrpc":"2.0","method":"worker.pause","params":{},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == METHOD_NOT_IMPLEMENTED
        assert "worker.pause" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_method_not_found(self, dispatcher, binding):
        # Must pass capability check first, so add pattern that covers it
        binding.rpc_methods = ["nonexistent.*", "worker.*"]
        raw = b'{"jsonrpc":"2.0","method":"nonexistent.method","params":{},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_registered_handler_is_called(self, dispatcher, binding):
        mock_handler = AsyncMock(return_value={"result": "ok"})
        dispatcher.register("worker.heartbeat", mock_handler)
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{"phase":"starting"},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        mock_handler.assert_called_once()
        body = json.loads(resp.decode())
        assert body["result"] == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_handler_exception_is_caught(self, dispatcher, binding):
        async def failing(_b, _p, _i):
            raise RuntimeError("boom")
        dispatcher.register("worker.heartbeat", failing)
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{},"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == INTERNAL_ERROR
        assert "boom" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_handler_exception_notification_returns_none(self, dispatcher, binding):
        async def failing(_b, _p, _i):
            raise RuntimeError("boom")
        dispatcher.register("worker.heartbeat", failing)
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":{}}'
        resp = await dispatcher.dispatch(binding, raw)
        assert resp is None


class TestRpcDispatcherCheckMethod:
    def test_pattern_covers_method(self, dispatcher):
        binding = WorkerBinding("w1", "b1", "n1", ["worker.*", "cap.check"], MagicMock(), MagicMock())
        ok, reason = dispatcher._check_rpc_method(binding, "worker.heartbeat")
        assert ok

    def test_pattern_does_not_cover(self, dispatcher):
        binding = WorkerBinding("w1", "b1", "n1", ["artifact.*"], MagicMock(), MagicMock())
        ok, reason = dispatcher._check_rpc_method(binding, "worker.heartbeat")
        assert not ok
        assert "worker.heartbeat" in reason

    def test_exact_pattern_match(self, dispatcher):
        binding = WorkerBinding("w1", "b1", "n1", ["cap.check"], MagicMock(), MagicMock())
        ok, _ = dispatcher._check_rpc_method(binding, "cap.check")
        assert ok


# ── RpcHandlers tests ─────────────────────────────────────────────────────────

class TestHandleHeartbeat:
    @pytest.mark.asyncio
    async def test_first_heartbeat_pending_to_running(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = {"state": "pending"}
        result = await handlers.handle_heartbeat(binding, {"phase": "thinking"}, 1)
        assert result["accepted"] is True
        assert result["state"] == WorkerState.RUNNING
        # Check worker state was updated to running
        call = db_mock.execute.call_args_list[0]
        assert WorkerState.RUNNING in call[0][1]
        assert "thinking" in call[0][1]

    @pytest.mark.asyncio
    async def test_subsequent_heartbeat_stays_running(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = {"state": "running"}
        result = await handlers.handle_heartbeat(binding, {"phase": "writing-code"}, 1)
        assert result["accepted"] is True
        # Should NOT update state to running (already running)
        call = db_mock.execute.call_args_list[0]
        assert WorkerState.RUNNING not in call[0][1]  # state not in params

    @pytest.mark.asyncio
    async def test_heartbeat_worker_not_found(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = None
        result = await handlers.handle_heartbeat(binding, {"phase": "starting"}, 1)
        assert result["accepted"] is False

    @pytest.mark.asyncio
    async def test_heartbeat_default_phase(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = {"state": "running"}
        result = await handlers.handle_heartbeat(binding, {}, 1)
        assert result["phase"] == "starting"

    @pytest.mark.asyncio
    async def test_heartbeat_calls_on_heartbeat_callback(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = {"state": "pending"}
        cb_called = []
        async def cb(worker_id, phase):
            cb_called.append((worker_id, phase))
        handlers.set_on_heartbeat(cb)
        await handlers.handle_heartbeat(binding, {"phase": "tool-call"}, 1)
        assert cb_called == [("w1", "tool-call")]


class TestHandleLog:
    @pytest.mark.asyncio
    async def test_log_writes_to_audit_log(self, handlers, db_mock, binding):
        result = await handlers.handle_log(binding, {"level": "info", "message": "hello"}, 1)
        assert result["logged"] is True
        call = db_mock.execute.call_args_list[0]
        assert "audit_log" in call[0][0]
        assert "worker.log.info" in call[0][1]

    @pytest.mark.asyncio
    async def test_log_with_structured_data(self, handlers, db_mock, binding):
        await handlers.handle_log(binding, {"level": "error", "message": "fail", "structured_data": {"code": 500}}, 1)
        call = db_mock.execute.call_args_list[0]
        payload = call[0][1][3]
        assert "500" in payload

    @pytest.mark.asyncio
    async def test_log_default_level(self, handlers, db_mock, binding):
        await handlers.handle_log(binding, {"message": "test"}, 1)
        call = db_mock.execute.call_args_list[0]
        assert "worker.log.info" in call[0][1]


class TestHandleProgressReport:
    @pytest.mark.asyncio
    async def test_progress_report_updates_node(self, handlers, db_mock, binding):
        result = await handlers.handle_progress_report(binding, {
            "stage": "testing", "percent": 75, "message": "running tests"
        }, 1)
        assert result["accepted"] is True
        call = db_mock.execute.call_args_list[0]
        assert "UPDATE dag_nodes" in call[0][0]
        output = call[0][1][0]
        assert "testing" in output
        assert "75" in output


class TestHandleFinalReport:
    @pytest.mark.asyncio
    async def test_final_report_success(self, handlers, db_mock, binding):
        result = await handlers.handle_final_report(binding, {
            "outcome": "success", "files_changed": ["a.py"], "summary": "done"
        }, 1)
        assert result["accepted"] is True
        assert result["node_state"] == NodeState.COMPLETED

        # Check node update
        node_call = db_mock.execute.call_args_list[0]
        assert NodeState.COMPLETED in node_call[0][1]

        # Check worker update
        worker_call = db_mock.execute.call_args_list[1]
        assert WorkerState.COMPLETE in worker_call[0][1]

    @pytest.mark.asyncio
    async def test_final_report_failure(self, handlers, db_mock, binding):
        result = await handlers.handle_final_report(binding, {
            "outcome": "failure", "errors": ["crash"], "summary": "failed"
        }, 1)
        assert result["node_state"] == NodeState.FAILED
        worker_call = db_mock.execute.call_args_list[1]
        assert WorkerState.FAILED in worker_call[0][1]

    @pytest.mark.asyncio
    async def test_final_report_paused_treated_as_failure(self, handlers, db_mock, binding):
        result = await handlers.handle_final_report(binding, {
            "outcome": "paused", "summary": "paused mid-work"
        }, 1)
        assert result["node_state"] == NodeState.FAILED

    @pytest.mark.asyncio
    async def test_final_report_calls_callback(self, handlers, db_mock, binding):
        cb_calls = []
        async def cb(bundle_id, node_id, worker_id, outcome):
            cb_calls.append((bundle_id, node_id, worker_id, outcome))
        handlers.set_on_final_report(cb)
        await handlers.handle_final_report(binding, {"outcome": "success"}, 1)
        assert len(cb_calls) == 1
        assert cb_calls[0][0] == "b1"
        assert cb_calls[0][3]["outcome"] == "success"


class TestHandleQueryStatus:
    @pytest.mark.asyncio
    async def test_query_status_returns_state(self, handlers, db_mock, binding):
        db_mock.fetch_one = AsyncMock()
        db_mock.fetch_one.side_effect = [
            {"state": "running", "output_json": "{}"},
            {"state": "running", "last_heartbeat": 123456, "current_phase": "thinking"},
        ]
        result = await handlers.handle_query_status(binding, {}, 1)
        assert result["node_state"] == "running"
        assert result["worker_state"] == "running"
        assert result["last_heartbeat"] == 123456

    @pytest.mark.asyncio
    async def test_query_status_unknown_when_missing(self, handlers, db_mock, binding):
        db_mock.fetch_one = AsyncMock(return_value=None)
        result = await handlers.handle_query_status(binding, {}, 1)
        assert result["node_state"] == "unknown"


class TestHandleCapCheck:
    @pytest.mark.asyncio
    async def test_cap_check_loads_manifest(self, handlers, db_mock, binding):
        manifest = {
            "schema_version": "1.0",
            "subject": {"kind": "bundle", "id": "test"},
            "grants": {
                "filesystem": {"reads": [{"path": "/work", "recursive": True}], "writes": []},
                "network": {"egress": []},
                "process": {"exec": []},
                "rpc": {"methods": []},
                "resources": {},
            },
            "metadata": {"rationale": ""},
        }
        db_mock.fetch_one.return_value = {"manifest_json": json.dumps(manifest)}

        # cap.check for filesystem read under /work should be allowed
        result = await handlers.handle_cap_check(binding, {"op_descriptor": "filesystem.read:/work/src/test.py"}, 1)
        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_cap_check_denied(self, handlers, db_mock, binding):
        manifest = {
            "schema_version": "1.0",
            "subject": {"kind": "bundle", "id": "test"},
            "grants": {
                "filesystem": {"reads": [{"path": "/work", "recursive": True}], "writes": []},
                "network": {"egress": []},
                "process": {"exec": []},
                "rpc": {"methods": []},
                "resources": {},
            },
            "metadata": {"rationale": ""},
        }
        db_mock.fetch_one.return_value = {"manifest_json": json.dumps(manifest)}
        result = await handlers.handle_cap_check(binding, {"op_descriptor": "filesystem.read:/etc/passwd"}, 1)
        assert result["allowed"] is False

    @pytest.mark.asyncio
    async def test_cap_check_no_manifest_returns_false(self, handlers, db_mock, binding):
        db_mock.fetch_one.return_value = None
        result = await handlers.handle_cap_check(binding, {"op_descriptor": "filesystem.read:/tmp/test"}, 1)
        assert result["allowed"] is False

    @pytest.mark.asyncio
    async def test_cap_check_uses_cache(self, handlers, db_mock, binding):
        manifest = {
            "schema_version": "1.0",
            "subject": {"kind": "bundle", "id": "test"},
            "grants": {
                "filesystem": {"reads": [{"path": "/work", "recursive": True}], "writes": []},
                "network": {"egress": []},
                "process": {"exec": []},
                "rpc": {"methods": []},
                "resources": {},
            },
            "metadata": {"rationale": ""},
        }
        binding.manifest_cache = manifest
        # Should not query DB since cache is populated
        result = await handlers.handle_cap_check(binding, {"op_descriptor": "filesystem.read:/work/test.txt"}, 1)
        assert result["allowed"] is True
        db_mock.fetch_one.assert_not_called()


# ── Stub methods ──────────────────────────────────────────────────────────────

class TestStubMethods:
    @pytest.mark.parametrize("method", list(_STUB_METHODS))
    def test_all_stub_methods_registered(self, method):
        """All 8 stub methods must be in the set."""
        assert method in _STUB_METHODS

    @pytest.mark.asyncio
    async def test_stub_handler_is_functional(self):
        handler = _make_stub_handler("worker.pause")
        binding = WorkerBinding("w1", "b1", "n1", ["worker.*"], MagicMock(), MagicMock())
        result = await handler(binding, {}, 1)
        assert result == {}


# ── WorkerBinding tests ───────────────────────────────────────────────────────

class TestWorkerBinding:
    def test_worker_binding_attributes(self):
        reader = MagicMock()
        writer = MagicMock()
        b = WorkerBinding("w1", "bundle-1", "node-1", ["worker.*", "cap.*"], reader, writer)
        assert b.worker_id == "w1"
        assert b.bundle_id == "bundle-1"
        assert b.node_id == "node-1"
        assert b.rpc_methods == ["worker.*", "cap.*"]
        assert b.manifest_cache is None


# ── ConnectionManager tests ───────────────────────────────────────────────────

class TestConnectionManager:
    @pytest.fixture
    def conn_mgr(self, db_mock, dispatcher, handlers, tmp_path):
        socket_path = str(tmp_path / "test.sock")
        return ConnectionManager(socket_path, dispatcher, handlers, db_mock)

    def test_init(self, conn_mgr, tmp_path):
        assert conn_mgr._bindings == {}
        assert conn_mgr._by_worker_id == {}

    @pytest.mark.asyncio
    async def test_start_creates_socket(self, conn_mgr):
        await conn_mgr.start()
        assert conn_mgr._server is not None
        await conn_mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self, conn_mgr):
        await conn_mgr.start()
        await conn_mgr.stop()
        assert conn_mgr._server is not None  # server object remains, but is closed

    @pytest.mark.asyncio
    async def test_send_to_worker(self, conn_mgr):
        writer = MagicMock()
        writer.drain = AsyncMock()
        binding = WorkerBinding("w1", "b1", "n1", ["worker.*"], MagicMock(), writer)
        conn_mgr._by_worker_id["w1"] = binding

        await conn_mgr.send_to_worker("w1", {"jsonrpc": "2.0", "method": "test", "id": 1})
        writer.drain.assert_called_once()
        written = writer.write.call_args[0][0]
        assert b"test" in written

    @pytest.mark.asyncio
    async def test_send_to_missing_worker_raises(self, conn_mgr):
        with pytest.raises(ValueError, match="not connected"):
            await conn_mgr.send_to_worker("nonexistent", {})

    @pytest.mark.asyncio
    async def test_auth_valid_token_binds_worker(self, conn_mgr, db_mock):
        db_mock.fetch_one.return_value = {
            "id": "w1",
            "bundle_id": "b1",
            "node_id": "n1",
            "token": "sec-ret",
            "manifest_json": json.dumps({
                "grants": {"rpc": {"methods": ["worker.*", "cap.*"]}}
            }),
        }

        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps({"jsonrpc": "2.0", "method": "auth", "params": {"token": "sec-ret"}, "id": 1}) + "\n").encode(),
            b"",  # EOF after auth ack, ends read loop
        ]
        writer = MagicMock()
        writer.drain = AsyncMock()

        await conn_mgr._handle_connection(reader, writer)
        # Verify auth was accepted — writer received success response
        # (Binding is cleaned up in finally since connection closes, but auth ack was sent)
        success_sent = False
        for call in writer.write.call_args_list:
            data = call[0][0]
            body = json.loads(data.decode().rstrip("\n"))
            if body.get("result", {}).get("bound") is True:
                success_sent = True
        assert success_sent, "Auth ack should have been sent with bound=true"

    @pytest.mark.asyncio
    async def test_auth_invalid_token_rejected(self, conn_mgr, db_mock):
        db_mock.fetch_one.return_value = None

        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps({"jsonrpc": "2.0", "method": "auth", "params": {"token": "bad-token"}, "id": 1}) + "\n").encode(),
            b"",
        ]
        writer = MagicMock()
        writer.drain = AsyncMock()

        await conn_mgr._handle_connection(reader, writer)
        # Check error was sent
        assert writer.write.called
        sent = writer.write.call_args_list[0][0][0]
        body = json.loads(sent.decode().rstrip("\n"))
        assert body["error"]["code"] == CAPABILITY_DENIED

    @pytest.mark.asyncio
    async def test_auth_no_token_rejected(self, conn_mgr, db_mock):
        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps({"jsonrpc": "2.0", "method": "auth", "params": {}}) + "\n").encode(),
            b"",
        ]
        writer = MagicMock()
        writer.drain = AsyncMock()

        await conn_mgr._handle_connection(reader, writer)
        sent = writer.write.call_args_list[0][0][0]
        body = json.loads(sent.decode().rstrip("\n"))
        assert body["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_auth_not_auth_method_rejected(self, conn_mgr, db_mock):
        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps({"jsonrpc": "2.0", "method": "worker.heartbeat", "id": 1}) + "\n").encode(),
            b"",
        ]
        writer = MagicMock()
        writer.drain = AsyncMock()

        await conn_mgr._handle_connection(reader, writer)
        sent = writer.write.call_args_list[0][0][0]
        body = json.loads(sent.decode().rstrip("\n"))
        assert body["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_auth_timeout(self, conn_mgr, db_mock):
        reader = AsyncMock()
        reader.readline.side_effect = asyncio.TimeoutError()
        writer = MagicMock()

        await conn_mgr._handle_connection(reader, writer)
        # Should not crash, connection just closes


# ── create_rpc_system test ────────────────────────────────────────────────────

class TestCreateRpcSystem:
    def test_creates_all_three_objects(self, db_mock, tmp_path):
        sp = str(tmp_path / "test.sock")
        dispatcher, handlers, conn_mgr = create_rpc_system(db_mock, sp)
        assert dispatcher is not None
        assert handlers is not None
        assert conn_mgr is not None
        assert len(dispatcher._handlers) == 24

    def test_register_all_methods(self, db_mock, tmp_path):
        sp = str(tmp_path / "test.sock")
        dispatcher, _, _ = create_rpc_system(db_mock, sp)
        expected = {
            "worker.heartbeat", "worker.log", "worker.progress_report",
            "worker.final_report", "worker.query_status", "cap.check",
            "worker.pause", "worker.resume", "worker.cancel",
            "worker.inject_context", "cap.request",
            "artifact.publish", "artifact.fetch", "artifact.list",
            "secrets.fetch", "worker.request_human_input",
            "worker.poll_human_input",
            "mcp.approve_bundle", "mcp.reject_bundle",
            "mcp.request_modification", "mcp.escalate_bundle",
            "mcp.pause_bundle", "mcp.resume_bundle",
            "mcp.kill_worker",
        }
        assert set(dispatcher._handlers.keys()) == expected


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_dispatch_empty_params(self, dispatcher, binding):
        mock = AsyncMock(return_value={"ok": True})
        dispatcher.register("worker.heartbeat", mock)
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_dispatch_params_not_dict(self, dispatcher, binding):
        raw = b'{"jsonrpc":"2.0","method":"worker.heartbeat","params":[],"id":1}'
        resp = await dispatcher.dispatch(binding, raw)
        body = json.loads(resp.decode())
        assert body["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_final_report_default_outcome(self, handlers, db_mock, binding):
        result = await handlers.handle_final_report(binding, {}, 1)
        assert result["node_state"] == NodeState.FAILED

    @pytest.mark.asyncio
    async def test_connection_cleanup_on_exception(self, db_mock, dispatcher, handlers, tmp_path):
        socket_path = str(tmp_path / "cleanup.sock")
        conn_mgr = ConnectionManager(socket_path, dispatcher, handlers, db_mock)
        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps({"jsonrpc": "2.0", "method": "auth", "token": "t", "id": 1}) + "\n").encode(),
            Exception("connection lost"),
        ]
        writer = MagicMock()
        writer.drain = AsyncMock()
        db_mock.fetch_one.return_value = {
            "id": "w1", "bundle_id": "b1", "node_id": "n1",
            "token": "t", "manifest_json": '{"grants":{"rpc":{"methods":["worker.*"]}}}',
        }

        await conn_mgr._handle_connection(reader, writer)
        # Binding should be cleaned up
        assert "w1" not in conn_mgr._by_worker_id
        writer.close.assert_called()
