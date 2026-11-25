"""Small read-only clients for Prometheus, Loki and GitHub, over one shared httpx client.

Every call has a timeout, every response size is bounded before it reaches a language
model, and every failure becomes a TelemetryError whose message is safe to show the model
and the user (no tokens, no stack traces).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from opspilot.telemetry.urls import UnsafeUrl, ensure_public_host


class TelemetryError(Exception):
    pass


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str | None
    # Re-checked on every request in production: a hostname can be repointed after it was
    # saved (DNS rebinding)
    public_only: bool


async def _get(
    http: httpx.AsyncClient,
    endpoint: Endpoint,
    path: str,
    params: dict[str, Any],
    *,
    name: str,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> Any:
    if endpoint.public_only:
        try:
            await ensure_public_host(endpoint.url)
        except UnsafeUrl as exc:
            raise TelemetryError(f"{name}: {exc}") from exc
    all_headers = {"Accept": "application/json", **(headers or {})}
    if endpoint.token:
        all_headers["Authorization"] = f"Bearer {endpoint.token}"
    try:
        response = await http.get(
            f"{endpoint.url}{path}", params=params, headers=all_headers, timeout=timeout_seconds
        )
    except httpx.TimeoutException as exc:
        raise TelemetryError(f"{name} didn't answer within {timeout_seconds:g}s") from exc
    except httpx.HTTPError as exc:
        raise TelemetryError(f"{name} is unreachable ({type(exc).__name__})") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = None
    if isinstance(body, dict):
        # Prometheus and Loki explain problems in "error"; GitHub in "message"
        detail = body.get("error") or body.get("message")
    if response.status_code == 401:
        raise TelemetryError(f"{name} rejected the credentials (HTTP 401)")
    if response.status_code == 403:
        if detail and "rate limit" in str(detail).lower():
            raise TelemetryError(f"{name}'s rate limit is used up; a token raises it")
        raise TelemetryError(f"{name} refused access (HTTP 403)")
    if response.status_code == 404:
        raise TelemetryError(f"{name}: not found")
    if body is None:
        raise TelemetryError(f"{name} returned something that isn't JSON")
    if response.status_code >= 400:
        raise TelemetryError(f"{name} refused the request: {detail or response.status_code}")
    return body


# --------------------------------------------------------------------------- Prometheus --


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]]  # (unix time, value)


class PrometheusClient:
    def __init__(self, http: httpx.AsyncClient, endpoint: Endpoint, timeout: float) -> None:
        self._http, self._endpoint, self._timeout = http, endpoint, timeout

    async def _api(self, path: str, params: dict[str, Any]) -> Any:
        body = await _get(
            self._http,
            self._endpoint,
            path,
            params,
            name="Prometheus",
            timeout_seconds=self._timeout,
        )
        if body.get("status") != "success":
            raise TelemetryError(f"Prometheus: {body.get('error', 'query failed')}")
        return body["data"]

    async def query_range(self, promql: str, start: datetime, end: datetime) -> list[Series]:
        # About 60 points whatever the window, never finer than 15s
        step = max(15, int((end - start).total_seconds() / 60))
        data = await self._api(
            "/api/v1/query_range",
            {
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
                # Prometheus stops the query itself rather than us waiting on it
                "timeout": f"{int(self._timeout)}s",
            },
        )
        if data.get("resultType") != "matrix":
            raise TelemetryError("Prometheus: expected a range of values")
        return [
            Series(
                labels=row.get("metric", {}),
                points=[(float(t), _number(v)) for t, v in row.get("values", [])],
            )
            for row in data.get("result", [])
        ]

    async def metric_names(self) -> list[str]:
        names = await self._api("/api/v1/label/__name__/values", {})
        return [str(n) for n in names]

    async def check(self) -> str:
        info = await self._api("/api/v1/status/buildinfo", {})
        return f"Prometheus {info.get('version', '')}".strip()


def _number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return float("nan")


# --------------------------------------------------------------------------------- Loki --

# Label values and filter text become part of a LogQL query: allow only what can't break out
SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def logql_string(text: str) -> str:
    """A LogQL double-quoted string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_logql(service: str, contains: str | None) -> str:
    if not SERVICE_NAME.match(service):
        raise TelemetryError("A service name may only use letters, digits, '.', '_' and '-'")
    query = "{service=" + logql_string(service) + "}"
    if contains:
        query += " |= " + logql_string(contains)
    return query


@dataclass
class LogLine:
    at: datetime
    labels: dict[str, str]
    line: str


class LokiClient:
    def __init__(self, http: httpx.AsyncClient, endpoint: Endpoint, timeout: float) -> None:
        self._http, self._endpoint, self._timeout = http, endpoint, timeout

    async def _api(self, path: str, params: dict[str, Any]) -> Any:
        body = await _get(
            self._http, self._endpoint, path, params, name="Loki", timeout_seconds=self._timeout
        )
        if body.get("status") != "success":
            raise TelemetryError(f"Loki: {body.get('error', 'query failed')}")
        return body["data"]

    async def query_range(
        self, logql: str, start: datetime, end: datetime, limit: int
    ) -> list[LogLine]:
        data = await self._api(
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": "backward",  # newest first
            },
        )
        lines = [
            LogLine(
                at=datetime.fromtimestamp(int(ts) / 1e9, UTC),
                labels=stream.get("stream", {}),
                line=line,
            )
            for stream in data.get("result", [])
            for ts, line in stream.get("values", [])
        ]
        lines.sort(key=lambda entry: entry.at, reverse=True)
        return lines[:limit]

    async def services(self) -> list[str]:
        return [str(s) for s in await self._api("/loki/api/v1/label/service/values", {})]

    async def check(self) -> str:
        await self._api("/loki/api/v1/labels", {})
        return "Loki is answering"


# ------------------------------------------------------------------------------- GitHub --

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


@dataclass
class Commit:
    sha: str
    at: datetime
    author: str
    message: str
    url: str


class GitHubClient:
    """Public repositories need no token (60 requests an hour per IP); a token raises the
    limit and allows private repositories."""

    def __init__(
        self, http: httpx.AsyncClient, endpoint: Endpoint, repository: str, timeout: float
    ) -> None:
        if not REPOSITORY.match(repository):
            raise TelemetryError("The GitHub repository must look like owner/name")
        self._http, self._endpoint, self._timeout = http, endpoint, timeout
        self.repository = repository

    async def _api(self, path: str, params: dict[str, Any]) -> Any:
        return await _get(
            self._http,
            self._endpoint,
            path,
            params,
            name="GitHub",
            timeout_seconds=self._timeout,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )

    async def commits(self, since: datetime, limit: int) -> list[Commit]:
        rows = await self._api(
            f"/repos/{self.repository}/commits",
            {"since": since.isoformat(), "per_page": min(limit, 100)},
        )
        commits = []
        for row in rows[:limit]:
            info = row.get("commit", {})
            author = info.get("author") or {}
            commits.append(
                Commit(
                    sha=row.get("sha", ""),
                    at=datetime.fromisoformat(author.get("date", "1970-01-01T00:00:00Z")),
                    author=author.get("name") or (row.get("author") or {}).get("login", ""),
                    message=(info.get("message") or "").splitlines()[0][:200]
                    if info.get("message")
                    else "",
                    url=row.get("html_url", ""),
                )
            )
        return commits

    async def check(self) -> str:
        repo = await self._api(f"/repos/{self.repository}", {})
        return f"GitHub repository {repo.get('full_name', self.repository)}"
