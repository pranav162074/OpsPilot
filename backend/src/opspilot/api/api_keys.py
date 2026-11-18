import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response, status
from sqlalchemy import func, or_, select, update

from opspilot.api.deps import Admin, SessionDep
from opspilot.api.errors import ApiError
from opspilot.api.keys import new_api_key
from opspilot.api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from opspilot.models import ApiKey, Role, Workspace

router = APIRouter(prefix="/v1/api-keys", tags=["api keys"])


def _not_found() -> ApiError:
    # Another workspace's key and a key that doesn't exist look exactly the same
    return ApiError(404, "not_found", "API key not found")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate, principal: Admin, session: SessionDep
) -> ApiKeyCreated:
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    row, key = new_api_key(principal.workspace.id, body.name, body.role, expires_at)
    session.add(row)
    await session.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), key=key)


@router.get("")
async def list_api_keys(principal: Admin, session: SessionDep) -> list[ApiKeyOut]:
    rows = await session.scalars(
        select(ApiKey)
        .where(ApiKey.workspace_id == principal.workspace.id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
    )
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: uuid.UUID, principal: Admin, session: SessionDep) -> Response:
    workspace_id = principal.workspace.id

    # Revocations in one workspace run one at a time. Without this, two admins revoking each
    # other's keys at the same moment could both pass the check below and leave no admin.
    await session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
    )

    key = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.workspace_id == workspace_id)
    )
    if key is None or key.revoked_at is not None:
        raise _not_found()

    now = datetime.now(UTC)
    if key.role == Role.ADMIN.value:
        other_admins = await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.workspace_id == workspace_id,
                ApiKey.role == Role.ADMIN.value,
                ApiKey.id != key.id,
                ApiKey.revoked_at.is_(None),
                or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
            )
        )
        if not other_admins:
            raise ApiError(
                409, "last_admin_key", "Create another admin key before revoking the last one"
            )

    await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(revoked_at=now))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
