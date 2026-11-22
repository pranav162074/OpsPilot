import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update

from opspilot.api.deps import Responder, SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.incidents.service import (
    AlertInput,
    ingest_alert,
    investigation_question,
    should_auto_investigate,
)
from opspilot.models import (
    Incident,
    IncidentEvent,
    IncidentStatus,
    Investigation,
    InvestigationStatus,
    Severity,
)

router = APIRouter(prefix="/v1", tags=["incidents"])

Label = Annotated[str, Field(max_length=500)]


class GenericAlert(BaseModel):
    """The simple format: one alert per request."""

    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, Field(min_length=1, max_length=300)]
    status: Literal["firing", "resolved"] = "firing"
    severity: Severity = Severity.MEDIUM
    description: Annotated[str, Field(max_length=5000)] | None = None
    labels: dict[Annotated[str, Field(max_length=100)], Label] = {}
    fingerprint: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    source: Annotated[str, Field(max_length=50)] = "api"


class AlertmanagerAlert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    fingerprint: str | None = None


class AlertmanagerPayload(BaseModel):
    """Prometheus Alertmanager's webhook format: several alerts per request."""

    version: str
    alerts: Annotated[list[AlertmanagerAlert], Field(max_length=100)]


# Alertmanager's severity label varies by team; map the common spellings
SEVERITY_LABELS = {
    "critical": Severity.CRITICAL,
    "page": Severity.CRITICAL,
    "high": Severity.HIGH,
    "error": Severity.HIGH,
    "major": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "medium": Severity.MEDIUM,
    "info": Severity.LOW,
    "low": Severity.LOW,
}


def _from_alertmanager(alert: AlertmanagerAlert) -> AlertInput:
    return AlertInput(
        title=alert.annotations.get("summary") or alert.labels.get("alertname", "Alert"),
        status=alert.status,
        severity=SEVERITY_LABELS.get(alert.labels.get("severity", "").lower(), Severity.MEDIUM),
        description=alert.annotations.get("description"),
        labels=alert.labels,
        fingerprint=alert.fingerprint,
        source="alertmanager",
    )


def _parse(body: dict[str, Any]) -> list[AlertInput]:
    try:
        if "alerts" in body:
            return [_from_alertmanager(a) for a in AlertmanagerPayload.model_validate(body).alerts]
        generic = GenericAlert.model_validate(body)
    except ValidationError as exc:
        # Reported in the usual 422 shape, with field names
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors()]
        ) from exc
    return [
        AlertInput(
            title=generic.title,
            status=generic.status,
            severity=generic.severity,
            description=generic.description,
            labels=generic.labels,
            fingerprint=generic.fingerprint,
            source=generic.source,
        )
    ]


async def _start_investigation(
    request: Request, session: SessionDep, incident: Incident, created_by: uuid.UUID | None
) -> Investigation:
    investigation = Investigation(
        workspace_id=incident.workspace_id,
        created_by_key_id=created_by,
        question=investigation_question(incident),
        status=InvestigationStatus.RUNNING.value,
        model=request.app.state.settings.llm_model,
        citations=[],
        trace=[],
    )
    session.add(investigation)
    await session.flush()
    incident.investigation_id = investigation.id
    session.add(
        IncidentEvent(
            incident_id=incident.id,
            type="investigation_started",
            data={"investigation_id": str(investigation.id)},
        )
    )
    return investigation


@router.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alerts(
    request: Request,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Webhook for monitoring tools: the simple format, or Prometheus Alertmanager's.
    Point the tool at this URL with a responder API key as the bearer token."""
    outcomes = [
        await ingest_alert(session, principal.workspace.id, alert) for alert in _parse(body)
    ]

    # New, serious incidents get an investigation, run in the background by the worker
    to_enqueue: list[uuid.UUID] = []
    if request.app.state.llm is not None:
        for outcome in outcomes:
            if outcome.action != "opened" or outcome.incident_id is None:
                continue
            incident = await session.get(Incident, outcome.incident_id)
            if incident is None or not should_auto_investigate(
                incident.severity, settings.auto_investigate_severity
            ):
                continue
            investigation = await _start_investigation(
                request, session, incident, principal.api_key.id
            )
            to_enqueue.append(investigation.id)
    await session.commit()
    # Queued only after the commit, so the worker always finds the rows it's told about
    for investigation_id in to_enqueue:
        request.app.state.enqueue_investigation(investigation_id)

    return {
        "results": [
            {
                "fingerprint": o.fingerprint,
                "action": o.action,
                "incident_id": str(o.incident_id) if o.incident_id else None,
            }
            for o in outcomes
        ]
    }


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    description: str | None
    severity: Severity
    status: IncidentStatus
    fingerprint: str
    source: str
    labels: dict[str, Any]
    alert_count: int
    first_alert_at: datetime
    last_alert_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    investigation_id: uuid.UUID | None
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: str
    data: dict[str, Any]
    created_at: datetime


class InvestigationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: InvestigationStatus
    answer: str | None
    citations: list[dict[str, Any]]
    error: str | None


class IncidentDetail(IncidentOut):
    timeline: list[EventOut]
    investigation: InvestigationSummary | None


@router.get("/incidents")
async def list_incidents(
    principal: Viewer,
    session: SessionDep,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[IncidentOut]:
    query = select(Incident).where(Incident.workspace_id == principal.workspace.id)
    if status_filter is not None:
        query = query.where(Incident.status == status_filter.value)
    rows = await session.scalars(
        query.order_by(Incident.created_at.desc(), Incident.id.desc()).limit(limit)
    )
    return [IncidentOut.model_validate(r) for r in rows]


async def _load(session: SessionDep, workspace_id: uuid.UUID, incident_id: uuid.UUID) -> Incident:
    incident = await session.scalar(
        select(Incident)
        .where(Incident.id == incident_id, Incident.workspace_id == workspace_id)
        # Always fresh from the database, even if this session already loaded it
        .execution_options(populate_existing=True)
    )
    if incident is None:
        raise ApiError(404, "not_found", "Incident not found")
    return incident


async def _detail(session: SessionDep, incident: Incident) -> IncidentDetail:
    events = await session.scalars(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident.id)
        .order_by(IncidentEvent.seq)
    )
    investigation = (
        await session.get(Investigation, incident.investigation_id)
        if incident.investigation_id
        else None
    )
    return IncidentDetail(
        **IncidentOut.model_validate(incident).model_dump(),
        timeline=[EventOut.model_validate(e) for e in events],
        investigation=InvestigationSummary.model_validate(investigation) if investigation else None,
    )


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID, principal: Viewer, session: SessionDep
) -> IncidentDetail:
    return await _detail(session, await _load(session, principal.workspace.id, incident_id))


async def _move(
    session: SessionDep,
    principal: Responder,
    incident_id: uuid.UUID,
    to: IncidentStatus,
    allowed_from: list[IncidentStatus],
    timestamp_field: str,
) -> IncidentDetail:
    await _load(session, principal.workspace.id, incident_id)
    moved = await session.scalar(
        update(Incident)
        .where(
            Incident.id == incident_id,
            Incident.status.in_([s.value for s in allowed_from]),
        )
        .values({"status": to.value, timestamp_field: datetime.now(UTC)})
        .returning(Incident.id)
    )
    if moved is None:
        raise ApiError(409, "invalid_state", f"This incident can't be marked {to.value}")
    session.add(
        IncidentEvent(
            incident_id=incident_id,
            type=to.value,
            data={"by": principal.api_key.name},
        )
    )
    await session.commit()
    return await _detail(session, await _load(session, principal.workspace.id, incident_id))


@router.post("/incidents/{incident_id}/acknowledge")
async def acknowledge_incident(
    incident_id: uuid.UUID, principal: Responder, session: SessionDep
) -> IncidentDetail:
    return await _move(
        session,
        principal,
        incident_id,
        IncidentStatus.ACKNOWLEDGED,
        [IncidentStatus.OPEN],
        "acknowledged_at",
    )


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: uuid.UUID, principal: Responder, session: SessionDep
) -> IncidentDetail:
    return await _move(
        session,
        principal,
        incident_id,
        IncidentStatus.RESOLVED,
        [IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED],
        "resolved_at",
    )


@router.post("/incidents/{incident_id}/investigate", status_code=status.HTTP_202_ACCEPTED)
async def investigate_incident(
    incident_id: uuid.UUID,
    request: Request,
    principal: Responder,
    session: SessionDep,
) -> IncidentDetail:
    """Starts a fresh investigation for an incident (again, after new runbooks, say)."""
    if request.app.state.llm is None:
        raise ApiError(
            503, "agent_unavailable", "Investigations need a language model; set GEMINI_API_KEY"
        )
    incident = await _load(session, principal.workspace.id, incident_id)
    investigation = await _start_investigation(request, session, incident, principal.api_key.id)
    await session.commit()
    request.app.state.enqueue_investigation(investigation.id)
    return await _detail(session, incident)
