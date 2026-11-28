"""The parts of GitHub's REST API a repository sync needs: one commit lookup, one recursive
tree listing, and the blobs that changed. A sync at an unchanged commit costs one request."""

import base64
import re
from dataclasses import dataclass
from typing import Any

import httpx

REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
TIMEOUT = 20.0


class GitHubError(Exception):
    def __init__(self, message: str, *, transient: bool, retry_after: float | None = None):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


@dataclass(frozen=True)
class TreeEntry:
    path: str
    sha: str
    size: int


@dataclass(frozen=True)
class Tree:
    commit: str
    entries: list[TreeEntry]
    # GitHub stops listing past ~100,000 entries; the sync says so instead of pretending
    truncated: bool


class GitHub:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str | None) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "OpsPilot",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        }

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            response = await self.http.get(
                f"{self.base_url}{path}", params=params, headers=self.headers, timeout=TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub unreachable ({type(exc).__name__})", transient=True) from exc
        if response.is_success:
            return response.json()
        remaining = response.headers.get("x-ratelimit-remaining")
        if response.status_code in (403, 429) and (
            remaining == "0" or "retry-after" in response.headers
        ):
            retry_after = response.headers.get("retry-after")
            raise GitHubError(
                "GitHub's rate limit is used up",
                transient=True,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else 60.0,
            )
        if response.status_code in (401, 403):
            raise GitHubError(
                "GitHub refused the token (revoked, expired, or missing access to the repository)",
                transient=False,
            )
        if response.status_code == 404:
            raise GitHubError(
                "Not found: check the repository and branch (a private repository needs a token)",
                transient=False,
            )
        if response.status_code == 409:
            raise GitHubError("The repository is empty", transient=False)
        return self._server_error(response)

    @staticmethod
    def _server_error(response: httpx.Response) -> Any:
        raise GitHubError(f"GitHub answered HTTP {response.status_code}", transient=True)

    async def default_branch(self, repository: str) -> str:
        data = await self._get(f"/repos/{repository}")
        return str(data["default_branch"])

    async def commit(self, repository: str, ref: str) -> str:
        data = await self._get(f"/repos/{repository}/commits/{ref}")
        return str(data["sha"])

    async def tree(self, repository: str, commit: str) -> Tree:
        data = await self._get(f"/repos/{repository}/git/trees/{commit}", recursive="1")
        entries = [
            TreeEntry(path=e["path"], sha=e["sha"], size=int(e.get("size", 0)))
            for e in data.get("tree", [])
            if e.get("type") == "blob"
        ]
        return Tree(commit=commit, entries=entries, truncated=bool(data.get("truncated")))

    async def blob(self, repository: str, sha: str) -> bytes:
        data = await self._get(f"/repos/{repository}/git/blobs/{sha}")
        if data.get("encoding") != "base64":
            raise GitHubError(f"Unexpected blob encoding {data.get('encoding')!r}", transient=False)
        return base64.b64decode(data["content"])
