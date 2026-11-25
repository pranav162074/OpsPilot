"""A workspace's telemetry, ready to query: built from its saved integrations, shared by the
API endpoints and the investigation agent so both read exactly the same data."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings
from opspilot.models import Deploy, Integration, IntegrationKind
from opspilot.security.secrets import SecretBox, SecretsUnavailable
from opspilot.telemetry.clients import (
    Commit,
    Endpoint,
    GitHubClient,
    LogLine,
    LokiClient,
    PrometheusClient,
    Series,
    TelemetryError,
    build_logql,
)

MAX_WINDOW_MINUTES = 6 * 60
MAX_SERIES = 10
MAX_LOG_LINES = 100
MAX_LOG_LINE_CHARS = 400
MAX_PROMQL_CHARS = 500


@dataclass
class Telemetry:
    prometheus: PrometheusClient | None = None
    loki: LokiClient | None = None
    github: GitHubClient | None = None


async def load_telemetry(
    session: AsyncSession, workspace_id: uuid.UUID, http: httpx.AsyncClient, settings: Settings
) -> Telemetry:
    rows = (
        await session.scalars(select(Integration).where(Integration.workspace_id == workspace_id))
    ).all()
    box = SecretBox(settings.integrations_key)
    telemetry = Telemetry()
    timeout = settings.telemetry_timeout_seconds
    for row in rows:
        token = None
        if row.secret_encrypted:
            try:
                token = box.decrypt(row.secret_encrypted)
            except SecretsUnavailable:
                continue  # unusable until the token is entered again
        endpoint = Endpoint(
            url=row.url, token=token, public_only=not settings.allow_private_integration_urls
        )
        if row.kind == IntegrationKind.PROMETHEUS:
            telemetry.prometheus = PrometheusClient(http, endpoint, timeout)
        elif row.kind == IntegrationKind.LOKI:
            telemetry.loki = LokiClient(http, endpoint, timeout)
        elif row.kind == IntegrationKind.GITHUB:
            telemetry.github = GitHubClient(
                http, endpoint, str(row.options.get("repository", "")), timeout
            )
    return telemetry


def _window(minutes: int, end: datetime | None) -> tuple[datetime, datetime]:
    if not 1 <= minutes <= MAX_WINDOW_MINUTES:
        raise TelemetryError(f"The window must be 1 to {MAX_WINDOW_MINUTES} minutes")
    end = end or datetime.now(UTC)
    return end - timedelta(minutes=minutes), end


# ------------------------------------------------------------------------------ metrics --


@dataclass
class SeriesSummary:
    labels: dict[str, str]
    last: float | None
    minimum: float | None
    maximum: float | None
    average: float | None
    points: list[tuple[datetime, float]]  # a few evenly spaced samples, for the shape


@dataclass
class MetricsResult:
    query: str
    start: datetime
    end: datetime
    series: list[SeriesSummary]
    total_series: int


def summarise(series: Series, samples: int = 8) -> SeriesSummary:
    values = [v for _, v in series.points if not math.isnan(v)]
    picked = series.points
    if len(picked) > samples:
        stride = (len(picked) - 1) / (samples - 1)
        picked = [picked[round(i * stride)] for i in range(samples)]
    return SeriesSummary(
        labels=series.labels,
        last=values[-1] if values else None,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        average=sum(values) / len(values) if values else None,
        points=[(datetime.fromtimestamp(t, UTC), v) for t, v in picked],
    )


async def query_metrics(
    telemetry: Telemetry, promql: str, minutes: int, end: datetime | None = None
) -> MetricsResult:
    if telemetry.prometheus is None:
        raise TelemetryError("No Prometheus is connected to this workspace")
    promql = promql.strip()
    if not promql or len(promql) > MAX_PROMQL_CHARS:
        raise TelemetryError(f"The query must be 1 to {MAX_PROMQL_CHARS} characters")
    start, end = _window(minutes, end)
    series = await telemetry.prometheus.query_range(promql, start, end)
    # The series that moved most come first: they're the interesting ones in an incident
    summaries = sorted(
        (summarise(s) for s in series),
        key=lambda s: -((s.maximum or 0) - (s.minimum or 0)),
    )
    return MetricsResult(promql, start, end, summaries[:MAX_SERIES], len(series))


async def metric_names(telemetry: Telemetry, contains: str | None, limit: int = 100) -> list[str]:
    if telemetry.prometheus is None:
        raise TelemetryError("No Prometheus is connected to this workspace")
    names = await telemetry.prometheus.metric_names()
    if contains:
        names = [n for n in names if contains.lower() in n.lower()]
    return names[:limit]


# --------------------------------------------------------------------------------- logs --


@dataclass
class LogsResult:
    query: str
    start: datetime
    end: datetime
    lines: list[LogLine]


async def search_logs(
    telemetry: Telemetry,
    service: str,
    contains: str | None,
    minutes: int,
    limit: int,
    end: datetime | None = None,
) -> LogsResult:
    if telemetry.loki is None:
        raise TelemetryError("No Loki is connected to this workspace")
    if contains is not None and len(contains) > 200:
        raise TelemetryError("The search text is limited to 200 characters")
    logql = build_logql(service, contains or None)
    start, end = _window(minutes, end)
    lines = await telemetry.loki.query_range(logql, start, end, min(limit, MAX_LOG_LINES))
    for entry in lines:
        if len(entry.line) > MAX_LOG_LINE_CHARS:
            entry.line = entry.line[:MAX_LOG_LINE_CHARS] + " [...]"
    return LogsResult(logql, start, end, lines)


# ------------------------------------------------------------------------------ changes --


@dataclass
class Change:
    kind: str  # "deploy" or "commit"
    at: datetime
    summary: str
    service: str | None = None
    version: str | None = None
    sha: str | None = None
    author: str | None = None
    url: str | None = None


@dataclass
class ChangesResult:
    since: datetime
    changes: list[Change]
    # Set when GitHub couldn't be read; deploys are still listed
    warning: str | None = None


async def recent_changes(
    session: AsyncSession,
    telemetry: Telemetry,
    workspace_id: uuid.UUID,
    hours: int,
    service: str | None = None,
    limit: int = 30,
    now: datetime | None = None,
) -> ChangesResult:
    if not 1 <= hours <= 24 * 14:
        raise TelemetryError("The window must be 1 to 336 hours")
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    query = select(Deploy).where(Deploy.workspace_id == workspace_id, Deploy.deployed_at >= since)
    if service:
        query = query.where(Deploy.service == service)
    deploys = (await session.scalars(query.order_by(Deploy.deployed_at.desc()).limit(limit))).all()
    changes = [
        Change(
            kind="deploy",
            at=d.deployed_at,
            summary=d.description or f"{d.service} {d.version} deployed to {d.environment}",
            service=d.service,
            version=d.version,
            sha=d.commit_sha,
            author=d.deployed_by,
        )
        for d in deploys
    ]
    warning = None
    if telemetry.github is not None:
        try:
            commits: list[Commit] = await telemetry.github.commits(since, limit)
        except TelemetryError as exc:
            warning = str(exc)
        else:
            changes += [
                Change(
                    kind="commit",
                    at=c.at,
                    summary=c.message,
                    sha=c.sha,
                    author=c.author,
                    url=c.url,
                )
                for c in commits
            ]
    changes.sort(key=lambda c: c.at, reverse=True)
    return ChangesResult(since, changes[:limit], warning)


# ------------------------------------------------------------- text for the language model --


def _fmt_number(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.4g}"


def _fmt_labels(labels: dict[str, str]) -> str:
    shown = {k: v for k, v in labels.items() if k != "__name__"}
    name = labels.get("__name__", "")
    return name + "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(shown.items())) + "}"


def metrics_text(result: MetricsResult) -> str:
    if not result.series:
        return (
            f"No data for `{result.query}` in this window. "
            "Check the metric name with list_metric_names."
        )
    header = (
        f"`{result.query}` from {result.start:%H:%M} to {result.end:%H:%M} UTC, "
        f"{result.total_series} series"
    )
    if result.total_series > len(result.series):
        header += f" (showing the {len(result.series)} that changed most)"
    lines = [header]
    for s in result.series:
        shape = ", ".join(f"{t:%H:%M}={_fmt_number(v)}" for t, v in s.points)
        lines.append(
            f"- {_fmt_labels(s.labels)}: last {_fmt_number(s.last)}, min {_fmt_number(s.minimum)}, "
            f"max {_fmt_number(s.maximum)}, avg {_fmt_number(s.average)}; over time: {shape}"
        )
    return "\n".join(lines)


def logs_text(result: LogsResult) -> str:
    if not result.lines:
        return f"No log lines match `{result.query}` in this window."
    lines = [f"`{result.query}`, {len(result.lines)} newest lines (newest first):"]
    lines += [f"{entry.at:%H:%M:%S} {entry.line}" for entry in result.lines]
    return "\n".join(lines)


def changes_text(result: ChangesResult) -> str:
    lines = [f"Changes since {result.since:%Y-%m-%d %H:%M} UTC (newest first):"]
    if result.warning:
        lines.append(f"(GitHub couldn't be read: {result.warning})")
    if not result.changes:
        lines.append("None recorded.")
    for c in result.changes:
        who = f" by {c.author}" if c.author else ""
        if c.kind == "deploy":
            sha = f" (commit {c.sha[:7]})" if c.sha else ""
            lines.append(f"- {c.at:%Y-%m-%d %H:%M} DEPLOY {c.summary}{sha}{who}")
        else:
            lines.append(f"- {c.at:%Y-%m-%d %H:%M} COMMIT {(c.sha or '')[:7]} {c.summary}{who}")
    return "\n".join(lines)
