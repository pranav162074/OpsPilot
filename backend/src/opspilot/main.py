from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from opspilot import __version__
from opspilot.agent.llm import build_chat_model
from opspilot.api import (
    api_keys,
    documents,
    health,
    incidents,
    investigations,
    search,
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
from opspilot.worker import enqueue_ingestion, enqueue_investigation


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
        app.state.embedder = build_embedder(settings)
        app.state.llm = build_chat_model(settings)
        app.state.query_cache = QueryEmbeddingCache(
            app.state.redis, settings.query_embedding_cache_seconds
        )
        try:
            async with AsyncExitStack() as stack:
                if mcp is not None:
                    await stack.enter_async_context(mcp.running())
                yield
        finally:
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
    if settings.mcp_enabled:
        # The same tools for AI assistants, at /mcp (see opspilot/mcp_server)
        mcp = McpEndpoint(app, settings)
        app.router.routes.append(mcp.route)
    return app


app = create_app()
