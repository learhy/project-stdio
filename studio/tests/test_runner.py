"""Tests for runner.py — bubblewrap arg building, worker spawning, token generation."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.runner import (
    LocalBwrapWorkerRunner,
    NoopWorkerRunner,
    WorkerSpawnResult,
    _generate_token,
    capability_to_bwrap_args,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    FilesystemGrants,
    NetworkGrants,
    ProcessGrants,
    RpcGrants,
    ResourceGrants,
    Grants,
    ManifestSubject,
    ManifestMetadata,
    EgressProxySettings,
)


def make_manifest(**fs_overrides) -> CapabilityManifest:
    """Create a test capability manifest."""
    fs_grants = {
        "reads": [{"path": "/usr/lib", "recursive": True}],
        "writes": [{"path": "/tmp/build", "recursive": True, "create": True}],
        **fs_overrides,
    }
    return CapabilityManifest(
        schema_version="1.0",
        subject=ManifestSubject(kind="bundle", id="test"),
        grants=Grants(
            filesystem=FilesystemGrants(
                reads=[FilesystemPathGrant(**r) for r in fs_grants.get("reads", [])],
                writes=[FilesystemWriteGrant(**w) for w in fs_grants.get("writes", [])],
            ),
            network=NetworkGrants(),
            process=ProcessGrants(),
            rpc=RpcGrants(methods=["worker.*", "cap.*"]),
            resources=ResourceGrants(),
        ),
        metadata=ManifestMetadata(rationale="test"),
    )


class TestGenerateToken:
    def test_token_is_hex_string(self):
        token = _generate_token()
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_tokens_are_unique(self):
        tokens = {_generate_token() for _ in range(100)}
        assert len(tokens) == 100


class TestWorkerSpawnResult:
    def test_attributes(self):
        proc = MagicMock()
        result = WorkerSpawnResult("w1", "tok", "n1", proc)
        assert result.worker_id == "w1"
        assert result.token == "tok"
        assert result.node_id == "n1"
        assert result.process is proc


class TestLocalBwrapWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    def test_default_worker_command(self, db_mock):
        runner = LocalBwrapWorkerRunner(db_mock, "/tmp/socket")
        assert len(runner.worker_command) == 1
        assert "studio-worker" in runner.worker_command[0]

    def test_custom_worker_command(self, db_mock):
        runner = LocalBwrapWorkerRunner(db_mock, "/tmp/socket", worker_command=["/usr/bin/code-buddy"])
        assert runner.worker_command == ["/usr/bin/code-buddy"]

    def test_bwrap_basic_args(self, runner):
        manifest = make_manifest()
        args = capability_to_bwrap_args(manifest, "/tmp/worktree", socket_path=runner.socket_path)

        assert args[0] == "bwrap"
        assert "--die-with-parent" in args
        assert "--tmpfs" in args
        assert "/tmp" in args
        assert "--bind" in args
        assert "/tmp/worktree" in args
        assert "/work" in args
        assert "--chdir" in args
        assert "--proc" in args
        assert "--dev" in args

    def test_bwrap_readonly_mounts(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = capability_to_bwrap_args(manifest, "/tmp/wt", socket_path=runner.socket_path)
        assert "--ro-bind" in args
        assert "/usr/lib" in args

    def test_bwrap_writable_mounts(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = capability_to_bwrap_args(manifest, "/tmp/wt", socket_path=runner.socket_path)
        # Should have bind for /tmp/build
        assert "--bind" in args
        assert "/tmp/build" in args

    def test_bwrap_unshare_net_when_proxy_active(self, runner):
        manifest = make_manifest()
        args = capability_to_bwrap_args(manifest, "/tmp/wt", socket_path=runner.socket_path, proxy_socket="/run/studio/proxy-test.sock")
        assert "--unshare-net" in args

    def test_bwrap_no_unshare_net_when_proxy_inactive(self, runner):
        manifest = make_manifest()
        args = capability_to_bwrap_args(manifest, "/tmp/wt", socket_path=runner.socket_path)
        assert "--unshare-net" not in args

    def test_bwrap_socket_directory_bound(self, runner):
        manifest = make_manifest()
        with patch("os.path.exists", return_value=True):
            args = capability_to_bwrap_args(manifest, "/tmp/wt", socket_path=runner.socket_path)
        assert "/run/studio" in args

    @pytest.mark.asyncio
    async def test_spawn_worker_inserts_db_row(self, runner, db_mock):
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                result = await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

                # Check DB insert
                insert_call = db_mock.execute.call_args_list[0]
                assert "INSERT INTO workers" in insert_call[0][0]
                assert insert_call[0][1][0] == "w1"
                assert insert_call[0][1][1] == "b1"
                assert insert_call[0][1][2] == "n1"
                assert insert_call[0][1][6] == "pending"

                assert result.worker_id == "w1"
                assert result.process is mock_proc
                assert len(result.token) == 64

    @pytest.mark.asyncio
    async def test_spawn_worker_env_includes_token(self, runner, db_mock):
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

                # Only one call (worker process, no worktree creation in test mode)
                _, _, kwargs = mock_spawn.mock_calls[0]
                assert "env" in kwargs
                env = kwargs["env"]
                assert "STUDIO_WORKER_TOKEN" in env
                assert "STUDIO_SOCKET_PATH" in env
                assert env["STUDIO_WORKER_ID"] == "w1"
                assert "STUDIO_WORKTREE_PATH" in env
                assert "STUDIO_BASE_BRANCH" in env
                assert "STUDIO_PROXY_SOCKET" in env
                assert "http_proxy" in env
                assert "https_proxy" in env

    @pytest.mark.asyncio
    async def test_spawn_worker_includes_task_spec_in_env(self, runner, db_mock):
        manifest = make_manifest()
        task_spec = {"objective": "build server", "inputs": {}}
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt", task_spec=task_spec)

                _, _, kwargs = mock_spawn.mock_calls[0]
                env = kwargs["env"]
                assert "STUDIO_TASK_SPEC" in env
                assert "build server" in env["STUDIO_TASK_SPEC"]

    @pytest.mark.asyncio
    async def test_kill_worker_terminate(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        await runner.kill_worker(proc)
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_worker_with_proxy_cleanup(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        # Add a mock proxy process
        proxy_proc = MagicMock()
        proxy_proc.terminate = MagicMock()
        proxy_proc.wait = AsyncMock(return_value=0)
        runner._proxy_processes["w1"] = proxy_proc

        with patch("os.unlink"):
            await runner.kill_worker(proc, "w1")

        proc.terminate.assert_called_once()
        proxy_proc.terminate.assert_called_once()
        assert "w1" not in runner._proxy_processes

    @pytest.mark.asyncio
    async def test_kill_worker_timeout_then_kill(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])

        await runner.kill_worker(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_worker_already_exited(self, runner):
        proc = MagicMock()
        proc.terminate = MagicMock(side_effect=ProcessLookupError())

        await runner.kill_worker(proc)
        # Should not raise


class TestNoopWorkerRunner:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return NoopWorkerRunner(db_mock)

    @pytest.mark.asyncio
    async def test_spawn_returns_result_with_no_process(self, runner, db_mock):
        manifest = make_manifest()
        result = await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

        assert result.worker_id == "w1"
        assert len(result.token) == 64
        assert result.process is None

        # Check DB insert
        insert_call = db_mock.execute.call_args_list[0]
        assert "INSERT INTO workers" in insert_call[0][0]
        assert insert_call[0][1][0] == "w1"

    @pytest.mark.asyncio
    async def test_noop_runner_inserts_pending_state(self, runner, db_mock):
        manifest = make_manifest()
        await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")
        insert_call = db_mock.execute.call_args_list[0]
        # Index 6 = state (shifted by token_expires_at at index 4)
        assert insert_call[0][1][6] == "pending"


class TestWorkerRespawn:
    """Tests for BUG #17: worker re-spawn handling."""

    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    @pytest.mark.asyncio
    async def test_respawn_after_terminal_state_deletes_and_inserts(self, runner, db_mock):
        """Worker in COMPLETE state is deleted before re-insert."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = {"id": "old-w1", "state": "complete"}

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                result = await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

        # Should have deleted old row
        delete_calls = [c for c in db_mock.execute.call_args_list
                        if "DELETE FROM workers" in str(c[0][0])]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1][0] == "old-w1"

        # Should have inserted new row
        insert_calls = [c for c in db_mock.execute.call_args_list
                        if "INSERT INTO workers" in str(c[0][0])]
        assert len(insert_calls) == 1
        assert result.worker_id == "w1"

    @pytest.mark.asyncio
    async def test_respawn_on_failed_worker_deletes_and_inserts(self, runner, db_mock):
        """Worker in FAILED state is deleted before re-insert."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = {"id": "old-w2", "state": "failed"}

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w2", "b1", "n1", manifest, "/tmp/wt")

        delete_calls = [c for c in db_mock.execute.call_args_list
                        if "DELETE FROM workers" in str(c[0][0])]
        assert len(delete_calls) == 1

    @pytest.mark.asyncio
    async def test_respawn_on_killed_worker_deletes_and_inserts(self, runner, db_mock):
        """Worker in KILLED state is deleted before re-insert."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = {"id": "old-w3", "state": "killed"}

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w3", "b1", "n1", manifest, "/tmp/wt")

        delete_calls = [c for c in db_mock.execute.call_args_list
                        if "DELETE FROM workers" in str(c[0][0])]
        assert len(delete_calls) == 1

    @pytest.mark.asyncio
    async def test_respawn_blocks_on_running_worker(self, runner, db_mock):
        """Worker in RUNNING state raises RuntimeError on re-spawn attempt."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = {"id": "w-active", "state": "running"}

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with pytest.raises(RuntimeError, match="already in state running"):
                await runner.spawn_worker("w-new", "b1", "n1", manifest, "/tmp/wt")

    @pytest.mark.asyncio
    async def test_respawn_retries_on_pending_worker(self, runner, db_mock):
        """Worker in PENDING state is reused (retried) on re-spawn attempt."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = {"id": "w-pending", "state": "pending"}

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                result = await runner.spawn_worker("w-new2", "b1", "n1", manifest, "/tmp/wt")

        # Should succeed without raising — pending worker is reused
        assert result.error == ""
        assert result.worker_id == "w-new2"

    @pytest.mark.asyncio
    async def test_respawn_no_existing_worker_inserts_normally(self, runner, db_mock):
        """No existing worker row: normal insert proceeds without delete."""
        manifest = make_manifest()
        db_mock.fetch_one.return_value = None

        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                result = await runner.spawn_worker("w4", "b1", "n1", manifest, "/tmp/wt")

        delete_calls = [c for c in db_mock.execute.call_args_list
                        if "DELETE FROM workers" in str(c[0][0])]
        assert len(delete_calls) == 0
        assert result.worker_id == "w4"


class TestWorkerTypeRouting:
    """Tests for worker_type parameter: review workers run studio-review."""

    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    @pytest.mark.asyncio
    async def test_review_worker_uses_studio_review_command(self, runner, db_mock):
        """worker_type='review' spawns studio-review instead of studio-worker."""
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt",
                                          worker_type="review")

        cmd = mock_spawn.call_args[0]
        assert any("studio-review" in c for c in cmd)
        assert not any("studio-worker" in c for c in cmd)

    @pytest.mark.asyncio
    async def test_developer_worker_uses_default_worker_command(self, runner, db_mock):
        """Default worker_type='developer' uses the configured worker_command."""
        manifest = make_manifest()
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

        cmd = mock_spawn.call_args[0]
        assert any("studio-worker" in c for c in cmd)

    @pytest.mark.asyncio
    async def test_review_worker_with_bwrap_includes_studio_review(self, runner, db_mock):
        """worker_type='review' with bwrap spawns bwrap + studio-review."""
        manifest = make_manifest()
        runner._bwrap_available = True
        with patch.dict("os.environ", {"STUDIO_TEST_MODE": "1"}):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt",
                                          worker_type="review")

        cmd = mock_spawn.call_args[0]
        assert "bwrap" in cmd
        assert any("studio-review" in c for c in cmd)
        assert not any("studio-worker" in c for c in cmd)


class TestStripRemotes:
    """Tests for _strip_remotes: workers must never have git remotes."""

    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    @pytest.mark.asyncio
    async def test_strip_remotes_removes_all_remotes(self, runner):
        """_strip_remotes removes all existing git remotes."""
        call_args_list = []

        async def mock_spawn(*args, **kwargs):
            call_args_list.append(args)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            # args: ("git", "-C", path, subcommand, ...)
            if len(args) >= 4 and args[3] == "remote" and len(args) == 4:
                # git remote (list)
                mock_proc.communicate = AsyncMock(return_value=(b"origin\nupstream\n", b""))
            else:
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.wait = AsyncMock(return_value=0)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=mock_spawn):
            await runner._strip_remotes("/tmp/wt")

        # Should have called git remote, then git remote remove for each
        remote_list_calls = [a for a in call_args_list
                            if len(a) >= 4 and a[3] == "remote" and len(a) == 4]
        remove_calls = [a for a in call_args_list
                       if len(a) >= 5 and a[3] == "remote" and a[4] == "remove"]
        config_calls = [a for a in call_args_list
                       if len(a) >= 4 and a[3] == "config"]

        assert len(remote_list_calls) == 1
        assert len(remove_calls) == 2  # origin and upstream
        assert any(a[5] == "origin" for a in remove_calls)
        assert any(a[5] == "upstream" for a in remove_calls)
        # Should disable credential helpers
        assert len(config_calls) == 2

    @pytest.mark.asyncio
    async def test_strip_remotes_no_remotes_graceful(self, runner):
        """_strip_remotes handles repos with no remotes gracefully."""
        call_args_list = []

        async def mock_spawn(*args, **kwargs):
            call_args_list.append(args)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            if len(args) >= 4 and args[3] == "remote" and len(args) == 4:
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            else:
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.wait = AsyncMock(return_value=0)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=mock_spawn):
            await runner._strip_remotes("/tmp/wt")

        remove_calls = [a for a in call_args_list
                       if len(a) >= 5 and a[3] == "remote" and a[4] == "remove"]
        assert len(remove_calls) == 0


class TestWorkerNoGitHubCredentials:
    """Workers must not receive GitHub credentials in their environment."""

    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def runner(self, db_mock):
        return LocalBwrapWorkerRunner(db_mock, "/run/studio/test.sock")

    @pytest.mark.asyncio
    async def test_worker_env_has_no_github_tokens(self, runner, db_mock):
        """Worker env contains no GH_TOKEN, GITHUB_TOKEN, or SSH_AUTH_SOCK."""
        manifest = make_manifest()
        with patch.dict("os.environ", {
            "STUDIO_TEST_MODE": "1",
            "GH_TOKEN": "ghp_fake_token",
            "GITHUB_TOKEN": "github_fake_token",
            "SSH_AUTH_SOCK": "/tmp/fake-ssh-agent.sock",
        }):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

            _, _, kwargs = mock_spawn.mock_calls[0]
            env = kwargs["env"]
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN" not in env
            assert "SSH_AUTH_SOCK" not in env

    @pytest.mark.asyncio
    async def test_worker_env_has_no_github_tokens_when_not_set(self, runner, db_mock):
        """Worker env is clean even when host doesn't have GitHub creds set."""
        manifest = make_manifest()
        clean_env = {"STUDIO_TEST_MODE": "1", "PATH": "/usr/bin", "HOME": "/tmp"}
        with patch.dict("os.environ", clean_env, clear=True):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
                mock_proc = MagicMock()
                mock_spawn.return_value = mock_proc

                await runner.spawn_worker("w1", "b1", "n1", manifest, "/tmp/wt")

            _, _, kwargs = mock_spawn.mock_calls[0]
            env = kwargs["env"]
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN" not in env
            assert "SSH_AUTH_SOCK" not in env
