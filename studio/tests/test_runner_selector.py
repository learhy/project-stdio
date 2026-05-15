"""Tests for RunnerSelector and capability_to_runner_compatibility (Bundle 4.4)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.models import (
    CapabilityManifest,
    ProcessGrants,
    Grants,
    RunnerSelectorSettings,
)
from studio.orchestrator.runner import (
    capability_to_runner_compatibility,
    RunnerSelector,
    LocalBwrapWorkerRunner,
    RemoteSSHWorkerRunner,
    K8sJobWorkerRunner,
    WorkerSpawnResult,
    RemoteWorkerHandle,
    K8sWorkerHandle,
)


def make_manifest(exec_grants=None):
    """Build a minimal capability manifest, optionally with exec grants."""
    grants = Grants()
    if exec_grants:
        grants.process = ProcessGrants(exec=exec_grants)
    return CapabilityManifest(
        schema_version="1.0",
        grants=grants,
    )


class TestCapabilityToRunnerCompatibility:
    def test_all_runners_compatible_by_default(self):
        compat = capability_to_runner_compatibility(make_manifest())
        for name in ("local", "remote_ssh", "k8s"):
            assert compat[name]["compatible"] is True

    def test_no_unenforced_grants_without_exec(self):
        compat = capability_to_runner_compatibility(make_manifest())
        for name in ("local", "remote_ssh", "k8s"):
            assert compat[name]["unenforced_grants"] == []

    def test_k8s_reports_exec_allowlist_unenforced(self):
        from studio.orchestrator.models import ExecGrant
        manifest = make_manifest(exec_grants=[
            ExecGrant(binary="python", rationale="test"),
        ])
        compat = capability_to_runner_compatibility(manifest)
        assert compat["k8s"]["unenforced_grants"] == ["exec_allowlist"]
        assert compat["local"]["unenforced_grants"] == []
        assert compat["remote_ssh"]["unenforced_grants"] == []

    def test_multiple_exec_grants_still_single_unenforced(self):
        from studio.orchestrator.models import ExecGrant
        manifest = make_manifest(exec_grants=[
            ExecGrant(binary="python"),
            ExecGrant(binary="node"),
        ])
        compat = capability_to_runner_compatibility(manifest)
        assert compat["k8s"]["unenforced_grants"] == ["exec_allowlist"]


class TestRunnerSelectorSelection:
    def _make_selector(self, **kwargs):
        """Build a RunnerSelector with one or more mock runners."""
        settings = RunnerSelectorSettings(**kwargs)
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        return RunnerSelector(db, settings, local=local), db, local

    def test_default_preference_resolves_to_local(self):
        sel, _, _ = self._make_selector(default_preference="any")
        assert sel._default_preference() == "local"

    def test_explicit_default_preference(self):
        sel, _, _ = self._make_selector(default_preference="k8s")
        assert sel._default_preference() == "k8s"

    def test_select_local_when_preferred(self):
        sel, _, local = self._make_selector()
        runner_type, runner = sel._select_runner("local")
        assert runner_type == "local"
        assert runner is local

    def test_select_any_falls_back_to_default(self):
        sel, _, local = self._make_selector()
        runner_type, runner = sel._select_runner("any")
        assert runner_type == "local"
        assert runner is local

    def test_unknown_preference_treated_as_any(self):
        sel, _, local = self._make_selector()
        runner_type, runner = sel._select_runner("nonexistent")
        assert runner_type == "local"
        assert runner is local

    def test_no_runners_available(self):
        settings = RunnerSelectorSettings()
        sel = RunnerSelector(MagicMock(), settings)
        runner_type, runner = sel._select_runner("local")
        assert runner_type == ""
        assert runner is None

    def test_preference_falls_back_when_runner_missing(self):
        sel, _, local = self._make_selector()
        runner_type, runner = sel._select_runner("k8s")
        assert runner_type == "local"
        assert runner is local

    def test_k8s_blocked_by_unenforced_grants(self):
        """When allow_unenforced_grants=False, k8s is skipped if manifest has exec grants."""
        from studio.orchestrator.models import ExecGrant
        settings = RunnerSelectorSettings(allow_unenforced_grants=False)
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, k8s=k8s)

        manifest = make_manifest(exec_grants=[ExecGrant(binary="python")])
        runner_type, runner = sel._select_runner("k8s", manifest=manifest)
        # k8s should be skipped, falls back to local
        assert runner_type == "local"
        assert runner is local

    def test_k8s_allowed_when_unenforced_grants_permitted(self):
        """When allow_unenforced_grants=True, k8s is selected despite exec grants."""
        from studio.orchestrator.models import ExecGrant
        settings = RunnerSelectorSettings(allow_unenforced_grants=True)
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, k8s=k8s)

        manifest = make_manifest(exec_grants=[ExecGrant(binary="python")])
        runner_type, runner = sel._select_runner("k8s", manifest=manifest)
        assert runner_type == "k8s"
        assert runner is k8s

    def test_k8s_ok_without_exec_grants(self):
        """k8s is fine when manifest has no exec grants."""
        settings = RunnerSelectorSettings(allow_unenforced_grants=False)
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, k8s=k8s)

        manifest = make_manifest()  # no exec grants
        runner_type, runner = sel._select_runner("k8s", manifest=manifest)
        assert runner_type == "k8s"
        assert runner is k8s

    def test_explicit_preference_over_any(self):
        """If user says 'remote_ssh', prefer that over the 'any' default."""
        settings = RunnerSelectorSettings(default_preference="any")
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh, k8s=k8s)

        runner_type, runner = sel._select_runner("remote_ssh")
        assert runner_type == "remote_ssh"
        assert runner is ssh


class TestRunnerSelectorSpawnWorker:
    def _make_selector_with_all(self, **settings_kwargs):
        settings = RunnerSelectorSettings(**settings_kwargs)
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh, k8s=k8s)
        return sel, db, local, ssh, k8s

    @pytest.mark.asyncio
    async def test_spawn_routes_to_local_by_default(self):
        sel, db, local, ssh, k8s = self._make_selector_with_all()
        expected = WorkerSpawnResult(
            worker_id="w1", token="tok", node_id="n1",
            process=MagicMock(spec=asyncio.subprocess.Process),
        )
        local.spawn_worker = AsyncMock(return_value=expected)

        result = await sel.spawn_worker("w1", "b1", "n1", make_manifest(), "/tmp/work")

        local.spawn_worker.assert_called_once()
        assert result is expected
        # runner_type should be recorded
        db.execute.assert_any_call(
            "UPDATE workers SET runner_type = ? WHERE id = ?",
            ("local", "w1"),
        )

    @pytest.mark.asyncio
    async def test_spawn_routes_by_preference(self):
        sel, db, local, ssh, k8s = self._make_selector_with_all()
        expected = WorkerSpawnResult(
            worker_id="w2", token="tok", node_id="n2",
            process=MagicMock(spec=K8sWorkerHandle),
        )
        k8s.spawn_worker = AsyncMock(return_value=expected)

        result = await sel.spawn_worker(
            "w2", "b1", "n2", make_manifest(), "/tmp/work",
            task_spec={"runner_preference": "k8s"},
        )

        k8s.spawn_worker.assert_called_once()
        assert result is expected
        db.execute.assert_any_call(
            "UPDATE workers SET runner_type = ? WHERE id = ?",
            ("k8s", "w2"),
        )

    @pytest.mark.asyncio
    async def test_spawn_returns_error_when_no_compatible_runner(self):
        settings = RunnerSelectorSettings(allow_unenforced_grants=False)
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        sel = RunnerSelector(db, settings)

        result = await sel.spawn_worker("w3", "b1", "n3", make_manifest(), "/tmp/work")
        assert result.error
        assert "No compatible runner" in result.error

    @pytest.mark.asyncio
    async def test_spawn_records_audit_log(self):
        sel, db, local, ssh, k8s = self._make_selector_with_all()
        local.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w4", token="tok", node_id="n4",
            process=MagicMock(spec=asyncio.subprocess.Process),
        ))

        await sel.spawn_worker("w4", "b1", "n4", make_manifest(), "/tmp/work")

        # Audit log should contain runner_selected
        audit_calls = [
            c for c in db.execute.call_args_list
            if "audit_log" in str(c.args[0])
        ]
        assert len(audit_calls) == 1
        # The event_type is the first positional param
        assert audit_calls[0].args[1][0] == "runner_selected"


class TestRunnerSelectorKillWorker:
    def _make_selector(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        local.kill_worker = AsyncMock()
        ssh.kill_worker = AsyncMock()
        k8s.kill_worker = AsyncMock()
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh, k8s=k8s)
        return sel, local, ssh, k8s

    @pytest.mark.asyncio
    async def test_kill_dispatches_to_local(self):
        sel, local, ssh, k8s = self._make_selector()
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        await sel.kill_worker(proc, "w1")
        local.kill_worker.assert_called_once_with(proc, "w1")
        ssh.kill_worker.assert_not_called()
        k8s.kill_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_dispatches_to_ssh(self):
        sel, local, ssh, k8s = self._make_selector()
        handle = MagicMock(spec=RemoteWorkerHandle)
        await sel.kill_worker(handle, "w2")
        ssh.kill_worker.assert_called_once_with(handle, "w2")
        local.kill_worker.assert_not_called()
        k8s.kill_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_dispatches_to_k8s(self):
        sel, local, ssh, k8s = self._make_selector()
        handle = MagicMock(spec=K8sWorkerHandle)
        await sel.kill_worker(handle, "w3")
        k8s.kill_worker.assert_called_once_with(handle, "w3")
        local.kill_worker.assert_not_called()
        ssh.kill_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_noop_when_runner_missing(self):
        """kill_worker should not crash when the target runner isn't available."""
        settings = RunnerSelectorSettings()
        sel = RunnerSelector(MagicMock(), settings)  # no runners at all
        proc = MagicMock(spec=asyncio.subprocess.Process)
        # Should not raise
        await sel.kill_worker(proc, "w4")


class TestRunnerSelectorClose:
    @pytest.mark.asyncio
    async def test_close_closes_all_runners(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        local.close = AsyncMock()
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        ssh.close = AsyncMock()
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        k8s.close = AsyncMock()
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh, k8s=k8s)

        await sel.close()
        local.close.assert_called_once()
        ssh.close.assert_called_once()
        k8s.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_errors_gracefully(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        local.close = AsyncMock(side_effect=RuntimeError("boom"))
        sel = RunnerSelector(db, settings, local=local)

        # Should not raise
        await sel.close()


class TestRunnerSelectorMixedFleet:
    """Integration-style tests for the full mixed-fleet flow."""
    @pytest.mark.asyncio
    async def test_all_three_runners_used_in_sequence(self):
        """Three spawns with different preferences hit the right runners."""
        settings = RunnerSelectorSettings()
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh, k8s=k8s)

        local.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w_local", token="t1", node_id="n1",
            process=MagicMock(spec=asyncio.subprocess.Process),
        ))
        ssh.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w_ssh", token="t2", node_id="n2",
            process=MagicMock(spec=RemoteWorkerHandle),
        ))
        k8s.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w_k8s", token="t3", node_id="n3",
            process=MagicMock(spec=K8sWorkerHandle),
        ))

        manifest = make_manifest()

        # Local
        await sel.spawn_worker("w_local", "b1", "n1", manifest, "/tmp/work",
                               task_spec={"runner_preference": "local"})
        local.spawn_worker.assert_called_once()

        # SSH
        await sel.spawn_worker("w_ssh", "b1", "n2", manifest, "/tmp/work",
                               task_spec={"runner_preference": "remote_ssh"})
        ssh.spawn_worker.assert_called_once()

        # K8s
        await sel.spawn_worker("w_k8s", "b1", "n3", manifest, "/tmp/work",
                               task_spec={"runner_preference": "k8s"})
        k8s.spawn_worker.assert_called_once()

        # Verify runner_type recorded for all three
        expected_updates = [
            ("UPDATE workers SET runner_type = ? WHERE id = ?", ("local", "w_local")),
            ("UPDATE workers SET runner_type = ? WHERE id = ?", ("remote_ssh", "w_ssh")),
            ("UPDATE workers SET runner_type = ? WHERE id = ?", ("k8s", "w_k8s")),
        ]
        for sql, params in expected_updates:
            db.execute.assert_any_call(sql, params)

    def test_get_runner_accessor(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        ssh = MagicMock(spec=RemoteSSHWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, remote_ssh=ssh)

        assert sel.get_runner("local") is local
        assert sel.get_runner("remote_ssh") is ssh
        assert sel.get_runner("k8s") is None

    def test_runner_names_property(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        local = MagicMock(spec=LocalBwrapWorkerRunner)
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        sel = RunnerSelector(db, settings, local=local, k8s=k8s)

        assert set(sel.runner_names) == {"local", "k8s"}

    @pytest.mark.asyncio
    async def test_start_watches_starts_k8s_watch(self):
        settings = RunnerSelectorSettings()
        db = MagicMock()
        k8s = MagicMock(spec=K8sJobWorkerRunner)
        k8s.start_watch = AsyncMock()
        sel = RunnerSelector(db, settings, k8s=k8s)

        await sel.start_watches()
        k8s.start_watch.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_watches_noop_without_k8s(self):
        settings = RunnerSelectorSettings()
        sel = RunnerSelector(MagicMock(), settings, local=MagicMock(spec=LocalBwrapWorkerRunner))
        # Should not raise
        await sel.start_watches()
