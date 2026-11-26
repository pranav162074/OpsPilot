"""The migrations build exactly the schema the models describe. The test database is created
by running every migration (see conftest), so any difference here is a missing migration."""

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.config import Settings
from opspilot.models import Base


async def test_migrations_match_the_models(settings: Settings) -> None:
    def diff(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(diff)
    finally:
        await engine.dispose()

    assert differences == []
