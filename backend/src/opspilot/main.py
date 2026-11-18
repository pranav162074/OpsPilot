from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from opspilot import __version__
from opspilot.api import health
from opspilot.config import get_settings
from opspilot.db import create_engine
from opspilot.logging import configure_logging
from opspilot.middleware import RequestIdMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Connections are created once at startup and shared by every request
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.engine = create_engine(settings)
    app.state.redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="OpsPilot", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    return app


app = create_app()
