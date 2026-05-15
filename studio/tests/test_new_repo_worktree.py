"""Tests for BUG #15: new-repo worktree initialization and post-worker git push."""
import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.runner import (
    LocalBwrapWorkerRunner,
    WorkerSpawnResult,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    Grants,
    NodeState,
)


def make_manifest():
    return CapabilityManifest(
        schema_version="1.0",
        grants=Grants(),
    )


def _make_async_db():
    """Build a mock database whose async methods are AsyncMock."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.conn = MagicMock()
    db.conn.commit = AsyncMock()
    return db


class TestInitNewRepo:
    """Tests for _init_new_repo: empty git repo creation."""

    @pytest.mark.asyncio
    async def test_init_new_repo_creates_valid_git_repo(self, tmp_path):
        runner = LocalBwrapWorkerRunner(
            db=MagicMock(),
            socket_path="/tmp/test.sock",
            egress_proxy=MagicMock(),
            worker_command=["echo"],
        )
        repo_path = str(tmp_path / "new-repo")

        await runner._init_new_repo(repo_path)

        assert os.path.isdir(os.path.join(repo_path, ".git"))

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert proc.returncode == 0
        assert int(stdout.decode().strip()) >= 1

        for key in ("user.name", "user.email"):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "config", key,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            assert stdout.decode().strip() != ""

    @pytest.mark.asyncio
    async def test_init_new_repo_does_not_have_origin_remote(self, tmp_path):
        runner = LocalBwrapWorkerRunner(
            db=MagicMock(),
            socket_path="/tmp/test.sock",
            egress_proxy=MagicMock(),
            worker_command=["echo"],
        )
        repo_path = str(tmp_path / "new-repo-no-remote")

        await runner._init_new_repo(repo_path)

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "remote",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert stdout.decode().strip() == ""


class TestSpawnWorkerNewRepo:
    """Tests for spawn_worker with target='new-repo'."""

    def _make_runner(self):
        db = _make_async_db()
        egress_proxy = MagicMock()
        egress_proxy.enabled = False
        runner = LocalBwrapWorkerRunner(
            db=db,
            socket_path="/tmp/test.sock",
            egress_proxy=egress_proxy,
            worker_command=["echo"],
        )
        return runner

    @pytest.mark.asyncio
    async def test_spawn_worker_new_repo_calls_init_new_repo(self):
        runner = self._make_runner()
        runner._check_bwrap = AsyncMock(return_value=False)
        runner._create_worktree = AsyncMock()
        runner._init_new_repo = AsyncMock()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_exec.return_value = mock_proc

            await runner.spawn_worker(
                worker_id="w-test",
                bundle_id="bundle-1",
                node_id="node-1",
                manifest=make_manifest(),
                worktree_path="/tmp/test-worktree",
                target="new-repo",
            )

        runner._init_new_repo.assert_called_once_with("/tmp/test-worktree")
        runner._create_worktree.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_worker_existing_repo_calls_create_worktree(self):
        runner = self._make_runner()
        runner._check_bwrap = AsyncMock(return_value=False)
        runner._create_worktree = AsyncMock()
        runner._init_new_repo = AsyncMock()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_exec.return_value = mock_proc

            await runner.spawn_worker(
                worker_id="w-test",
                bundle_id="bundle-1",
                node_id="node-1",
                manifest=make_manifest(),
                worktree_path="/tmp/test-worktree",
                target="existing-repo",
            )

        runner._create_worktree.assert_called_once()
        runner._init_new_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_worker_new_repo_sets_STUDIO_TARGET_env(self):
        runner = self._make_runner()
        runner._check_bwrap = AsyncMock(return_value=False)
        runner._init_new_repo = AsyncMock()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_exec.return_value = mock_proc

            await runner.spawn_worker(
                worker_id="w-test-2",
                bundle_id="bundle-2",
                node_id="node-2",
                manifest=make_manifest(),
                worktree_path="/tmp/test-worktree-2",
                target="new-repo",
            )

            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["env"].get("STUDIO_TARGET") == "new-repo"


class TestDispatchWorkerTarget:
    """Tests for _dispatch_worker reading target from bundle proposal_json."""

    @pytest.mark.asyncio
    async def test_dispatch_reads_target_from_proposal(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({"proposal": {"target": "new-repo", "target_name": "my-api"}}),
        })

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None
        runner = MagicMock()
        runner.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w1", token="tok", node_id="n1",
            process=mock_proc,
        ))

        executor = DagExecutor(db, MagicMock(), runner, MagicMock(), MagicMock())
        executor._drain_worker_pipes = MagicMock()  # no-op to skip pipe draining

        node = {
            "id": "b1:n1",
            "node_id": "n1",
            "kind": "worker",
            "spec_json": json.dumps({"objective": "Build API"}),
        }

        await executor._dispatch_worker("b1", node)

        runner.spawn_worker.assert_called_once()
        call_kwargs = runner.spawn_worker.call_args.kwargs
        assert call_kwargs["target"] == "new-repo"
        assert executor._worker_targets.get("w_b1_n1") == "new-repo"
        assert executor._worker_worktree_paths.get("w_b1_n1") == "/tmp/studio-worktrees/b1/n1"

    @pytest.mark.asyncio
    async def test_dispatch_defaults_to_existing_repo(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({"other": "data"}),
        })

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None
        runner = MagicMock()
        runner.spawn_worker = AsyncMock(return_value=WorkerSpawnResult(
            worker_id="w2", token="tok", node_id="n2",
            process=mock_proc,
        ))

        executor = DagExecutor(db, MagicMock(), runner, MagicMock(), MagicMock())
        executor._drain_worker_pipes = MagicMock()  # no-op

        node = {
            "id": "b2:n2",
            "node_id": "n2",
            "kind": "worker",
            "spec_json": json.dumps({"objective": "Test"}),
        }

        await executor._dispatch_worker("b2", node)

        call_kwargs = runner.spawn_worker.call_args.kwargs
        assert call_kwargs["target"] == "existing-repo"


class TestPushWorkerChanges:
    """Tests for _push_worker_changes post-worker completion."""

    @pytest.mark.asyncio
    async def test_push_existing_repo_pushes_branch(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={"node_id": "n1"})

        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._worker_targets["w1"] = "existing-repo"
        executor._worker_worktree_paths["w1"] = "/tmp/studio-worktrees/b1/n1"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with patch("os.path.exists", return_value=True):
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                mock_proc.returncode = 0
                mock_exec.return_value = mock_proc

                await executor._push_worker_changes("w1", "b1")

                mock_exec.assert_called_once()
                call_args = mock_exec.call_args[0]
                assert call_args[0] == "git"
                assert "-C" in call_args
                assert "push" in call_args

    @pytest.mark.asyncio
    async def test_push_new_repo_creates_github_repo(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({"proposal": {"target": "new-repo", "target_name": "pm-sanity-api"}}),
        })

        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._worker_targets["w2"] = "new-repo"
        executor._worker_worktree_paths["w2"] = "/tmp/studio-worktrees/b2/n2"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with patch("shutil.rmtree"):
                with patch("os.path.exists", return_value=True):
                    mock_proc = AsyncMock()
                    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    await executor._push_worker_changes("w2", "b2")

                    mock_exec.assert_called_once()
                    call_args = mock_exec.call_args[0]
                    assert call_args[0] == "gh"
                    assert "repo" in call_args
                    assert "create" in call_args

    @pytest.mark.asyncio
    async def test_push_cleans_up_state(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={"node_id": "n3"})

        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._worker_targets["w3"] = "existing-repo"
        executor._worker_worktree_paths["w3"] = "/tmp/studio-worktrees/b3/n3"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with patch("os.path.exists", return_value=True):
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                mock_proc.returncode = 0
                mock_exec.return_value = mock_proc

                await executor._push_worker_changes("w3", "b3")

        assert "w3" not in executor._worker_targets
        assert "w3" not in executor._worker_worktree_paths

    @pytest.mark.asyncio
    async def test_push_handles_missing_worktree_gracefully(self):
        from studio.orchestrator.executor import DagExecutor

        executor = DagExecutor(
            _make_async_db(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        )
        await executor._push_worker_changes("nonexistent", "b1")

    @pytest.mark.asyncio
    async def test_new_repo_cleanup_removes_temp_dir(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        db.fetch_one = AsyncMock(return_value={
            "proposal_json": json.dumps({"proposal": {"target": "new-repo", "target_name": "test-repo"}}),
        })

        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._worker_targets["w4"] = "new-repo"
        executor._worker_worktree_paths["w4"] = "/tmp/studio-worktrees/b4/n4"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with patch("shutil.rmtree") as mock_rmtree:
                with patch("os.path.exists", return_value=True):
                    mock_proc = AsyncMock()
                    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    await executor._push_worker_changes("w4", "b4")

                    mock_rmtree.assert_called_once_with("/tmp/studio-worktrees/b4/n4")


class TestOnFinalReportPush:
    """Tests that _on_final_report calls _push_worker_changes on success."""

    @pytest.mark.asyncio
    async def test_final_report_success_triggers_push(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._active_bundles.add("b1")
        executor._push_worker_changes = AsyncMock()
        executor._process_node_completion = AsyncMock()
        executor._count_running_workers = AsyncMock(return_value=0)

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = 0
        executor._running_workers["w1"] = proc

        await executor._on_final_report("b1", "n1", "w1", {
            "outcome": "success",
            "node_state": NodeState.COMPLETED,
        })

        executor._push_worker_changes.assert_called_once_with("w1", "b1")

    @pytest.mark.asyncio
    async def test_final_report_failure_does_not_push(self):
        from studio.orchestrator.executor import DagExecutor

        db = _make_async_db()
        executor = DagExecutor(db, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        executor._active_bundles.add("b1")
        executor._push_worker_changes = AsyncMock()
        executor._fail_bundle = AsyncMock()
        executor._count_running_workers = AsyncMock(return_value=0)

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        executor._running_workers["w1"] = proc

        await executor._on_final_report("b1", "n1", "w1", {
            "outcome": "failure",
            "node_state": NodeState.FAILED,
        })

        executor._push_worker_changes.assert_not_called()
