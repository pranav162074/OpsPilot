"""Sources (GitHub sync), originals in object storage, the dead-letter queue, and code chunking.
GitHub is an in-process fake behind httpx's MockTransport and S3 is moto: no network."""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from moto import mock_aws
from sqlalchemy import select, update

from opspilot import dead_letters
from opspilot.config import Settings
from opspilot.ingestion.code import chunk_code, language_of
from opspilot.ingestion.service import Outcome
from opspilot.models import DeadLetter, Document, Source
from opspilot.sources.github import TreeEntry
from opspilot.sources.sync import sync_source, wanted
from opspilot.storage import LocalStore, NoStore, S3Store, StorageError, document_key
from tests.conftest import auth, upload

REPO = "acme/platform"

RUNBOOK = """# Payments API

## Connection pool exhaustion

Requests fail with ERR_POOL_TIMEOUT. Cancel long transactions with pg_cancel_backend.
"""

RETRY_PY = '''"""Gateway retries."""
import time

MAX_ATTEMPTS = 5


def backoff(attempt: int) -> float:
    """Seconds to wait before the next attempt: exponential, capped at 30."""
    return min(30.0, 0.5 * 2**attempt)


class GatewayClient:
    """Talks to the payment gateway."""

    def charge(self, amount: int) -> str:
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._post("/charge", amount)
            except TimeoutError:
                time.sleep(backoff(attempt))
        raise RuntimeError("gave up")
'''


class FakeGitHub:
    """Just enough of GitHub's REST API: repos, commits, trees and blobs."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.commit = "c0ffee" + "0" * 34
        self.calls: list[str] = []
        self.status: int | None = None  # every request answers this, when set
        self.token: str | None = None  # required when set

    def push(self, files: dict[str, bytes | None]) -> None:
        for path, data in files.items():
            if data is None:
                self.files.pop(path, None)
            else:
                self.files[path] = data
        self.commit = hashlib.sha1(
            json.dumps(sorted(self.files), default=str).encode() + self.commit.encode(),
            usedforsecurity=False,
        ).hexdigest()

    @staticmethod
    def sha(data: bytes) -> str:
        # The real git blob sha
        return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.status:
            return httpx.Response(self.status, json={"message": "nope"})
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.startswith(f"/repos/{REPO}/commits/"):
            ref = path.rsplit("/", 1)[1]
            if ref not in ("main", self.commit):
                return httpx.Response(404, json={"message": "No commit found"})
            return httpx.Response(200, json={"sha": self.commit})
        if path == f"/repos/{REPO}/git/trees/{self.commit}":
            tree = [
                {"path": p, "type": "blob", "sha": self.sha(d), "size": len(d)}
                for p, d in self.files.items()
            ]
            tree += [{"path": "docs", "type": "tree", "sha": "t" * 40}]
            return httpx.Response(200, json={"sha": self.commit, "tree": tree, "truncated": False})
        if path.startswith(f"/repos/{REPO}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            for data in self.files.values():
                if self.sha(data) == sha:
                    return httpx.Response(
                        200,
                        json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
                    )
        return httpx.Response(404, json={"message": "Not Found"})

    def blob_calls(self) -> int:
        return sum(1 for c in self.calls if "/git/blobs/" in c)


@pytest.fixture
def github(app: FastAPI) -> FakeGitHub:
    fake = FakeGitHub()
    app.state.telemetry_http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def store(app: FastAPI, tmp_path: Path) -> LocalStore:
    local = LocalStore(str(tmp_path / "objects"))
    app.state.store = local
    return local


async def connect(client: AsyncClient, key: str, **body: Any) -> Any:
    res = await client.post(
        "/v1/sources",
        json={"repository": REPO, "paths": ["docs", "src"], **body},
        headers=auth(key),
    )
    assert res.status_code == 201, res.text
    return res.json()


async def sync(
    app: FastAPI, settings: Settings, source_id: str, store: Any = None, force: bool = False
) -> Any:
    return await sync_source(
        app.state.sessionmaker,
        settings,
        uuid.UUID(source_id),
        app.state.telemetry_http,
        store or NoStore(),
        force=force,
    )


async def documents(app: FastAPI, source_id: str) -> dict[str, Document]:
    async with app.state.sessionmaker() as session:
        rows = await session.scalars(
            select(Document).where(Document.source_id == uuid.UUID(source_id))
        )
        return {d.external_path or "": d for d in rows.all()}


# ------------------------------------------------------------------------- GitHub sync --


async def test_a_repository_syncs_incrementally(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    github: FakeGitHub,
    queued_syncs: list[Any],
    store: LocalStore,
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    github.push(
        {
            "docs/payments.md": RUNBOOK.encode(),
            "docs/old.md": b"# Old\n\nRetired.\n",
            "docs/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
            "src/gateway/retry.py": RETRY_PY.encode(),
            "src/node_modules/left-pad/index.js": b"module.exports = 1;",
            "README.md": b"# Not under docs/ or src/\n",
        }
    )

    source = await connect(client, key, include_code=True)
    first = await sync(app, settings, source["id"], store)

    assert queued_syncs == [(uuid.UUID(source["id"]), False)]
    assert source["ref"] == "main"  # the repository's default branch
    assert source["webhook"]["url"].endswith(f"/v1/sources/{source['id']}/webhook")
    assert first.status == "synced"
    assert (first.added, first.updated, first.deleted) == (3, 0, 0)
    docs = await documents(app, source["id"])
    assert set(docs) == {"docs/payments.md", "docs/old.md", "src/gateway/retry.py"}
    assert docs["src/gateway/retry.py"].source_type == "code"
    assert docs["src/gateway/retry.py"].media_type == "text/x-python"
    assert docs["docs/payments.md"].source_type == "runbook"
    assert set(first.queued) == {d.id for d in docs.values()}
    # The originals are kept, byte for byte
    original = await client.get(
        f"/v1/documents/{docs['src/gateway/retry.py'].id}/original", headers=auth(key)
    )
    assert original.content == RETRY_PY.encode()
    assert original.headers["content-disposition"] == 'attachment; filename="retry.py"'

    # Indexed, code is searchable by function
    for document in docs.values():
        assert await ingest(document.id) == Outcome.READY
    hit = (
        await client.post(
            "/v1/search",
            json={"query": "GatewayClient.charge retry", "limit": 3},
            headers=auth(key),
        )
    ).json()["results"]
    headings = {r["heading"] for r in hit}
    assert "GatewayClient" in headings or "GatewayClient.charge" in headings

    # Same commit: one request, nothing downloaded
    github.calls.clear()
    unchanged = await sync(app, settings, source["id"], store)
    assert unchanged.status == "unchanged"
    assert github.calls == [f"/repos/{REPO}/commits/main"]

    # One file changed, one deleted, one added: only those are downloaded
    github.push(
        {
            "docs/payments.md": (RUNBOOK + "\nAlso check the pool size.\n").encode(),
            "docs/old.md": None,
            "docs/new.md": b"# New\n\nFresh.\n",
        }
    )
    github.calls.clear()
    second = await sync(app, settings, source["id"], store)
    after = await documents(app, source["id"])

    assert (second.added, second.updated, second.deleted, second.unchanged) == (1, 1, 1, 1)
    assert github.blob_calls() == 2
    assert set(after) == {"docs/payments.md", "docs/new.md", "src/gateway/retry.py"}
    assert after["docs/payments.md"].id == docs["docs/payments.md"].id  # updated in place
    assert after["docs/payments.md"].status == "pending"
    assert "pool size" in after["docs/payments.md"].content
    listed = (await client.get(f"/v1/sources/{source['id']}", headers=auth(key))).json()
    assert listed["status"] == "idle"
    assert listed["last_commit"] == github.commit
    assert listed["documents"] == 3
    assert listed["last_result"]["added"] == 1


async def test_files_with_the_same_content_dont_collide(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    github: FakeGitHub,
    queued_syncs: list[Any],
    enqueued: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    github.push({"docs/a/README.md": b"# Same\n", "docs/b/README.md": b"# Same\n"})
    source = await connect(client, key)
    # The same text uploaded by hand is still allowed, and still a duplicate of itself
    assert (await upload(client, key, "# Same\n", "same.md")).status_code == 202

    result = await sync(app, settings, source["id"])

    assert result.added == 2
    assert (await upload(client, key, "# Same\n", "again.md")).status_code == 409


async def test_github_problems_retry_or_go_to_the_dead_letter_queue(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    github: FakeGitHub,
    queued_syncs: list[Any],
) -> None:
    from opspilot.sources.sync import SyncRetry

    _, key = await create_workspace()
    github.push({"docs/a.md": b"# A\n"})
    source = await connect(client, key)

    github.status = 502
    with pytest.raises(SyncRetry):
        await sync(app, settings, source["id"])
    github.status = 401  # the token was revoked: waiting won't help
    revoked = await sync(app, settings, source["id"])
    again = await sync(app, settings, source["id"])

    assert revoked.status == "failed"
    assert "refused the token" in (revoked.error or "")
    async with app.state.sessionmaker() as session:
        letters = (await session.scalars(select(DeadLetter))).all()
        row = await session.get(Source, uuid.UUID(source["id"]))
    assert row is not None and row.status == "failed" and row.failures == 3
    assert again.status == "failed"
    # One dead letter for the job, counting its failures
    assert [(d.task, d.failures) for d in letters] == [(dead_letters.SOURCE_SYNC, 2)]

    github.status = None
    listed = (await client.get("/v1/dead-letters", headers=auth(key))).json()
    retried = await client.post(f"/v1/dead-letters/{listed[0]['id']}/retry", headers=auth(key))
    twice = await client.post(f"/v1/dead-letters/{listed[0]['id']}/retry", headers=auth(key))
    assert retried.json()["status"] == "retried"
    assert queued_syncs[-1] == (uuid.UUID(source["id"]), False)
    assert twice.status_code == 409
    assert (await sync(app, settings, source["id"])).status == "synced"


async def test_connecting_checks_the_repository_and_needs_admin(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    github: FakeGitHub,
    queued_syncs: list[Any],
) -> None:
    _, key = await create_workspace()
    responder = (
        await client.post(
            "/v1/api-keys", json={"name": "r", "role": "responder"}, headers=auth(key)
        )
    ).json()["key"]
    github.token = "ghp_secret"

    private = await client.post("/v1/sources", json={"repository": REPO}, headers=auth(key))
    bad_name = await client.post(
        "/v1/sources", json={"repository": "not a repo"}, headers=auth(key)
    )
    not_admin = await client.post(
        "/v1/sources", json={"repository": REPO, "token": "ghp_secret"}, headers=auth(responder)
    )
    ok = await client.post(
        "/v1/sources",
        json={"repository": f"https://github.com/{REPO}.git", "token": "ghp_secret"},
        headers=auth(key),
    )

    assert private.status_code == 422
    assert private.json()["error"]["code"] == "repository_unreachable"
    assert bad_name.status_code == 422
    assert not_admin.status_code == 403
    assert ok.status_code == 201
    assert ok.json()["repository"] == REPO
    async with app.state.sessionmaker() as session:
        row = await session.get(Source, uuid.UUID(ok.json()["id"]))
    assert row is not None and "ghp_secret" not in (row.token_encrypted or "")
    listed = (await client.get("/v1/sources", headers=auth(responder))).json()
    assert "secret" not in json.dumps(listed)
    sync_ = await client.post(
        f"/v1/sources/{ok.json()['id']}/sync?force=true", headers=auth(responder)
    )
    assert sync_.status_code == 202
    assert queued_syncs[-1] == (uuid.UUID(ok.json()["id"]), True)


async def test_push_webhooks_must_be_signed(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    github: FakeGitHub,
    queued_syncs: list[Any],
) -> None:
    _, key = await create_workspace()
    source = await connect(client, key)
    url = f"/v1/sources/{source['id']}/webhook"
    secret = source["webhook"]["secret"]
    queued_syncs.clear()

    def signed(event: str, body: dict[str, Any], key_: str = secret) -> Awaitable[httpx.Response]:
        raw = json.dumps(body).encode()
        signature = "sha256=" + hmac.new(key_.encode(), raw, hashlib.sha256).hexdigest()
        return client.post(
            url,
            content=raw,
            headers={
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )

    ping = await signed("ping", {"zen": "Keep it logically awesome."})
    push = await signed("push", {"ref": "refs/heads/main"})
    other_branch = await signed("push", {"ref": "refs/heads/feature"})
    forged = await signed("push", {"ref": "refs/heads/main"}, "not-the-secret")
    unsigned = await client.post(
        url, json={"ref": "refs/heads/main"}, headers={"X-GitHub-Event": "push"}
    )
    unknown = await client.post(
        f"/v1/sources/{uuid.uuid4()}/webhook", json={}, headers={"X-GitHub-Event": "push"}
    )

    assert ping.status_code == 200
    assert push.status_code == 202
    assert other_branch.json() == {"ignored": "ref"}
    assert forged.status_code == 401
    assert unsigned.status_code == 401
    assert unknown.status_code == 401
    assert queued_syncs == [(uuid.UUID(source["id"]), False)]


def test_which_files_are_synced() -> None:
    config = {"paths": ["docs"], "include_code": False}
    entry = lambda path, size=10: TreeEntry(path=path, sha="x", size=size)  # noqa: E731
    assert wanted(entry("docs/runbook.md"), config, 1000) == "text/markdown"
    assert wanted(entry("docs/notes.rst"), config, 1000) == "text/plain"
    assert wanted(entry("docs/app.py"), config, 1000) is None  # code not asked for
    assert wanted(entry("docs/app.py"), {**config, "include_code": True}, 1000) == "text/x-python"
    assert wanted(entry("docsy/x.md"), config, 1000) is None  # a prefix, not a directory
    assert wanted(entry("docs/big.md", 2000), config, 1000) is None
    assert wanted(entry("docs/.github/x.md"), config, 1000) is None
    assert wanted(entry("x/node_modules/y.md"), {"paths": []}, 1000) is None
    assert wanted(entry("anything/a.md"), {"paths": []}, 1000) == "text/markdown"


# --------------------------------------------------------------------- object storage --


async def test_uploads_keep_their_original(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    store: LocalStore,
) -> None:
    _, key = await create_workspace()
    content = "# Runbook\r\n\r\nWindows line endings, kept in the original.\r\n"

    created = (await upload(client, key, content, "runbook.md")).json()
    original = await client.get(f"/v1/documents/{created['id']}/original", headers=auth(key))
    async with app.state.sessionmaker() as session:
        doc = await session.get(Document, uuid.UUID(created["id"]))

    assert created["has_original"] is True
    assert original.content == content.encode()  # as uploaded; the index has normalized text
    assert original.headers["x-content-type-options"] == "nosniff"
    assert (
        doc is not None
        and doc.storage_key
        and doc.storage_key.endswith(hashlib.sha256(content.encode()).hexdigest())
    )
    # Through the store: a plain path would hit Windows' MAX_PATH, as the store itself did
    assert await store.get(doc.storage_key) == content.encode()

    deleted = await client.delete(f"/v1/documents/{created['id']}", headers=auth(key))
    assert deleted.status_code == 204
    assert await store.get(doc.storage_key) is None


class BrokenStore(LocalStore):
    async def put(self, key: str, data: bytes, content_type: str) -> None:
        raise StorageError("MinIO is down")

    async def delete(self, key: str) -> None:
        raise StorageError("MinIO is down")


async def test_storage_failures(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    store: LocalStore,
) -> None:
    _, key = await create_workspace()
    kept = (await upload(client, key, "# Kept\n", "kept.md")).json()
    async with app.state.sessionmaker() as session:
        kept_doc = await session.get(Document, uuid.UUID(kept["id"]))
    assert kept_doc is not None and kept_doc.storage_key
    kept_key = kept_doc.storage_key
    app.state.store = BrokenStore(str(store.root))

    refused = await upload(client, key, "# New\n", "new.md")
    deleted = await client.delete(f"/v1/documents/{kept['id']}", headers=auth(key))
    letters = (await client.get("/v1/dead-letters", headers=auth(key))).json()

    assert refused.status_code == 503
    assert deleted.status_code == 204  # the document goes; its file waits in the queue
    assert [(d["task"], d["error"]) for d in letters] == [
        (dead_letters.DELETE_OBJECT, "MinIO is down")
    ]
    still_down = await client.post(f"/v1/dead-letters/{letters[0]['id']}/retry", headers=auth(key))
    assert still_down.status_code == 503
    app.state.store = store
    fixed = await client.post(f"/v1/dead-letters/{letters[0]['id']}/retry", headers=auth(key))
    assert fixed.json()["status"] == "retried"
    assert await store.get(kept_key) is None


@pytest.fixture
def s3() -> Iterator[S3Store]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        yield S3Store(client, "opspilot-test")


async def test_the_s3_store(s3: S3Store) -> None:
    await s3.ensure_bucket()
    await s3.ensure_bucket()  # twice is fine
    key = document_key(uuid.uuid4(), uuid.uuid4(), b"hello")

    await s3.put(key, b"hello", "text/plain")

    assert await s3.get(key) == b"hello"
    assert await s3.get("missing/key") is None
    await s3.delete(key)
    await s3.delete(key)  # deleting what's gone is fine
    assert await s3.get(key) is None
    broken = S3Store(s3.client, "no-such-bucket")
    with pytest.raises(StorageError):
        await broken.put(key, b"x", "text/plain")


def test_the_local_store_stays_in_its_directory(tmp_path: Path) -> None:
    import asyncio

    local = LocalStore(str(tmp_path))
    with pytest.raises(StorageError):
        asyncio.run(local.put("../../etc/passwd", b"x", "text/plain"))


# -------------------------------------------------------------------- dead letters --


async def test_ingestion_that_keeps_failing_lands_in_the_queue_and_can_be_retried(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    other_ws, other_key = await create_workspace("Globex")
    doc = (await upload(client, key, RUNBOOK, "payments.md")).json()
    # It has been tried as often as allowed already
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(Document)
            .where(Document.id == uuid.UUID(doc["id"]))
            .values(attempts=settings.ingestion_max_attempts)
        )
        await session.commit()

    assert await ingest(doc["id"]) == Outcome.FAILED
    letters = (await client.get("/v1/dead-letters", headers=auth(key))).json()
    foreign = await client.post(
        f"/v1/dead-letters/{letters[0]['id']}/retry", headers=auth(other_key)
    )
    enqueued.clear()
    retried = await client.post(f"/v1/dead-letters/{letters[0]['id']}/retry", headers=auth(key))

    assert [(d["task"], d["job_key"]) for d in letters] == [
        (dead_letters.INGESTION, f"document:{doc['id']}")
    ]
    assert "Gave up after" in letters[0]["error"]
    assert foreign.status_code == 404
    assert retried.json()["status"] == "retried"
    assert enqueued == [uuid.UUID(doc["id"])]
    # Back to a fresh start, and it works this time
    assert await ingest(doc["id"]) == Outcome.READY
    assert (await client.get("/v1/dead-letters", headers=auth(key))).json() == []
    history = (await client.get("/v1/dead-letters?status=retried", headers=auth(key))).json()
    assert len(history) == 1
    discard_closed = await client.post(
        f"/v1/dead-letters/{history[0]['id']}/discard", headers=auth(key)
    )
    assert discard_closed.status_code == 409


# ---------------------------------------------------------------------- code chunking --


def test_python_is_chunked_by_function_and_class() -> None:
    chunks = chunk_code(RETRY_PY, "python", 400)
    assert [c.heading for c in chunks] == ["(module)", "backoff", "GatewayClient"]
    for c in chunks:
        assert RETRY_PY[c.char_start : c.char_end] == c.content
    # Too big for one chunk: the class header, then each method
    small = chunk_code(RETRY_PY, "python", 40)
    assert "GatewayClient.charge" in [c.heading.split(" (part")[0] for c in small]
    assert language_of("src/app.tsx") == "typescript"
    assert language_of("README.md") is None


def test_brace_languages_are_chunked_by_declaration() -> None:
    ts = """import { db } from "./db";

// Charges a card.
export async function charge(amount: number): Promise<string> {
  const note = "a { brace } in a string";
  if (amount <= 0) {
    throw new Error("bad amount");
  }
  return db.insert({ amount });
}

export class PaymentClient {
  constructor(private key: string) {}

  retry(times: number): void {
    for (let i = 0; i < times; i++) {}
  }
}
"""
    assert [c.heading for c in chunk_code(ts, "typescript", 400)] == [
        "(module)",
        "charge",
        "PaymentClient",
    ]
    split = [c.heading for c in chunk_code(ts, "typescript", 30)]
    assert "PaymentClient.retry" in split
    go = """package main

type Server struct {
\taddr string
}

// Start listens.
func (s *Server) Start() error {
\treturn nil
}
"""
    assert [c.heading for c in chunk_code(go, "go", 400)] == ["(module)", "Server", "Server.Start"]
    # Unparseable Python still gets indexed
    assert [c.heading for c in chunk_code("def broken(:\n    pass\n", "python", 400)] == [
        "(module)"
    ]
