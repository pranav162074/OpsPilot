"""Telemetry for a workspace: where it lives (integrations), what changed (deploys), and
read-only queries over it (metrics, logs, changes). The agent uses the same service layer."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opspilot.api.deps import Admin, Responder, SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.models import Deploy, Integration, IntegrationKind
from opspilot.security.secrets import SecretBox, SecretsUnavailable
from opspilot.telemetry.clients import REPOSITORY, TelemetryError
from opspilot.telemetry.service import (
    MAX_LOG_LINES,
    MAX_WINDOW_MINUTES,
    load_telemetry,
    metric_names,
    query_metrics,
    recent_changes,
    search_logs,
)
from opspilot.telemetry.urls import UnsafeUrl, ensure_public_host, parse_base_url

router = APIRouter(prefix="/v1", tags=["telemetry"])


def _http(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.telemetry_http
    return client


def _telemetry_error(exc: TelemetryError) -> ApiError:
    # The workspace's own Prometheus/Loki/GitHub failed, not OpsPilot: a gateway error
    return ApiError(502, "telemetry_error", str(exc))


# --------------------------------------------------------------------------- integrations --


class IntegrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: Annotated[str, Field(min_length=1, max_length=500)]
    # Sent as "Authorization: Bearer <token>". Stored encrypted; never returned.
    token: SecretStr | None = None
    # GitHub only: owner/name
    repository: Annotated[str | None, Field(max_length=201)] = None


class IntegrationOut(BaseModel):
    kind: IntegrationKind
    url: str
    has_token: bool
    repository: str | None
    updated_at: datetime


def _integration_out(row: Integration) -> IntegrationOut:
    return IntegrationOut(
        kind=IntegrationKind(row.kind),
        url=row.url,
        has_token=row.secret_encrypted is not None,
        repository=row.options.get("repository"),
        updated_at=row.updated_at,
    )


DEFAULT_URLS = {IntegrationKind.GITHUB: "https://api.github.com"}


@router.get("/integrations")
async def list_integrations(principal: Viewer, session: SessionDep) -> list[IntegrationOut]:
    rows = await session.scalars(
        select(Integration)
        .where(Integration.workspace_id == principal.workspace.id)
        .order_by(Integration.kind)
    )
    return [_integration_out(row) for row in rows]


@router.put("/integrations/{kind}")
async def save_integration(
    kind: IntegrationKind,
    body: IntegrationIn,
    principal: Admin,
    session: SessionDep,
    settings: SettingsDep,
) -> IntegrationOut:
    """Connects (or reconnects) a workspace's Prometheus, Loki or GitHub repository."""
    try:
        url = parse_base_url(body.url)
        if not settings.allow_private_integration_urls:
            await ensure_public_host(url)
    except UnsafeUrl as exc:
        raise ApiError(422, "invalid_url", str(exc)) from exc
    options: dict[str, Any] = {}
    if kind == IntegrationKind.GITHUB:
        if not body.repository or not REPOSITORY.match(body.repository):
            raise ApiError(422, "invalid_repository", "Give the repository as owner/name")
        options["repository"] = body.repository
    elif body.repository is not None:
        raise ApiError(422, "invalid_request", "Only GitHub integrations take a repository")

    secret = None
    if body.token is not None and body.token.get_secret_value():
        try:
            secret = SecretBox(settings.integrations_key).encrypt(body.token.get_secret_value())
        except SecretsUnavailable as exc:
            raise ApiError(409, "secrets_unavailable", str(exc)) from exc

    values = {
        "workspace_id": principal.workspace.id,
        "kind": kind.value,
        "url": url,
        "secret_encrypted": secret,
        "options": options,
    }
    statement = (
        insert(Integration)
        .values(id=uuid.uuid4(), **values)
        .on_conflict_do_update(
            index_elements=["workspace_id", "kind"],
            set_={**values, "updated_at": datetime.now(UTC)},
        )
        .returning(Integration)
    )
    row = (await session.scalars(statement, execution_options={"populate_existing": True})).one()
    await session.commit()
    return _integration_out(row)


@router.delete("/integrations/{kind}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    kind: IntegrationKind, principal: Admin, session: SessionDep
) -> Response:
    row = await session.scalar(
        select(Integration).where(
            Integration.workspace_id == principal.workspace.id, Integration.kind == kind.value
        )
    )
    if row is None:
        raise ApiError(404, "not_found", "That integration isn't set up")
    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CheckResult(BaseModel):
    kind: IntegrationKind
    ok: bool
    message: str


@router.post("/integrations/{kind}/check")
async def check_integration(
    kind: IntegrationKind,
    request: Request,
    principal: Admin,
    session: SessionDep,
    settings: SettingsDep,
) -> CheckResult:
    """Tries the connection with the saved URL and token."""
    telemetry = await load_telemetry(session, principal.workspace.id, _http(request), settings)
    client = {
        IntegrationKind.PROMETHEUS: telemetry.prometheus,
        IntegrationKind.LOKI: telemetry.loki,
        IntegrationKind.GITHUB: telemetry.github,
    }[kind]
    if client is None:
        raise ApiError(
            404, "not_found", "That integration isn't set up (or its token can't be read)"
        )
    try:
        message = await client.check()
    except TelemetryError as exc:
        return CheckResult(kind=kind, ok=False, message=str(exc))
    return CheckResult(kind=kind, ok=True, message=message)


# ------------------------------------------------------------------------------ deploys --


class DeployIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Annotated[str, Field(min_length=1, max_length=100)]
    version: Annotated[str, Field(min_length=1, max_length=100)]
    environment: Annotated[str, Field(min_length=1, max_length=50)] = "production"
    commit_sha: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{7,64}$")] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    deployed_by: Annotated[str | None, Field(max_length=100)] = None
    # Defaults to now; CI can send the real time if it reports late
    deployed_at: datetime | None = None


class DeployOut(BaseModel):
    id: uuid.UUID
    service: str
    version: str
    environment: str
    commit_sha: str | None
    description: str | None
    deployed_by: str | None
    deployed_at: datetime


def _deploy_out(row: Deploy) -> DeployOut:
    return DeployOut(
        id=row.id,
        service=row.service,
        version=row.version,
        environment=row.environment,
        commit_sha=row.commit_sha,
        description=row.description,
        deployed_by=row.deployed_by,
        deployed_at=row.deployed_at,
    )


@router.post("/deploys", status_code=status.HTTP_201_CREATED)
async def record_deploy(body: DeployIn, principal: Responder, session: SessionDep) -> DeployOut:
    """CI calls this after each release, so investigations can line problems up with changes."""
    deployed_at = body.deployed_at or datetime.now(UTC)
    if deployed_at.tzinfo is None:
        raise ApiError(422, "invalid_request", "deployed_at needs a timezone, e.g. ...Z")
    row = Deploy(
        workspace_id=principal.workspace.id,
        **body.model_dump(exclude={"deployed_at"}),
        deployed_at=deployed_at,
    )
    session.add(row)
    await session.commit()
    return _deploy_out(row)


@router.get("/deploys")
async def list_deploys(
    principal: Viewer,
    session: SessionDep,
    service: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[DeployOut]:
    query = select(Deploy).where(Deploy.workspace_id == principal.workspace.id)
    if service:
        query = query.where(Deploy.service == service)
    rows = await session.scalars(query.order_by(Deploy.deployed_at.desc()).limit(limit))
    return [_deploy_out(row) for row in rows]


# ------------------------------------------------------------------------------ queries --


class MetricsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, Field(min_length=1, max_length=500, description="PromQL")]
    minutes: Annotated[int, Field(ge=1, le=MAX_WINDOW_MINUTES)] = 60


class Point(BaseModel):
    at: datetime
    value: float | None


class SeriesOut(BaseModel):
    labels: dict[str, str]
    last: float | None
    min: float | None
    max: float | None
    avg: float | None
    samples: list[Point]


class MetricsOut(BaseModel):
    query: str
    start: datetime
    end: datetime
    total_series: int
    series: list[SeriesOut]


def _finite(value: float | None) -> float | None:
    return None if value is None or value != value else value  # NaN isn't valid JSON


@router.post("/telemetry/metrics")
async def metrics(
    body: MetricsQuery,
    request: Request,
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
) -> MetricsOut:
    telemetry = await load_telemetry(session, principal.workspace.id, _http(request), settings)
    try:
        result = await query_metrics(telemetry, body.query, body.minutes)
    except TelemetryError as exc:
        raise _telemetry_error(exc) from exc
    return MetricsOut(
        query=result.query,
        start=result.start,
        end=result.end,
        total_series=result.total_series,
        series=[
            SeriesOut(
                labels=s.labels,
                last=_finite(s.last),
                min=_finite(s.minimum),
                max=_finite(s.maximum),
                avg=_finite(s.average),
                samples=[Point(at=t, value=_finite(v)) for t, v in s.points],
            )
            for s in result.series
        ],
    )


@router.get("/telemetry/metric-names")
async def list_metric_names(
    request: Request,
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
    contains: Annotated[str | None, Query(max_length=100)] = None,
) -> list[str]:
    telemetry = await load_telemetry(session, principal.workspace.id, _http(request), settings)
    try:
        return await metric_names(telemetry, contains)
    except TelemetryError as exc:
        raise _telemetry_error(exc) from exc


class LogsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
    contains: Annotated[str | None, Field(max_length=200)] = None
    minutes: Annotated[int, Field(ge=1, le=MAX_WINDOW_MINUTES)] = 60
    limit: Annotated[int, Field(ge=1, le=MAX_LOG_LINES)] = 50


class LogLineOut(BaseModel):
    at: datetime
    labels: dict[str, str]
    line: str


class LogsOut(BaseModel):
    query: str
    start: datetime
    end: datetime
    lines: list[LogLineOut]


@router.post("/telemetry/logs")
async def logs(
    body: LogsQuery,
    request: Request,
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
) -> LogsOut:
    telemetry = await load_telemetry(session, principal.workspace.id, _http(request), settings)
    try:
        result = await search_logs(telemetry, body.service, body.contains, body.minutes, body.limit)
    except TelemetryError as exc:
        raise _telemetry_error(exc) from exc
    return LogsOut(
        query=result.query,
        start=result.start,
        end=result.end,
        lines=[LogLineOut(at=x.at, labels=x.labels, line=x.line) for x in result.lines],
    )


class ChangeOut(BaseModel):
    kind: str
    at: datetime
    summary: str
    service: str | None
    version: str | None
    sha: str | None
    author: str | None
    url: str | None


class ChangesOut(BaseModel):
    since: datetime
    warning: str | None
    changes: list[ChangeOut]


@router.get("/telemetry/changes")
async def changes(
    request: Request,
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
    hours: Annotated[int, Query(ge=1, le=336)] = 24,
    service: Annotated[str | None, Query(max_length=100)] = None,
) -> ChangesOut:
    """Deploys recorded by CI, plus the GitHub repository's commits when it's connected."""
    telemetry = await load_telemetry(session, principal.workspace.id, _http(request), settings)
    try:
        result = await recent_changes(session, telemetry, principal.workspace.id, hours, service)
    except TelemetryError as exc:
        raise _telemetry_error(exc) from exc
    return ChangesOut(
        since=result.since,
        warning=result.warning,
        changes=[ChangeOut(**vars(c)) for c in result.changes],
    )
