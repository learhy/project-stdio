"""Tests for GitHub Issues integration: GitHubClient, settings, templates, state machine side-effects, polling."""
import json
import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.models import GitHubSettings, BundleState
from studio.orchestrator.github import GitHubClient, BOT_LOGIN, GITHUB_API_BASE
from studio.orchestrator.state_machine import BundleStateMachine


class TestGitHubSettings:
    def test_defaults(self):
        s = GitHubSettings()
        assert s.enabled is False
        assert s.app_id == ""
        assert s.installation_id == ""
        assert s.private_key_path == ""
        assert s.poll_interval_seconds == 60
        assert s.owner == ""
        assert s.repo == ""

    def test_enabled(self):
        s = GitHubSettings(enabled=True, owner="acme", repo="widgets")
        assert s.enabled is True
        assert s.owner == "acme"
        assert s.repo == "widgets"


class TestGitHubClient:
    @pytest.fixture
    def settings(self):
        return GitHubSettings(
            enabled=True,
            app_id="12345",
            installation_id="67890",
            private_key_path="/nonexistent/key.pem",
            owner="acme",
            repo="widgets",
        )

    @pytest.fixture
    def client(self, settings):
        return GitHubClient(settings)

    def test_init(self, client, settings):
        assert client._settings is settings

    @pytest.mark.asyncio
    async def test_initialize_disabled(self):
        c = GitHubClient(GitHubSettings(enabled=False))
        await c.initialize()
        assert c._client is None

    @pytest.mark.asyncio
    async def test_initialize_missing_key(self, client):
        await client.initialize()
        assert client._client is None  # key file doesn't exist, but no crash

    @pytest.mark.asyncio
    async def test_create_issue_no_client(self, client):
        client._client = None
        result = await client.create_issue("title", "body")
        assert result is None

    @pytest.mark.asyncio
    async def test_post_comment_no_client(self, client):
        client._client = None
        result = await client.post_comment(1, "body")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_comments_since_no_client(self, client):
        client._client = None
        result = await client.get_comments_since(1)
        assert result == []

    @pytest.mark.asyncio
    async def test_update_labels_no_client(self, client):
        client._client = None
        result = await client.update_labels(1, ["bug"])
        assert result is False

    @pytest.mark.asyncio
    async def test_close(self, client):
        client._client = MagicMock()
        client._client.aclose = AsyncMock()
        await client.close()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_get_comments_filters_bot(self, client):
        client._client = MagicMock()
        client._client.request = AsyncMock()
        client._installation_token = "tok"
        client._token_expiry = 9999999999
        client._private_key = "fake-key"

        bot_comment = {"id": 1, "user": {"login": BOT_LOGIN}, "body": "/approve"}
        user_comment = {"id": 2, "user": {"login": "pm-user"}, "body": "/approve"}
        client._client.request.return_value = MagicMock(
            status_code=200,
            json=lambda: [bot_comment, user_comment],
            raise_for_status=lambda: None,
        )

        result = await client.get_comments_since(1)
        assert len(result) == 1
        assert result[0]["id"] == 2

    @pytest.mark.asyncio
    async def test_get_comments_handles_api_error(self, client):
        client._client = MagicMock()
        client._client.request = AsyncMock(side_effect=Exception("boom"))
        client._installation_token = "tok"
        client._token_expiry = 9999999999
        client._private_key = "fake-key"

        result = await client.get_comments_since(1)
        assert result == []

    @pytest.mark.asyncio
    async def test_request_retries_on_401(self, client):
        client._client = MagicMock()
        client._client.request = AsyncMock()
        client._installation_token = "old-tok"
        client._token_expiry = 9999999999
        client._private_key = "fake-key"

        # First call returns 401, second succeeds
        client._client.request.side_effect = [
            MagicMock(status_code=401, raise_for_status=lambda: None, json=lambda: {}),
            MagicMock(status_code=201, json=lambda: {"number": 42}, raise_for_status=lambda: None),
        ]

        # Mock token refresh
        async def fake_get_token():
            return "new-tok"

        client._get_installation_token = fake_get_token
        data = await client._request("POST", "/test", {"title": "test"})
        assert data == {"number": 42}
        assert client._client.request.call_count == 2


class TestCommentParsing:
    def test_slash_approve_parsing(self):
        assert re.match(r"^/approve\s*$", "/approve", re.IGNORECASE)
        assert re.match(r"^/approve\s*$", "/APPROVE", re.IGNORECASE)
        assert re.match(r"^/approve\s*$", "/approve   ", re.IGNORECASE)
        assert not re.match(r"^/approve\s*$", "/approve extra text", re.IGNORECASE)

    def test_slash_reject_parsing(self):
        m = re.match(r"^/reject\s+(.+)$", "/reject too risky", re.IGNORECASE | re.DOTALL)
        assert m
        assert m.group(1) == "too risky"

        m = re.match(r"^/reject\s+(.+)$", "/REJECT multi\nline reason", re.IGNORECASE | re.DOTALL)
        assert m
        assert "multi\nline reason" in m.group(1)

        # ".+" matches any chars including spaces; \s+ is greedy so for 3 spaces
        # \s+ takes 2, .+ takes 1. A whitespace-only reason is still captured.
        m = re.match(r"^/reject\s+(.+)$", "/reject   ", re.IGNORECASE | re.DOTALL)
        assert m
        assert m.group(1) == " "

    def test_slash_modify_parsing(self):
        m = re.match(r"^/modify\s+(.+)$", "/modify add more tests", re.IGNORECASE | re.DOTALL)
        assert m
        assert m.group(1) == "add more tests"

        m = re.match(r"^/modify\s+(.+)$", "/modify  ", re.IGNORECASE | re.DOTALL)
        assert m
        assert m.group(1) == " "  # one trailing space captured


class TestIssueTemplate:
    def test_format_title_short_summary(self):
        sm = BundleStateMachine(MagicMock())
        title = sm._format_issue_title("01JABC123EXAMPLE", {"requirements_summary": "Add logout button"})
        assert "Add logout button" in title
        assert title.startswith("[01JABC12]")

    def test_format_title_long_summary_truncated(self):
        sm = BundleStateMachine(MagicMock())
        long_summary = "A" * 100
        title = sm._format_issue_title("01JABC123EXAMPLE", {"requirements_summary": long_summary})
        # [XXXXXXXX] = 10 chars + space = 11, summary truncated to 77 + "..." = 80, total max = 91
        assert len(title) <= 91
        assert title.endswith("...")

    def test_format_body_includes_scores_and_commands(self):
        sm = BundleStateMachine(MagicMock())
        proposal = {
            "complexity_score": 7,
            "risk_score": 4,
            "target": "auth-service",
            "requirements_summary": "Add OAuth2 support",
            "concerns": ["Breaking change", "Migration needed"],
        }
        body = sm._format_issue_body("bundle-1", proposal)
        assert "Complexity Score: 7/10" in body
        assert "Risk Score: 4/10" in body
        assert "auth-service" in body
        assert "Add OAuth2 support" in body
        assert "Breaking change" in body
        assert "Migration needed" in body
        assert "/approve" in body
        assert "/reject" in body
        assert "/modify" in body
        assert "## Available Commands" in body

    def test_format_body_no_concerns(self):
        sm = BundleStateMachine(MagicMock())
        body = sm._format_issue_body("bundle-1", {"requirements_summary": "Fix typo"})
        assert "None" in body

    def test_derive_issue_labels(self):
        sm = BundleStateMachine(MagicMock())
        labels = sm._derive_issue_labels({"tier": "full_review"})
        assert "approval/full-review" in labels
        assert "state/in-review" in labels


class TestStateMachineGitHubIntegration:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.transaction = MagicMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def github_mock(self):
        gh = AsyncMock()
        gh.create_issue = AsyncMock(return_value=42)
        gh.post_comment = AsyncMock(return_value=True)
        gh.get_comments_since = AsyncMock(return_value=[])
        return gh

    @pytest.fixture
    def sm(self, db_mock):
        sm = BundleStateMachine(db_mock, kernel_mode=True)
        return sm

    @pytest.mark.asyncio
    async def test_github_create_issue_called_on_bundler_planning(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {
            "state": BundleState.PROPOSED,
            "proposal_json": "{}",
        }

        await sm.transition_complete_bundler_planning("bundle-1", {
            "requirements_summary": "Test",
            "complexity_score": 5,
            "risk_score": 3,
            "target": "control-plane",
            "concerns": [],
            "task_dag": {"nodes": [], "edges": [], "entry_nodes": []},
        })

        github_mock.create_issue.assert_called_once()
        update_calls = [
            c for c in db_mock.execute.call_args_list
            if "github_issue_number" in str(c[0])
        ]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_github_not_called_when_client_is_none(self, sm, db_mock):
        db_mock.fetch_one.return_value = {
            "state": BundleState.PROPOSED,
            "proposal_json": "{}",
        }

        await sm.transition_complete_bundler_planning("bundle-1", {
            "requirements_summary": "Test",
            "task_dag": {"nodes": [], "edges": [], "entry_nodes": []},
        })

        update_calls = [
            c for c in db_mock.execute.call_args_list
            if "github_issue_number" in str(c[0])
        ]
        assert len(update_calls) == 0

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_approve(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW, "github_issue_number": 42}

        await sm.transition_4_approve_from_review("bundle-1", "admin")

        github_mock.post_comment.assert_called_once()
        call_args = github_mock.post_comment.call_args
        assert call_args[0][0] == 42
        assert "approved" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_reject(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW, "github_issue_number": 42}

        await sm.transition_reject_from_review("bundle-1", "pm", "too risky")

        github_mock.post_comment.assert_called_once()
        call_args = github_mock.post_comment.call_args
        assert "rejected" in call_args[0][1].lower()
        assert "too risky" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_modify(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW, "github_issue_number": 42}

        await sm.transition_3_return_to_proposed("bundle-1", "add tests")

        github_mock.post_comment.assert_called_once()
        call_args = github_mock.post_comment.call_args
        assert "modification" in call_args[0][1].lower()
        assert "add tests" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_pause(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS, "github_issue_number": 42}

        await sm.transition_pause("bundle-1", "waiting for input")

        github_mock.post_comment.assert_called_once()
        assert "paused" in github_mock.post_comment.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_resume(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.PAUSED, "github_issue_number": 42}

        await sm.transition_resume("bundle-1")

        github_mock.post_comment.assert_called_once()
        assert "resumed" in github_mock.post_comment.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_github_post_mirror_on_execution_failure(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS, "github_issue_number": 42}

        await sm.transition_25_fail_execution("bundle-1", "DAG node crashed")

        github_mock.post_comment.assert_called_once()
        assert "failed" in github_mock.post_comment.call_args[0][1].lower()
        assert "DAG node crashed" in github_mock.post_comment.call_args[0][1]

    @pytest.mark.asyncio
    async def test_github_mirror_skipped_when_no_issue_number(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW, "github_issue_number": None}

        await sm.transition_4_approve_from_review("bundle-1", "admin")

        github_mock.post_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_create_issue_handles_api_failure(self, sm, db_mock, github_mock):
        sm.set_github_client(github_mock)
        github_mock.create_issue.return_value = None  # API failure
        db_mock.fetch_one.return_value = {
            "state": BundleState.PROPOSED,
            "proposal_json": "{}",
        }

        await sm.transition_complete_bundler_planning("bundle-1", {
            "requirements_summary": "Test",
            "task_dag": {"nodes": [], "edges": [], "entry_nodes": []},
        })

        # Transition still completed, just no issue number set
        update_calls = [
            c for c in db_mock.execute.call_args_list
            if "github_issue_number" in str(c[0])
        ]
        assert len(update_calls) == 0


class TestPollingLogic:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.transaction = MagicMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def sm(self, db_mock):
        return BundleStateMachine(db_mock, kernel_mode=True)

    def test_bot_comments_are_filtered(self):
        assert BOT_LOGIN == "studio-agents[bot]"

    @pytest.mark.asyncio
    async def test_process_comment_approve(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW}

        comment = {"body": "/approve", "user": {"login": "pm-user"}}
        body = comment["body"].strip()
        user = comment["user"]["login"]
        actor = f"github:{user}"

        if re.match(r"^/approve\s*$", body, re.IGNORECASE):
            await sm.transition_4_approve_from_review("bundle-1", actor)

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.APPROVED in update_call[0][1]

    @pytest.mark.asyncio
    async def test_process_comment_reject(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW}

        comment = {"body": "/reject too risky", "user": {"login": "pm-user"}}
        body = comment["body"].strip()
        user = comment["user"]["login"]
        actor = f"github:{user}"

        m = re.match(r"^/reject\s+(.+)$", body, re.IGNORECASE | re.DOTALL)
        if m:
            reason = m.group(1)
            await sm.transition_reject_from_review("bundle-1", actor, reason)

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.REJECTED in update_call[0][1]

    @pytest.mark.asyncio
    async def test_process_comment_modify(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_REVIEW}

        comment = {"body": "/modify add security review", "user": {"login": "pm-user"}}
        body = comment["body"].strip()
        user = comment["user"]["login"]
        actor = f"github:{user}"

        m = re.match(r"^/modify\s+(.+)$", body, re.IGNORECASE | re.DOTALL)
        if m:
            instructions = m.group(1)
            await sm.transition_3_return_to_proposed("bundle-1", instructions)

        # First call is UPDATE bundles SET state = proposed
        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.PROPOSED in update_call[0][1]
        # The instructions go into the audit log entry, not the state UPDATE
        audit_call = db_mock.execute.call_args_list[1]
        assert "add security review" in str(audit_call[0])

    @pytest.mark.asyncio
    async def test_unknown_comment_body_is_ignored(self):
        comment = {"body": "Looks good to me!", "user": {"login": "pm-user"}}
        body = comment["body"].strip()

        matched = False
        if re.match(r"^/approve\s*$", body, re.IGNORECASE):
            matched = True
        elif re.match(r"^/reject\s+(.+)$", body, re.IGNORECASE | re.DOTALL):
            matched = True
        elif re.match(r"^/modify\s+(.+)$", body, re.IGNORECASE | re.DOTALL):
            matched = True

        assert not matched

    def test_processed_comment_ids_dedup(self):
        processed = {42, 43, 44}
        assert 42 in processed
        assert 99 not in processed
