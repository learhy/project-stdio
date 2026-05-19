"""GitHub App-authenticated REST API client for issue management.

Authenticates as a GitHub App using a JWT, exchanges for an installation token,
and performs issue create / comment post / comment list / label update operations.
All public methods return safe defaults on failure — never raise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt

from .models import GitHubSettings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# Bot user name for self-triggering prevention
BOT_LOGIN = "studio-agents[bot]"

# Rate-limit threshold: pause when remaining calls drop below this
RATE_LIMIT_REMAINING_THRESHOLD = 100


class GitHubRateLimiter:
    """Tracks GitHub API rate-limit state and paces calls to avoid exhausting quotas.

    Inspects X-RateLimit-Remaining and X-RateLimit-Reset headers from responses.
    When remaining calls drop below the threshold, subsequent calls are delayed
    until the reset window or until the minimum backoff has elapsed.
    """

    def __init__(self, remaining_threshold: int = RATE_LIMIT_REMAINING_THRESHOLD) -> None:
        self._threshold = remaining_threshold
        self._remaining: int | None = None
        self._reset_at: int = 0
        self._backoff_until: float = 0.0

    def update(self, headers: dict[str, str]) -> None:
        """Update tracked state from response headers."""
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            self._remaining = int(remaining)
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            self._reset_at = int(reset)

    @property
    def should_pause(self) -> bool:
        """True when rate-limit remaining is below threshold and no backoff is active."""
        if self._remaining is None:
            return False
        return self._remaining < self._threshold

    async def wait_if_needed(self) -> None:
        """Pause if rate-limited or in active backoff."""
        now = time.time()
        if now < self._backoff_until:
            delay = self._backoff_until - now
            logger.info("Rate-limit backoff: sleeping %.1fs", delay)
            await asyncio.sleep(delay)

        if self._remaining is not None and self._remaining < self._threshold:
            wait = max(self._reset_at - int(now) + 1, 30)
            logger.info("Rate-limit low (%d remaining): sleeping %ds until reset",
                        self._remaining, wait)
            await asyncio.sleep(wait)

    def backoff(self, seconds: int = 60) -> None:
        """Force a backoff period (called on 403/429 responses)."""
        self._backoff_until = time.time() + seconds
        logger.warning("Rate-limit backoff triggered: %ds", seconds)


class GitHubClient:
    """GitHub App-authenticated REST API client for issue management."""

    def __init__(self, settings: GitHubSettings) -> None:
        self._settings = settings
        self._private_key: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._installation_token: str | None = None
        self._token_expiry: int = 0
        self._rate_limiter = GitHubRateLimiter()
        self._account_type_cache: dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load private key and set up httpx client. Must call before API operations."""
        if not self._settings.enabled:
            return
        key_path = Path(self._settings.private_key_path)
        if not key_path.exists():
            logger.error("GitHub private key not found: %s", key_path)
            return
        self._private_key = key_path.read_text()
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "studio-agents",
            },
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Public API ─────────────────────────────────────────────────────────

    async def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> int | None:
        """Create a GitHub Issue. Returns issue number, or None on failure."""
        if self._client is None:
            return None
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        owner = self._settings.owner
        repo = self._settings.repo
        path = f"/repos/{owner}/{repo}/issues"
        data = await self._request("POST", path, payload)
        if data is None:
            return None
        return data.get("number")

    async def post_comment(self, issue_number: int, body: str) -> bool:
        """Post a comment on an issue. Returns True on success."""
        if self._client is None:
            return False
        owner = self._settings.owner
        repo = self._settings.repo
        path = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        data = await self._request("POST", path, {"body": body})
        return data is not None

    async def get_comments_since(
        self, issue_number: int, since: datetime | None = None
    ) -> list[dict]:
        """Get comments on an issue created after `since`. Filters out bot comments."""
        if self._client is None:
            return []
        owner = self._settings.owner
        repo = self._settings.repo
        path = f"/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page=100"
        if since is not None:
            iso = since.isoformat()
            # url-encode the + in the timezone offset
            path += f"&since={iso}"
        data = await self._request("GET", path)
        if data is None:
            return []
        # Filter out bot-authored comments
        return [c for c in data if c.get("user", {}).get("login") != BOT_LOGIN]

    async def update_labels(self, issue_number: int, labels: list[str]) -> bool:
        """Update issue labels. Returns True on success."""
        if self._client is None:
            return False
        owner = self._settings.owner
        repo = self._settings.repo
        path = f"/repos/{owner}/{repo}/issues/{issue_number}"
        data = await self._request("PATCH", path, {"labels": labels})
        return data is not None

    async def _get_account_type(self, owner: str) -> str | None:
        """Determine whether `owner` is a User or Organization. Cached in memory."""
        if owner in self._account_type_cache:
            return self._account_type_cache[owner]
        data = await self._request("GET", f"/users/{owner}")
        if data is None:
            return None
        account_type = data.get("type")
        if account_type:
            self._account_type_cache[owner] = account_type
        return account_type

    async def create_repo(self, name: str, private: bool = True, description: str = "") -> dict | None:
        """Create a new GitHub repository under the configured owner. Returns repo data or None."""
        if self._client is None:
            return None
        owner = self._settings.owner
        payload: dict[str, Any] = {"name": name, "private": private}
        if description:
            payload["description"] = description
        account_type = await self._get_account_type(owner)
        if account_type == "Organization":
            path = f"/orgs/{owner}/repos"
        else:
            path = "/user/repos"
        data = await self._request("POST", path, payload)
        if data is None and account_type != "Organization":
            # Fallback: try the org endpoint if user endpoint failed
            data = await self._request("POST", f"/orgs/{owner}/repos", payload)
        elif data is None:
            data = await self._request("POST", "/user/repos", payload)
        return data

    async def create_pr(
        self, owner: str, repo: str, title: str, head: str, base: str = "main", body: str = ""
    ) -> dict | None:
        """Create a pull request. Returns PR data or None on failure."""
        if self._client is None:
            return None
        payload: dict[str, Any] = {"title": title, "head": head, "base": base}
        if body:
            payload["body"] = body
        path = f"/repos/{owner}/{repo}/pulls"
        return await self._request("POST", path, payload)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _make_jwt(self) -> str:
        """Create a signed JWT for GitHub App authentication (expires in 10 min)."""
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self._settings.app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _get_installation_token(self) -> str | None:
        """Exchange the GitHub App JWT for an installation access token."""
        if self._installation_token and time.time() < self._token_expiry - 60:
            return self._installation_token
        if self._private_key is None:
            return None
        jwt_token = self._make_jwt()
        async with httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "studio-agents",
            },
            timeout=httpx.Timeout(30.0),
        ) as client:
            try:
                path = f"/app/installations/{self._settings.installation_id}/access_tokens"
                resp = await client.post(
                    path,
                    headers={"Authorization": f"Bearer {jwt_token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                self._installation_token = data["token"]
                # Token typically expires in 1 hour; be conservative
                self._token_expiry = int(time.time()) + 3000
                return self._installation_token
            except Exception as exc:
                logger.warning("GitHub App token exchange failed: %s", exc)
                return None

    async def _request(
        self, method: str, path: str, json_body: dict | None = None
    ) -> dict | None:
        """Make an authenticated API request. Returns parsed JSON or None on failure."""
        if self._client is None:
            return None
        await self._rate_limiter.wait_if_needed()
        token = await self._get_installation_token()
        if token is None:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self._client.request(method, path, json=json_body, headers=headers)
            self._rate_limiter.update(dict(resp.headers))
            if resp.status_code == 401:
                # Token may have expired — force refresh and retry once
                self._installation_token = None
                token = await self._get_installation_token()
                if token is None:
                    return None
                headers["Authorization"] = f"Bearer {token}"
                resp = await self._client.request(method, path, json=json_body, headers=headers)
                self._rate_limiter.update(dict(resp.headers))
            if resp.status_code in (403, 429):
                self._rate_limiter.backoff(120)
                logger.warning("GitHub API %s %s returned %d", method, path, resp.status_code)
                return None
            resp.raise_for_status()
            return resp.json() if resp.status_code != 204 else {}
        except Exception as exc:
            logger.warning("GitHub API %s %s failed: %s", method, path, exc)
            return None
