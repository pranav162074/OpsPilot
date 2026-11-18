from fastapi import APIRouter, Request, Response, status

from opspilot.api.deps import (
    RateLimiterDep,
    SessionDep,
    SettingsDep,
    Viewer,
    check_admin_token,
    enforce,
)
from opspilot.api.keys import new_api_key
from opspilot.api.schemas import (
    ApiKeyCreated,
    ApiKeyOut,
    Me,
    WorkspaceCreate,
    WorkspaceCreated,
    WorkspaceOut,
)
from opspilot.models import Role, Workspace

router = APIRouter(prefix="/v1", tags=["workspaces"])


@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> WorkspaceCreated:
    """Creates a workspace and its first admin key. Needs the server's admin token."""
    # Rate limit before checking the token, so it can't be guessed at speed
    client_ip = request.client.host if request.client else "unknown"
    result = await limiter.hit(
        f"create-workspace:{client_ip}", settings.workspace_create_limit_per_hour, 3600
    )
    enforce(result, response)
    check_admin_token(request, settings)

    workspace = Workspace(name=body.name)
    session.add(workspace)
    await session.flush()  # assigns workspace.id

    key_row, key = new_api_key(workspace.id, "Initial admin key", Role.ADMIN)
    session.add(key_row)
    await session.commit()

    return WorkspaceCreated(
        workspace=WorkspaceOut.model_validate(workspace),
        api_key=ApiKeyCreated(**ApiKeyOut.model_validate(key_row).model_dump(), key=key),
    )


@router.get("/me")
async def me(principal: Viewer) -> Me:
    """The calling key and its workspace. Useful to check a key works."""
    return Me(
        workspace=WorkspaceOut.model_validate(principal.workspace),
        api_key=ApiKeyOut.model_validate(principal.api_key),
    )
