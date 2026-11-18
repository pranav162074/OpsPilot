from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from opspilot.ratelimit import RateLimiter
from tests.conftest import ADMIN_TOKEN, auth


@pytest.fixture
async def strict_client(
    make_app: Callable[..., FastAPI], client: AsyncClient
) -> AsyncIterator[AsyncClient]:
    """An app with tiny limits. Depends on `client` so the database is cleaned first."""
    application = make_app(api_rate_limit_per_minute=3, workspace_create_limit_per_hour=2)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _new_key(http: AsyncClient) -> str:
    res = await http.post(
        "/v1/workspaces", json={"name": "Acme"}, headers={"X-Admin-Token": ADMIN_TOKEN}
    )
    key: str = res.json()["api_key"]["key"]
    return key


async def test_limits_each_key(strict_client: AsyncClient) -> None:
    key = await _new_key(strict_client)

    codes = [(await strict_client.get("/v1/me", headers=auth(key))) for _ in range(4)]

    assert [r.status_code for r in codes] == [200, 200, 200, 429]
    assert [r.headers["X-RateLimit-Remaining"] for r in codes] == ["2", "1", "0", "0"]
    blocked = codes[-1]
    assert blocked.headers["X-RateLimit-Limit"] == "3"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert blocked.json()["error"]["code"] == "rate_limited"


async def test_keys_have_separate_limits(strict_client: AsyncClient) -> None:
    first = await _new_key(strict_client)
    second = await _new_key(strict_client)
    for _ in range(3):
        await strict_client.get("/v1/me", headers=auth(first))

    assert (await strict_client.get("/v1/me", headers=auth(first))).status_code == 429
    assert (await strict_client.get("/v1/me", headers=auth(second))).status_code == 200


async def test_limits_workspace_creation_even_with_a_wrong_token(
    strict_client: AsyncClient,
) -> None:
    async def attempt() -> int:
        res = await strict_client.post(
            "/v1/workspaces", json={"name": "x"}, headers={"X-Admin-Token": "guess"}
        )
        return res.status_code

    assert [await attempt() for _ in range(3)] == [403, 403, 429]


async def test_fails_open_when_redis_is_down(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, key = await create_workspace()
    dead = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    app.state.rate_limiter = RateLimiter(dead)
    try:
        res = await client.get("/v1/me", headers=auth(key))
    finally:
        await dead.aclose()

    assert res.status_code == 200
