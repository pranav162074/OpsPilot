"""Human approval for what the agent suggests doing. Nothing the agent proposes is carried out
until a responder approves it here; rejecting it records why."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from opspilot.actions import WebhookUnavailable, load_webhook, payload, send
from opspilot.api.deps import Principal, Responder, SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.models import (
    ActionStatus,
    Incident,
    IncidentEvent,
    Investigation,
    InvestigationAction,
)

router = APIRouter(prefix="/v1", tags=["actions"])


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    investigation_id: uuid.UUID
    title: str
    command: str | None
    rationale: str | None
    risk: str
    citations: list[str]
    # False: the command appears in none of the cited sources. flags: injection heuristics
    # the cited sources trip. Either way, approving needs "confirm_unverified": true.
    verified: bool
    flags: list[str]
    status: ActionStatus
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    result: dict[str, Any] | None
    created_at: datetime


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Annotated[str | None, Field(max_length=2000)] = None


class Approval(Decision):
    # Required for an action whose command isn't in its sources, or whose sources are flagged
    confirm_unverified: bool = False


async def _load(session: SessionDep, principal: Principal, action_id: uuid.UUID) -> Any:
    action = await session.scalar(
        select(InvestigationAction)
        .where(
            InvestigationAction.id == action_id,
            InvestigationAction.workspace_id == principal.workspace.id,
        )
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise ApiError(404, "not_found", "Action not found")
    return action


async def _incident_of(session: SessionDep, investigation_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Incident.id).where(Incident.investigation_id == investigation_id)
    )


async def _record(
    session: SessionDep, action: InvestigationAction, type_: str, **data: Any
) -> None:
    incident_id = await _incident_of(session, action.investigation_id)
    if incident_id is not None:
        session.add(
            IncidentEvent(
                incident_id=incident_id,
                type=type_,
                data={"action_id": str(action.id), "title": action.title, **data},
            )
        )


async def _decide(
    session: SessionDep,
    principal: Principal,
    action_id: uuid.UUID,
    to: ActionStatus,
    from_: ActionStatus,
    note: str | None,
) -> InvestigationAction:
    """One conditional UPDATE: two people approving at once can't both send it."""
    await _load(session, principal, action_id)
    changed = await session.scalar(
        update(InvestigationAction)
        .where(InvestigationAction.id == action_id, InvestigationAction.status == from_.value)
        .values(
            status=to.value,
            decided_by_key_id=principal.api_key.id,
            decided_by=principal.api_key.name,
            decided_at=datetime.now(UTC),
            decision_note=note,
        )
        .returning(InvestigationAction.id)
    )
    if changed is None:
        current = await _load(session, principal, action_id)
        raise ApiError(409, "invalid_state", f"This action is already {current.status}")
    return await _load(session, principal, action_id)  # type: ignore[no-any-return]


async def _dispatch(
    request: Request, session: SessionDep, settings: SettingsDep, action: InvestigationAction
) -> None:
    """Sends an approved action to the workspace's actions webhook, if it has one."""
    try:
        webhook = await load_webhook(session, action.workspace_id, settings)
    except WebhookUnavailable as exc:
        result: dict[str, Any] | None = {"ok": False, "error": str(exc)}
    else:
        if webhook is None:
            return  # stays "approved": carried out by hand
        body = payload(action, await _incident_of(session, action.investigation_id))
        result = await send(request.app.state.telemetry_http, webhook, body, settings)
    ok = bool(result and result["ok"])
    action.status = (ActionStatus.EXECUTED if ok else ActionStatus.FAILED).value
    action.result = result
    await _record(session, action, "action_executed" if ok else "action_failed")


@router.get("/investigations/{investigation_id}/actions")
async def list_investigation_actions(
    investigation_id: uuid.UUID, principal: Viewer, session: SessionDep
) -> list[ActionOut]:
    owner = await session.scalar(
        select(Investigation.id).where(
            Investigation.id == investigation_id,
            Investigation.workspace_id == principal.workspace.id,
        )
    )
    if owner is None:
        raise ApiError(404, "not_found", "Investigation not found")
    rows = await session.scalars(
        select(InvestigationAction)
        .where(InvestigationAction.investigation_id == investigation_id)
        .order_by(InvestigationAction.position)
    )
    return [ActionOut.model_validate(r) for r in rows]


@router.get("/actions")
async def list_actions(
    principal: Viewer,
    session: SessionDep,
    status_filter: Annotated[ActionStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ActionOut]:
    """The workspace's actions, newest first; ?status=proposed is the approval queue."""
    query = select(InvestigationAction).where(
        InvestigationAction.workspace_id == principal.workspace.id
    )
    if status_filter is not None:
        query = query.where(InvestigationAction.status == status_filter.value)
    rows = await session.scalars(
        query.order_by(InvestigationAction.created_at.desc(), InvestigationAction.position).limit(
            limit
        )
    )
    return [ActionOut.model_validate(r) for r in rows]


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: uuid.UUID,
    request: Request,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    body: Approval | None = None,
) -> ActionOut:
    """Approves a proposed action, and sends it to the actions webhook if there is one."""
    note = body.note if body else None
    current = await _load(session, principal, action_id)
    if (not current.verified or current.flags) and not (body and body.confirm_unverified):
        reasons = []
        if not current.verified:
            reasons.append("its command appears in none of the sources it cites")
        if current.flags:
            reasons.append(f"its sources look like prompt injection ({', '.join(current.flags)})")
        raise ApiError(
            409,
            "confirmation_required",
            f"Check this action yourself: {'; '.join(reasons)}. To approve it anyway, send "
            '"confirm_unverified": true.',
        )
    action = await _decide(
        session, principal, action_id, ActionStatus.APPROVED, ActionStatus.PROPOSED, note
    )
    await _record(session, action, "action_approved", by=principal.api_key.name)
    # Committed before sending: if this process dies mid-request, the action is visibly
    # approved (and can be retried), never sent without a record of who approved it
    await session.commit()
    await _dispatch(request, session, settings, action)
    await session.commit()
    return ActionOut.model_validate(await _load(session, principal, action_id))


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: uuid.UUID,
    principal: Responder,
    session: SessionDep,
    body: Decision | None = None,
) -> ActionOut:
    note = body.note if body else None
    action = await _decide(
        session, principal, action_id, ActionStatus.REJECTED, ActionStatus.PROPOSED, note
    )
    await _record(session, action, "action_rejected", by=principal.api_key.name)
    await session.commit()
    return ActionOut.model_validate(action)


@router.post("/actions/{action_id}/retry")
async def retry_action(
    action_id: uuid.UUID,
    request: Request,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
) -> ActionOut:
    """Sends an approved action whose webhook call failed again."""
    action = await _load(session, principal, action_id)
    moved = await session.scalar(
        update(InvestigationAction)
        .where(
            InvestigationAction.id == action_id,
            InvestigationAction.status == ActionStatus.FAILED.value,
        )
        .values(status=ActionStatus.APPROVED.value)
        .returning(InvestigationAction.id)
    )
    if moved is None:
        raise ApiError(
            409, "invalid_state", f"Only failed actions can be retried ({action.status})"
        )
    await session.commit()
    action = await _load(session, principal, action_id)
    await _dispatch(request, session, settings, action)
    await session.commit()
    return ActionOut.model_validate(await _load(session, principal, action_id))
