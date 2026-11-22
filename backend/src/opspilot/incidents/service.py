"""Alerts in, incidents out.

Monitoring tools send the same alert again and again while a problem lasts. Each alert has a
fingerprint (from the tool, or computed from its labels); while an incident with that
fingerprint is unresolved, repeats only bump its counter. The deduplication is one
INSERT ... ON CONFLICT against a partial unique index, so even a burst of identical alerts
arriving at the same instant opens exactly one incident.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, literal_column, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.models import Incident, IncidentEvent, IncidentStatus, Severity

Action = Literal["opened", "deduplicated", "resolved", "ignored"]


@dataclass
class AlertInput:
    title: str
    status: Literal["firing", "resolved"]
    severity: Severity = Severity.MEDIUM
    description: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    fingerprint: str | None = None
    source: str = "api"


@dataclass
class AlertOutcome:
    fingerprint: str
    action: Action
    incident_id: uuid.UUID | None


def fingerprint_of(alert: AlertInput) -> str:
    if alert.fingerprint:
        return alert.fingerprint
    # Same title and labels, same problem. Label order doesn't matter.
    canonical = json.dumps({"title": alert.title, "labels": alert.labels}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def ingest_alert(
    session: AsyncSession, workspace_id: uuid.UUID, alert: AlertInput
) -> AlertOutcome:
    fingerprint = fingerprint_of(alert)

    if alert.status == "resolved":
        incident_id = await session.scalar(
            update(Incident)
            .where(
                Incident.workspace_id == workspace_id,
                Incident.fingerprint == fingerprint,
                Incident.status != IncidentStatus.RESOLVED.value,
            )
            .values(status=IncidentStatus.RESOLVED.value, resolved_at=datetime.now(UTC))
            .returning(Incident.id)
        )
        if incident_id is None:
            return AlertOutcome(fingerprint, "ignored", None)
        session.add(IncidentEvent(incident_id=incident_id, type="resolved", data={"by": "alert"}))
        return AlertOutcome(fingerprint, "resolved", incident_id)

    table = Incident.__table__
    statement = (
        insert(Incident)
        .values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            title=alert.title[:300],
            description=alert.description,
            severity=alert.severity.value,
            status=IncidentStatus.OPEN.value,
            fingerprint=fingerprint,
            source=alert.source,
            labels=alert.labels,
            alert_count=1,
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "fingerprint"],
            index_where=text("status <> 'resolved'"),
            set_={"alert_count": table.c.alert_count + 1, "last_alert_at": func.now()},
        )
        # xmax is 0 for a freshly inserted row: tells "opened" apart from "deduplicated"
        .returning(Incident.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await session.execute(statement)).one()
    if row.inserted:
        session.add(
            IncidentEvent(
                incident_id=row.id,
                type="opened",
                data={"severity": alert.severity.value, "source": alert.source},
            )
        )
        return AlertOutcome(fingerprint, "opened", row.id)
    return AlertOutcome(fingerprint, "deduplicated", row.id)


def investigation_question(incident: Incident) -> str:
    labels = ", ".join(f"{k}={v}" for k, v in sorted(incident.labels.items())) or "none"
    return (
        f"An alert is firing: {incident.title}\n"
        f"Severity: {incident.severity}\n"
        f"Details: {incident.description or 'none'}\n"
        f"Labels: {labels}\n\n"
        "Using our runbooks and postmortems: what is the likely cause, and what should the "
        "on-call engineer check and do first?"
    )


def should_auto_investigate(severity: str, threshold: str) -> bool:
    if threshold == "none":
        return False
    return Severity(severity).rank >= Severity(threshold).rank
