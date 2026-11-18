from httpx import AsyncClient


async def test_generates_an_id_when_none_is_sent(client: AsyncClient) -> None:
    res = await client.get("/health/live")
    request_id = res.headers["X-Request-ID"]
    assert len(request_id) == 32


async def test_keeps_a_safe_id_from_the_caller(client: AsyncClient) -> None:
    res = await client.get("/health/live", headers={"X-Request-ID": "trace-abc_123.4"})
    assert res.headers["X-Request-ID"] == "trace-abc_123.4"


async def test_replaces_ids_that_could_inject_into_logs(client: AsyncClient) -> None:
    for bad in ['abc"} {"level":"ERROR', "a" * 65, "has space", ""]:
        res = await client.get("/health/live", headers={"X-Request-ID": bad})
        assert res.headers["X-Request-ID"] != bad
        assert len(res.headers["X-Request-ID"]) == 32
