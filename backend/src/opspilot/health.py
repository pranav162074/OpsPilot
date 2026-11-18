import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class CheckResult(BaseModel):
    status: Literal["up", "down"]
    latency_ms: float
    # Only the error type, never its message: messages can contain hosts or credentials
    error: str | None = None


Check = Callable[[], Awaitable[None]]


async def check_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        # Retrieval depends on pgvector, so the app isn't ready until the migration added it
        version = await conn.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        if version is None:
            raise RuntimeError("pgvector extension is not installed")


async def check_redis(redis: Redis) -> None:
    if not await redis.ping():
        raise RuntimeError("Redis did not answer PING")


async def _timed(check: Check, limit_seconds: float) -> CheckResult:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(limit_seconds):
            await check()
    except Exception as exc:  # any failure means "down", whatever the cause
        return CheckResult(
            status="down",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=type(exc).__name__,
        )
    return CheckResult(status="up", latency_ms=round((time.perf_counter() - started) * 1000, 1))


async def run_checks(checks: dict[str, Check], limit_seconds: float) -> dict[str, CheckResult]:
    # All checks run at the same time, so one slow dependency can't add up with another
    names = list(checks)
    results = await asyncio.gather(*(_timed(checks[name], limit_seconds) for name in names))
    return dict(zip(names, results, strict=True))
