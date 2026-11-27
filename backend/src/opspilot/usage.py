"""What the workspace's model calls cost, and the monthly cap on it."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.api.errors import ApiError
from opspilot.models import Investigation, Workspace


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_to_date_usd(
    session: AsyncSession, workspace_id: uuid.UUID, now: datetime
) -> Decimal:
    spent = await session.scalar(
        select(func.coalesce(func.sum(Investigation.cost_usd), 0)).where(
            Investigation.workspace_id == workspace_id,
            Investigation.created_at >= month_start(now),
        )
    )
    return Decimal(spent or 0)


async def over_budget(
    session: AsyncSession, workspace: Workspace, now: datetime | None = None
) -> Decimal | None:
    """This month's spend if it has reached the workspace's cap, else None."""
    if workspace.monthly_budget_usd is None:
        return None
    spent = await month_to_date_usd(session, workspace.id, now or datetime.now(UTC))
    return spent if spent >= workspace.monthly_budget_usd else None


async def ensure_within_budget(
    session: AsyncSession, workspace: Workspace, now: datetime | None = None
) -> None:
    """Refuses new model work once this month's spend has reached the workspace's cap. A run
    already going is never cut off, so a month can end slightly over; the cap stops the next."""
    spent = await over_budget(session, workspace, now)
    if spent is not None:
        raise ApiError(
            402,
            "budget_exceeded",
            f"This month's model budget (${workspace.monthly_budget_usd}) is used up "
            f"(${spent:.2f} spent). An admin can raise it with PUT /v1/usage/budget.",
        )
