from collections.abc import Callable
from typing import Any

from httpx import AsyncClient

from tests.conftest import ADMIN_TOKEN, auth


async def test_unknown_routes_use_the_error_shape(client: AsyncClient) -> None:
    res = await client.get("/v1/nothing-here")

    assert res.status_code == 404
    assert res.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_wrong_method_uses_the_error_shape(client: AsyncClient) -> None:
    res = await client.put("/v1/me")

    assert res.status_code == 405
    assert res.json()["error"]["code"] == "method_not_allowed"


async def test_validation_errors_name_the_field(client: AsyncClient) -> None:
    res = await client.post("/v1/workspaces", json={}, headers={"X-Admin-Token": ADMIN_TOKEN})

    assert res.status_code == 422
    error = res.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "name"


async def test_malformed_ids_are_validation_errors(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()

    res = await client.delete("/v1/api-keys/not-a-uuid", headers=auth(admin))

    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == "key_id"
