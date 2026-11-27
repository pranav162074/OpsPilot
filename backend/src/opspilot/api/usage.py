"""Cost tracking: tokens and dollars per day, and the workspace's monthly budget."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Date, cast, func, select

from opspilot.api.deps import Admin, SessionDep, SettingsDep, Viewer
from opspilot.models import Investigation
from opspilot.usage import month_to_date_usd

router = APIRouter(prefix="/v1/usage", tags=["usage"])


class Day(BaseModel):
    date: date
    investigations: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class Pricing(BaseModel):
    model: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float


class UsageOut(BaseModel):
    days: list[Day]
    totals: Day
    month_to_date_usd: Decimal
    monthly_budget_usd: Decimal | None
    # Investigations whose sources tripped the prompt-injection checks, in the period
    flagged_investigations: int
    pricing: Pricing


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # null removes the cap
    monthly_budget_usd: Annotated[Decimal | None, Field(ge=0, max_digits=10, decimal_places=2)]


@router.get("")
async def get_usage(
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> UsageOut:
    """Per day (UTC), newest first: investigations, model calls, tokens and cost."""
    now = datetime.now(UTC)
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day = cast(func.timezone("UTC", Investigation.created_at), Date).label("day")
    rows = (
        await session.execute(
            select(
                day,
                func.count(),
                func.coalesce(func.sum(Investigation.llm_calls), 0),
                func.coalesce(func.sum(Investigation.input_tokens), 0),
                func.coalesce(func.sum(Investigation.output_tokens), 0),
                func.coalesce(func.sum(Investigation.cost_usd), 0),
                func.count().filter(Investigation.flagged_sources > 0),
            )
            .where(
                Investigation.workspace_id == principal.workspace.id,
                Investigation.created_at >= since,
            )
            .group_by(day)
            .order_by(day.desc())
        )
    ).all()
    per_day = [
        Day(
            date=r[0],
            investigations=r[1],
            llm_calls=r[2],
            input_tokens=r[3],
            output_tokens=r[4],
            cost_usd=Decimal(r[5] or 0),
        )
        for r in rows
    ]
    totals = Day(
        date=now.date(),
        investigations=sum(d.investigations for d in per_day),
        llm_calls=sum(d.llm_calls for d in per_day),
        input_tokens=sum(d.input_tokens for d in per_day),
        output_tokens=sum(d.output_tokens for d in per_day),
        cost_usd=sum((d.cost_usd for d in per_day), Decimal(0)),
    )
    return UsageOut(
        days=per_day,
        totals=totals,
        month_to_date_usd=await month_to_date_usd(session, principal.workspace.id, now),
        monthly_budget_usd=principal.workspace.monthly_budget_usd,
        flagged_investigations=sum(r[6] for r in rows),
        pricing=Pricing(
            model=settings.llm_model,
            input_usd_per_mtok=settings.llm_input_usd_per_mtok,
            output_usd_per_mtok=settings.llm_output_usd_per_mtok,
        ),
    )


@router.put("/budget")
async def set_budget(body: BudgetIn, principal: Admin, session: SessionDep) -> BudgetIn:
    """Caps the workspace's monthly model spend. Past it, new investigations (and postmortem
    drafts) get 402 until the month ends or the cap is raised."""
    workspace = principal.workspace
    workspace.monthly_budget_usd = body.monthly_budget_usd
    session.add(workspace)
    await session.commit()
    return BudgetIn(monthly_budget_usd=workspace.monthly_budget_usd)
