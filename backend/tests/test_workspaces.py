from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import ADMIN_TOKEN, auth


async def test_creates_a_workspace_with_an_admin_key(client: AsyncClient) -> None:
    res = await client.post(
        "/v1/workspaces", json={"name": "  Acme  "}, headers={"X-Admin-Token": ADMIN_TOKEN}
    )

    assert res.status_code == 201
    body = res.json()
    assert body["workspace"]["name"] == "Acme"
    key = body["api_key"]
    assert key["role"] == "admin"
    assert key["key"].startswith("ops_")
    assert key["key"].startswith(key["prefix"])


async def test_the_key_works_straight_away(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    workspace, key = await create_workspace("Acme")

    res = await client.get("/v1/me", headers=auth(key))

    assert res.status_code == 200
    assert res.json()["workspace"]["id"] == workspace["id"]
    assert res.json()["api_key"]["role"] == "admin"
    assert "key" not in res.json()["api_key"]  # the secret is only ever shown once


async def test_needs_the_admin_token(client: AsyncClient) -> None:
    missing = await client.post("/v1/workspaces", json={"name": "Acme"})
    wrong = await client.post(
        "/v1/workspaces", json={"name": "Acme"}, headers={"X-Admin-Token": "nope"}
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert wrong.json()["error"]["message"] == "The admin token is not valid"


@pytest.mark.parametrize("token", [None, "", "   "])
async def test_is_switched_off_without_an_admin_token(
    make_app: Callable[..., FastAPI], token: str | None
) -> None:
    application = make_app(admin_token=token)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/v1/workspaces", json={"name": "Acme"}, headers={"X-Admin-Token": ""}
            )

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "Workspace creation is disabled on this server"


async def test_rejects_bad_names(client: AsyncClient) -> None:
    headers = {"X-Admin-Token": ADMIN_TOKEN}

    blank = await client.post("/v1/workspaces", json={"name": "   "}, headers=headers)
    too_long = await client.post("/v1/workspaces", json={"name": "x" * 101}, headers=headers)
    extra = await client.post(
        "/v1/workspaces", json={"name": "Acme", "id": "mine"}, headers=headers
    )

    assert blank.status_code == too_long.status_code == extra.status_code == 422
