"""Maintenance commands.

uv run python -m opspilot.cli reindex          # documents not embedded by the current model
uv run python -m opspilot.cli reindex --all    # every document
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opspilot.config import get_settings
from opspilot.embeddings import build_embedder
from opspilot.ingestion.service import mark_for_reindex
from opspilot.worker import enqueue_ingestion


async def reindex(everything: bool) -> int:
    settings = get_settings()
    embedder = build_embedder(settings)
    await embedder.aclose()
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            ids = await mark_for_reindex(session, embedder.model, everything)
    finally:
        await engine.dispose()
    for document_id in ids:
        enqueue_ingestion(document_id)
    return len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(prog="opspilot")
    commands = parser.add_subparsers(dest="command", required=True)
    reindex_cmd = commands.add_parser("reindex", help="Queue documents to be indexed again")
    reindex_cmd.add_argument("--all", action="store_true", help="Every document, not just stale")
    args = parser.parse_args()

    if args.command == "reindex":
        count = asyncio.run(reindex(args.all))
        print(f"Queued {count} documents for re-indexing (the worker must be running)")


if __name__ == "__main__":
    main()
