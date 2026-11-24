import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.api.errors import ApiError
from opspilot.config import Settings
from opspilot.models import ApiKey, Role, Workspace
from opspilot.ratelimit import RateLimiter, RateLimitResult
from opspilot.security.api_keys import hash_api_key, looks_like_api_key

# Writing last_used_at on every request would turn every read into a write. Once a minute
# is accurate enough to spot unused keys.
LAST_USED_RESOLUTION = timedelta(minutes=1)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def _unauthorized(message: str) -> ApiError:
    # The same header tells clients how to authenticate, as the HTTP spec asks
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


def rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_seconds),
    }


def enforce(result: RateLimitResult, response: Response) -> None:
    if not result.allowed:
        headers = rate_limit_headers(result) | {"Retry-After": str(result.reset_seconds)}
        raise ApiError(429, "rate_limited", "Too many requests, slow down", headers=headers)
    response.headers.update(rate_limit_headers(result))


@dataclass(frozen=True)
class Principal:
    """Who is calling: the key and the workspace it belongs to."""

    api_key: ApiKey
    workspace: Workspace

    @property
    def role(self) -> Role:
        return Role(self.api_key.role)


async def find_active_key(
    session: AsyncSession, token: str, now: datetime
) -> tuple[ApiKey, Workspace] | None:
    """The key and its workspace, if the token is a valid, unrevoked, unexpired API key."""
    if not looks_like_api_key(token):
        return None
    row = await session.execute(
        select(ApiKey, Workspace)
        .join(Workspace, Workspace.id == ApiKey.workspace_id)
        .where(ApiKey.key_hash == hash_api_key(token))
    )
    found = row.one_or_none()
    if found is None or not found.ApiKey.is_active(now):
        return None
    return found.ApiKey, found.Workspace


async def authenticate(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> Principal:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("Send an API key as: Authorization: Bearer <key>")

    # Malformed tokens never reach the database
    if not looks_like_api_key(token):
        raise _unauthorized("The API key is not valid")

    now = datetime.now(UTC)
    found = await find_active_key(session, token, now)
    # Unknown, revoked and expired keys all get the same answer, so a caller learns nothing
    if found is None:
        raise _unauthorized("The API key is not valid")

    api_key, workspace = found

    # Limit per key, so one noisy client can't slow down the rest of its workspace
    result = await limiter.hit(f"key:{api_key.id}", settings.api_rate_limit_per_minute, 60)
    enforce(result, response)

    if api_key.last_used_at is None or now - api_key.last_used_at > LAST_USED_RESOLUTION:
        await session.execute(
            update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
        )
        await session.commit()
        api_key.last_used_at = now

    return Principal(api_key=api_key, workspace=workspace)


def require_role(required: Role) -> Callable[..., Awaitable[Principal]]:
    async def check(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.role.allows(required):
            raise ApiError(403, "forbidden", f"This needs the {required.value} role or higher")
        return principal

    return check


def check_admin_token(request: Request, settings: Settings) -> None:
    expected = settings.admin_token
    if expected is None:
        raise ApiError(403, "forbidden", "Workspace creation is disabled on this server")
    given = request.headers.get("X-Admin-Token", "")
    # compare_digest takes the same time however many characters match, so the token
    # can't be worked out from response times
    if not hmac.compare_digest(given.encode(), expected.get_secret_value().encode()):
        raise ApiError(403, "forbidden", "The admin token is not valid")


Viewer = Annotated[Principal, Depends(require_role(Role.VIEWER))]
Responder = Annotated[Principal, Depends(require_role(Role.RESPONDER))]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]
