import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from opspilot.ingestion.service import Outcome
from tests.conftest import ADMIN_TOKEN, auth, upload

RUNBOOK = """# Payments API

## High error rate

1. Check the dashboard.
2. Roll back the last deploy.

## Database failover

Promote the replica and update the connection string.
"""


async def key_with_role(client: AsyncClient, admin: str, role: str) -> str:
    res = await client.post("/v1/api-keys", json={"name": role, "role": role}, headers=auth(admin))
    key: str = res.json()["key"]
    return key


async def test_upload_stores_the_document_and_queues_it(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> None:
    _, key = await create_workspace()

    res = await upload(client, key, RUNBOOK, "payments-runbook.md", source_type="runbook")

    assert res.status_code == 202
    doc = res.json()
    assert doc["title"] == "payments-runbook"  # from the file name when no title is given
    assert doc["source_type"] == "runbook"
    assert doc["media_type"] == "text/markdown"
    assert doc["status"] == "pending"
    assert doc["size_bytes"] == len(RUNBOOK.encode())
    assert enqueued == [uuid.UUID(doc["id"])]


async def test_ingestion_makes_the_document_ready_with_chunks(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK, title="Payments")).json()

    assert await ingest(doc["id"]) == Outcome.READY

    fetched = (await client.get(f"/v1/documents/{doc['id']}", headers=auth(key))).json()
    chunks = (await client.get(f"/v1/documents/{doc['id']}/chunks", headers=auth(key))).json()
    assert fetched["status"] == "ready"
    assert fetched["title"] == "Payments"
    assert fetched["processed_at"] is not None
    assert fetched["chunk_count"] == len(chunks) == 2
    assert [c["heading"] for c in chunks] == [
        "Payments API > High error rate",
        "Payments API > Database failover",
    ]
    for c in chunks:
        assert RUNBOOK[c["char_start"] : c["char_end"]] == c["content"]


async def test_viewers_can_read_but_not_upload(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> None:
    _, admin = await create_workspace()
    viewer = await key_with_role(client, admin, "viewer")
    responder = await key_with_role(client, admin, "responder")

    denied = await upload(client, viewer, RUNBOOK)
    allowed = await upload(client, responder, RUNBOOK)
    listed = await client.get("/v1/documents", headers=auth(viewer))

    assert denied.status_code == 403
    assert allowed.status_code == 202
    assert [d["id"] for d in listed.json()["items"]] == [allowed.json()["id"]]


async def test_rejects_files_it_cannot_index(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> None:
    _, key = await create_workspace()

    pdf = await upload(client, key, b"%PDF-1.7", "postmortem.pdf")
    not_utf8 = await upload(client, key, "caf\xe9".encode("latin-1"), "notes.txt")
    binary = await upload(client, key, b"abc\x00def", "notes.txt")
    empty = await upload(client, key, "  \n\n ", "notes.md")

    assert pdf.status_code == 415
    assert pdf.json()["error"]["code"] == "unsupported_file_type"
    assert not_utf8.json()["error"]["code"] == binary.json()["error"]["code"] == "invalid_encoding"
    assert empty.json()["error"]["code"] == "empty_document"
    assert enqueued == []


async def test_rejects_files_over_the_size_limit(
    make_app: Callable[..., FastAPI], client: AsyncClient
) -> None:
    # `client` is requested only so the database is cleaned first
    application = make_app(max_document_bytes=100)
    async with application.router.lifespan_context(application):
        application.state.enqueue_ingestion = lambda _: None
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            created = await http.post(
                "/v1/workspaces", json={"name": "Acme"}, headers={"X-Admin-Token": ADMIN_TOKEN}
            )
            key = created.json()["api_key"]["key"]
            at_limit = await upload(http, key, "a" * 100, "a.txt")
            over = await upload(http, key, "b" * 101, "b.txt")
            way_over = await upload(http, key, "c" * 50_000, "c.txt")

    assert at_limit.status_code == 202
    assert over.status_code == way_over.status_code == 413


async def test_duplicates_are_detected_per_workspace(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> None:
    _, acme = await create_workspace("Acme")
    _, globex = await create_workspace("Globex")
    first = (await upload(client, acme, "Line one\nLine two\n", "a.txt")).json()

    # Same text with Windows line endings and a byte-order mark is still the same document
    again = await upload(client, acme, "﻿Line one\r\nLine two\r\n".encode(), "b.txt")
    elsewhere = await upload(client, globex, "Line one\nLine two\n", "a.txt")

    assert again.status_code == 409
    assert again.json()["error"] == {
        "code": "duplicate_document",
        "message": "This workspace already has a document with the same content",
        "details": {"document_id": first["id"]},
    }
    assert elsewhere.status_code == 202


async def test_upload_still_succeeds_when_the_queue_is_down(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    def broken(_: uuid.UUID) -> None:
        raise ConnectionError("Redis is down")

    app.state.enqueue_ingestion = broken
    _, key = await create_workspace()

    res = await upload(client, key, RUNBOOK)

    # Saved as pending; the requeue job sends it once the queue is back
    assert res.status_code == 202
    assert res.json()["status"] == "pending"


async def test_list_is_newest_first_and_paginates_with_a_cursor(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> None:
    _, key = await create_workspace()
    ids = [(await upload(client, key, f"doc {i}", f"{i}.txt")).json()["id"] for i in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/v1/documents", params=params, headers=auth(key))).json()
        seen += [d["id"] for d in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert seen == list(reversed(ids))


async def test_list_filters_and_bad_cursors(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    ready = (await upload(client, key, "one", "1.txt", source_type="postmortem")).json()
    await upload(client, key, "two", "2.txt")
    await ingest(ready["id"])

    by_status = await client.get("/v1/documents", params={"status": "ready"}, headers=auth(key))
    by_type = await client.get("/v1/documents", params={"source_type": "other"}, headers=auth(key))
    bad = await client.get("/v1/documents", params={"cursor": "nope"}, headers=auth(key))

    assert [d["id"] for d in by_status.json()["items"]] == [ready["id"]]
    assert [d["filename"] for d in by_type.json()["items"]] == ["2.txt"]
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_cursor"


async def test_workspaces_cannot_see_each_others_documents(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, acme = await create_workspace("Acme")
    _, globex = await create_workspace("Globex")
    doc = (await upload(client, acme, RUNBOOK)).json()
    await ingest(doc["id"])
    path = f"/v1/documents/{doc['id']}"

    responses = [
        await client.get(path, headers=auth(globex)),
        await client.get(f"{path}/chunks", headers=auth(globex)),
        await client.post(f"{path}/reingest", headers=auth(globex)),
        await client.delete(path, headers=auth(globex)),
    ]
    listed = await client.get("/v1/documents", headers=auth(globex))

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert listed.json()["items"] == []
    assert (await client.get(path, headers=auth(acme))).status_code == 200


async def test_delete_removes_the_document_and_its_chunks(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    from sqlalchemy import func, select

    from opspilot.models import Chunk

    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK)).json()
    await ingest(doc["id"])

    res = await client.delete(f"/v1/documents/{doc['id']}", headers=auth(key))

    assert res.status_code == 204
    assert (await client.get(f"/v1/documents/{doc['id']}", headers=auth(key))).status_code == 404
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Chunk)) == 0


async def test_reingest(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK)).json()
    path = f"/v1/documents/{doc['id']}/reingest"

    while_pending = await client.post(path, headers=auth(key))
    await ingest(doc["id"])
    after_ready = await client.post(path, headers=auth(key))

    assert while_pending.status_code == 409
    assert while_pending.json()["error"]["code"] == "ingestion_in_progress"
    assert after_ready.status_code == 202
    assert after_ready.json()["status"] == "pending"
    assert enqueued == [uuid.UUID(doc["id"])] * 2

    # Indexing again with smaller chunks replaces the old chunks instead of adding to them
    assert await ingest(doc["id"], chunk_max_tokens=10, chunk_overlap_tokens=0) == Outcome.READY
    chunks = (await client.get(f"/v1/documents/{doc['id']}/chunks", headers=auth(key))).json()
    assert len(chunks) > 2
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
