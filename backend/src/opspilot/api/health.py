from functools import partial
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from opspilot.api.deps import SettingsDep
from opspilot.health import CheckResult, check_postgres, check_redis, run_checks

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, CheckResult]


@router.get("/live")
async def live() -> LiveResponse:
    """The process is running. Restart it if this fails. It checks nothing else on purpose,
    so a database outage doesn't make every instance restart at once."""
    return LiveResponse(status="ok")


@router.get("/ready", responses={503: {"model": ReadyResponse}})
async def ready(request: Request, response: Response, settings: SettingsDep) -> ReadyResponse:
    """Every dependency answers. A load balancer sends traffic only while this returns 200."""
    state = request.app.state
    checks = await run_checks(
        {
            "postgres": partial(check_postgres, state.engine),
            "redis": partial(check_redis, state.redis),
        },
        limit_seconds=settings.health_check_timeout_seconds,
    )
    all_up = all(check.status == "up" for check in checks.values())
    if not all_up:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ok" if all_up else "unavailable", checks=checks)
