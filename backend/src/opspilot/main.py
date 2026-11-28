import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from opspilot import __version__
from opspilot.agent.llm import build_chat_model
from opspilot.api import (
    actions,
    api_keys,
    dead_letters,
    documents,
    health,
    incidents,
    investigations,
    search,
    sources,
    telemetry,
    usage,
    workspaces,
)
from opspilot.api.errors import register_error_handlers
from opspilot.config import Settings, get_settings
from opspilot.db import create_engine, create_sessionmaker
from opspilot.embeddings import build_embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.logging import configure_logging
from opspilot.mcp_server.http import McpEndpoint
from opspilot.middleware import RequestIdMiddleware
from opspilot.ratelimit import RateLimiter
from opspilot.search.rerank import build_reranker
from opspilot.storage import S3Store, build_store
from opspilot.worker import enqueue_ingestion, enqueue_investigation, enqueue_source_sync


def create_app(settings: Settings | None = None) -> FastAPI:
    """Builds the app. Tests pass their own settings, for example a tiny rate limit."""
    settings = settings or get_settings()
    mcp: McpEndpoint | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Connections are created once at startup and shared by every request
        configure_logging(settings.log_level)
        app.state.settings = settings
        app.state.engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        app.state.redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        app.state.rate_limiter = RateLimiter(app.state.redis)
        # Swappable, so tests can capture what would be queued
        app.state.enqueue_ingestion = enqueue_ingestion
        app.state.enqueue_investigation = enqueue_investigation
        app.state.enqueue_source_sync = enqueue_source_sync
        app.state.embedder = build_embedder(settings)
        app.state.llm = build_chat_model(settings)
        app.state.reranker = build_reranker(settings)
        app.state.store = build_store(settings)
        if isinstance(app.state.store, S3Store) and settings.environment == "development":
            # MinIO from docker compose starts empty
            await app.state.store.ensure_bucket()
        # Streamed investigations run as tasks owned by the app, not by their connection
        app.state.running_investigations = set()
        app.state.query_cache = QueryEmbeddingCache(
            app.state.redis, settings.query_embedding_cache_seconds
        )
        # Prometheus, Loki and GitHub requests; no redirects, so a saved URL can't bounce
        # the server somewhere else
        app.state.telemetry_http = httpx.AsyncClient(
            timeout=settings.telemetry_timeout_seconds, follow_redirects=False
        )
        try:
            async with AsyncExitStack() as stack:
                if mcp is not None:
                    await stack.enter_async_context(mcp.running())
                yield
        finally:
            # Streamed runs still going are stopped; they stay "running" with their
            # checkpoint, and the scheduler resumes them in a worker
            for task in list(app.state.running_investigations):
                task.cancel()
            await asyncio.gather(*app.state.running_investigations, return_exceptions=True)
            await app.state.telemetry_http.aclose()
            await app.state.embedder.aclose()
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="OpsPilot", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(workspaces.router)
    app.include_router(api_keys.router)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(investigations.router)
    app.include_router(incidents.router)
    app.include_router(telemetry.router)
    app.include_router(actions.router)
    app.include_router(usage.router)
    app.include_router(sources.router)
    app.include_router(dead_letters.router)
    if settings.mcp_enabled:
        # The same tools for AI assistants, at /mcp (see opspilot/mcp_server)
        mcp = McpEndpoint(app, settings)
        app.router.routes.append(mcp.route)
    return app


app = create_app()
