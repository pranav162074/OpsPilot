"""Tests run against their own database (<name>_test) and Redis database 15, so they never
touch development data. Both are wiped between tests."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.config import Settings, get_settings
from opspilot.embeddings import HashingEmbedder
from opspilot.ingestion.service import Outcome, ingest_document
from opspilot.main import create_app

ADMIN_TOKEN = "test-admin-token"


TEST_INTEGRATIONS_KEY = "x1VYkO0tCXOuwtq2B8JKxqOTOGiTbXg8vY1ZVuCtEEk="


def _test_settings(**overrides: Any) -> Settings:
    base = get_settings()
    db_url = make_url(base.database_url)
    test_db = db_url.set(database=f"{db_url.database}_test")
    redis_url = base.redis_url.rsplit("/", 1)[0] + "/15"
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": test_db.render_as_string(hide_password=False),
        "redis_url": redis_url,
        "admin_token": ADMIN_TOKEN,
        # Tests never call paid APIs, whatever your .env says: offline embeddings, and no
        # language model unless a test installs a scripted one
        "embedding_provider": "hashing",
        "gemini_api_key": None,
        # A fixed test key, so integration tokens can be stored; never use it anywhere else
        "integrations_key": TEST_INTEGRATIONS_KEY,
    }
    values.update(overrides)
    return Settings(**values)


async def _create_database(settings: Settings) -> None:
    url = make_url(settings.database_url)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    settings = _test_settings()
    asyncio.run(_create_database(settings))
    config = Config("alembic.ini")
    # env.py reads the URL from settings, so point it at the test database for this run
    get_settings.cache_clear()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = settings.database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return _test_settings()


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    return lambda **overrides: create_app(_test_settings(**overrides))


async def _clean(settings: Settings) -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE workspaces, api_keys, documents, chunks, investigations, incidents,"
                " incident_events CASCADE"
            )
        )
    await engine.dispose()
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    await _clean(settings)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def enqueued(app: FastAPI) -> list[uuid.UUID]:
    """Document IDs the API queued for ingestion. Nothing reaches a real Celery broker."""
    queued: list[uuid.UUID] = []
    app.state.enqueue_ingestion = queued.append
    return queued


@pytest.fixture
def queued_investigations(app: FastAPI) -> list[uuid.UUID]:
    """Investigation IDs handed to the worker. Nothing reaches a real Celery broker."""
    queued: list[uuid.UUID] = []
    app.state.enqueue_investigation = queued.append
    return queued


@pytest.fixture
def queued_syncs(app: FastAPI) -> list[tuple[uuid.UUID, bool]]:
    """Source syncs handed to the worker, with their force flag. No real broker."""
    queued: list[tuple[uuid.UUID, bool]] = []
    app.state.enqueue_source_sync = lambda source_id, force=False: queued.append((source_id, force))
    return queued


@pytest.fixture
def ingest(app: FastAPI, settings: Settings) -> Callable[..., Awaitable[Outcome]]:
    """Runs ingestion the way the worker would, in the test's event loop."""

    async def run(document_id: str | uuid.UUID, **overrides: Any) -> Outcome:
        effective = settings.model_copy(update=overrides) if overrides else settings
        return await ingest_document(
            app.state.sessionmaker, effective, uuid.UUID(str(document_id)), HashingEmbedder()
        )

    return run


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def create_workspace(client: AsyncClient) -> Callable[..., Any]:
    """Creates a workspace and returns (workspace, admin key)."""

    async def create(name: str = "Acme") -> tuple[dict[str, Any], str]:
        res = await client.post(
            "/v1/workspaces", json={"name": name}, headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert res.status_code == 201, res.text
        body = res.json()
        return body["workspace"], body["api_key"]["key"]

    return create


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def upload(
    client: AsyncClient,
    key: str,
    content: str | bytes = "# Runbook\n\nRestart the service.\n",
    filename: str = "runbook.md",
    **form: str,
) -> Response:
    data = content.encode() if isinstance(content, str) else content
    return await client.post(
        "/v1/documents", files={"file": (filename, data)}, data=form, headers=auth(key)
    )
