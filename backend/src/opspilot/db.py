from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from opspilot.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # pool_pre_ping replaces connections the database closed while they sat idle
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
