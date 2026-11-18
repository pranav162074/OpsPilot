import asyncio

from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.health import run_checks

# Nothing listens on port 1, so connections there fail immediately
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_POSTGRES = "postgresql+asyncpg://opspilot:opspilot@127.0.0.1:1/opspilot"


async def test_live_needs_no_dependencies(client: AsyncClient) -> None:
    res = await client.get("/health/live")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_ready_when_everything_is_up(client: AsyncClient) -> None:
    res = await client.get("/health/ready")
    body = res.json()

    assert res.status_code == 200
    assert body["status"] == "ok"
    assert body["checks"]["postgres"]["status"] == "up"
    assert body["checks"]["redis"]["status"] == "up"
    assert body["checks"]["postgres"]["latency_ms"] >= 0


async def test_not_ready_when_redis_is_down(app: FastAPI, client: AsyncClient) -> None:
    await app.state.redis.aclose()
    app.state.redis = Redis.from_url(UNREACHABLE_REDIS, socket_connect_timeout=1)

    res = await client.get("/health/ready")
    body = res.json()

    assert res.status_code == 503
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"]["status"] == "down"
    assert body["checks"]["redis"]["error"]
    assert body["checks"]["postgres"]["status"] == "up"


async def test_not_ready_when_postgres_is_down(app: FastAPI, client: AsyncClient) -> None:
    await app.state.engine.dispose()
    app.state.engine = create_async_engine(UNREACHABLE_POSTGRES)

    res = await client.get("/health/ready")
    body = res.json()

    assert res.status_code == 503
    assert body["checks"]["postgres"]["status"] == "down"
    assert body["checks"]["redis"]["status"] == "up"


async def test_errors_never_expose_connection_details(app: FastAPI, client: AsyncClient) -> None:
    await app.state.engine.dispose()
    app.state.engine = create_async_engine(UNREACHABLE_POSTGRES)

    res = await client.get("/health/ready")

    assert "opspilot:opspilot" not in res.text
    assert "127.0.0.1" not in res.text


async def test_a_hanging_check_times_out() -> None:
    async def hangs() -> None:
        await asyncio.sleep(10)

    async def fine() -> None:
        return None

    started = asyncio.get_running_loop().time()
    results = await run_checks({"slow": hangs, "fast": fine}, limit_seconds=0.2)
    elapsed = asyncio.get_running_loop().time() - started

    assert results["slow"].status == "down"
    assert results["slow"].error == "TimeoutError"
    assert results["fast"].status == "up"
    assert elapsed < 1  # checks run in parallel, and the slow one is cut off
