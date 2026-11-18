from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from opspilot.security.api_keys import hash_api_key
from tests.conftest import auth


async def test_creates_a_key_that_is_shown_once(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()

    res = await client.post(
        "/v1/api-keys",
        json={"name": "CI bot", "role": "responder", "expires_in_days": 30},
        headers=auth(admin),
    )

    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "responder"
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)

    listed = (await client.get("/v1/api-keys", headers=auth(admin))).json()
    assert [k["name"] for k in listed] == ["CI bot", "Initial admin key"]
    assert all("key" not in k for k in listed)


async def test_stores_only_a_hash(app: FastAPI, create_workspace: Callable[..., Any]) -> None:
    _, admin = await create_workspace()

    async with app.state.sessionmaker() as session:
        stored = (await session.execute(text("SELECT key_hash, prefix FROM api_keys"))).one()

    assert stored.key_hash == hash_api_key(admin)
    assert admin not in stored
    assert stored.prefix == admin[:12]


async def test_rejects_unknown_roles_and_bad_expiry(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()

    bad_role = await client.post(
        "/v1/api-keys", json={"name": "x", "role": "owner"}, headers=auth(admin)
    )
    bad_expiry = await client.post(
        "/v1/api-keys",
        json={"name": "x", "role": "viewer", "expires_in_days": 0},
        headers=auth(admin),
    )

    assert bad_role.status_code == bad_expiry.status_code == 422


async def test_revoking_a_key_stops_it_working(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()
    viewer = (
        await client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=auth(admin))
    ).json()

    first = await client.delete(f"/v1/api-keys/{viewer['id']}", headers=auth(admin))
    again = await client.delete(f"/v1/api-keys/{viewer['id']}", headers=auth(admin))

    assert first.status_code == 204
    assert again.status_code == 404
    assert (await client.get("/v1/me", headers=auth(viewer["key"]))).status_code == 401
    listed = (await client.get("/v1/api-keys", headers=auth(admin))).json()
    assert next(k for k in listed if k["id"] == viewer["id"])["revoked_at"] is not None


async def test_cannot_revoke_the_last_admin_key(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()
    me = (await client.get("/v1/me", headers=auth(admin))).json()

    res = await client.delete(f"/v1/api-keys/{me['api_key']['id']}", headers=auth(admin))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "last_admin_key"


async def test_can_revoke_an_admin_key_when_another_exists(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()
    second = (
        await client.post("/v1/api-keys", json={"name": "a2", "role": "admin"}, headers=auth(admin))
    ).json()
    me = (await client.get("/v1/me", headers=auth(admin))).json()

    res = await client.delete(f"/v1/api-keys/{me['api_key']['id']}", headers=auth(second["key"]))

    assert res.status_code == 204
    assert (await client.get("/v1/me", headers=auth(admin))).status_code == 401


async def test_workspaces_cannot_see_each_others_keys(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, acme = await create_workspace("Acme")
    _, globex = await create_workspace("Globex")
    acme_viewer = (
        await client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=auth(acme))
    ).json()

    listed = (await client.get("/v1/api-keys", headers=auth(globex))).json()
    revoke = await client.delete(f"/v1/api-keys/{acme_viewer['id']}", headers=auth(globex))

    assert [k["name"] for k in listed] == ["Initial admin key"]
    assert revoke.status_code == 404  # the same answer as a key that doesn't exist
    assert (await client.get("/v1/me", headers=auth(acme_viewer["key"]))).status_code == 200
