from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from opspilot.models import ApiKey
from tests.conftest import auth


@pytest.mark.parametrize(
    "header",
    [
        None,
        "ops_abcdefghijklmnopqrstuvwxyz",  # no scheme
        "Basic ops_abcdefghijklmnopqrstuvwxyz",
        "Bearer ",
        "Bearer not-a-key",
        "Bearer ops_abcdefghijklmnopqrstuvwxyz0123456789",  # well formed but unknown
    ],
)
async def test_rejects_missing_or_bad_keys(client: AsyncClient, header: str | None) -> None:
    headers = {"Authorization": header} if header is not None else {}

    res = await client.get("/v1/me", headers=headers)

    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"] == "Bearer"
    assert res.json()["error"]["code"] == "unauthorized"


async def test_revoked_and_expired_keys_look_like_unknown_ones(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()
    revoked = (
        await client.post("/v1/api-keys", json={"name": "r", "role": "viewer"}, headers=auth(admin))
    ).json()
    expired = (
        await client.post("/v1/api-keys", json={"name": "e", "role": "viewer"}, headers=auth(admin))
    ).json()
    await client.delete(f"/v1/api-keys/{revoked['id']}", headers=auth(admin))
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == expired["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    unknown = await client.get("/v1/me", headers=auth("ops_" + "x" * 43))
    for key in (revoked["key"], expired["key"]):
        res = await client.get("/v1/me", headers=auth(key))
        assert res.status_code == 401
        assert res.json() == unknown.json()


@pytest.mark.parametrize(
    ("role", "can_read", "can_manage_keys"),
    [("viewer", True, False), ("responder", True, False), ("admin", True, True)],
)
async def test_roles(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    role: str,
    can_read: bool,
    can_manage_keys: bool,
) -> None:
    _, admin = await create_workspace()
    created = await client.post(
        "/v1/api-keys", json={"name": role, "role": role}, headers=auth(admin)
    )
    key = created.json()["key"]

    me = await client.get("/v1/me", headers=auth(key))
    keys = await client.get("/v1/api-keys", headers=auth(key))

    assert (me.status_code == 200) is can_read
    assert (keys.status_code == 200) is can_manage_keys
    if not can_manage_keys:
        assert keys.status_code == 403
        assert keys.json()["error"]["message"] == "This needs the admin role or higher"


async def test_last_used_is_recorded_at_most_once_a_minute(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, key = await create_workspace()

    async def last_used() -> datetime | None:
        async with app.state.sessionmaker() as session:
            return await session.scalar(select(ApiKey.last_used_at))

    assert await last_used() is None
    await client.get("/v1/me", headers=auth(key))
    first = await last_used()
    assert first is not None

    await client.get("/v1/me", headers=auth(key))
    assert await last_used() == first  # within the minute: no write

    async with app.state.sessionmaker() as session:
        await session.execute(update(ApiKey).values(last_used_at=first - timedelta(minutes=2)))
        await session.commit()
    await client.get("/v1/me", headers=auth(key))
    latest = await last_used()
    assert latest is not None
    assert latest > first
