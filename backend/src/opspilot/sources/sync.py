"""Keeping a workspace's documents in step with a GitHub repository.

A sync is incremental. It reads the branch's latest commit, and stops there if the documents
already match it. Otherwise it lists the whole tree in one request and compares each file's
blob sha with the one stored on its document: only new and changed files are downloaded and
re-indexed, and documents whose file is gone are deleted. A repository of a thousand files
where one changed costs four requests and one re-index.

Which files: those under the configured paths, documentation always (.md, .markdown, .txt,
.rst), code only when asked for (chunked by function, see ingestion/code.py). Dependency and
build directories are skipped, as are files over SOURCE_MAX_FILE_BYTES and anything that isn't
UTF-8 text.

Failures: GitHub being down or rate limited raises SyncRetry, which the worker retries with
backoff (then the dead-letter queue). A revoked token or a deleted repository won't fix itself:
the source is marked failed and the job goes straight to the dead-letter queue.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot import dead_letters
from opspilot.config import Settings
from opspilot.ingestion.code import language_of, media_type_for
from opspilot.models import Document, DocumentStatus, Source, SourceStatus, SourceType
from opspilot.security.injection import remove_invisible
from opspilot.security.secrets import SecretBox
from opspilot.sources.github import GitHub, GitHubError, TreeEntry
from opspilot.storage import ObjectStore, StorageError, document_key

logger = logging.getLogger(__name__)

DOC_SUFFIXES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
}
SKIPPED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".venv",
        "venv",
        ".next",
        "coverage",
    }
)
# A sync stuck this long (a dead worker) can be taken over
STALE_SYNC = timedelta(minutes=15)
DOWNLOAD_CONCURRENCY = 8


class SyncRetry(Exception):
    """A temporary failure: worth trying again later."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class SyncResult:
    status: str  # "synced", "unchanged", "skipped" (another sync has it), "failed"
    commit: str | None = None
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0
    truncated: bool = False
    error: str | None = None
    queued: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("queued")
        return data


def wanted(entry: TreeEntry, config: dict[str, Any], max_bytes: int) -> str | None:
    """The media type to index the file as, or None to leave it out."""
    path = PurePosixPath(entry.path)
    if any(part in SKIPPED_DIRS or part.startswith(".") for part in path.parts[:-1]):
        return None
    prefixes = [p.strip("/") for p in config.get("paths") or [] if p.strip("/")]
    if prefixes and not any(entry.path == p or entry.path.startswith(f"{p}/") for p in prefixes):
        return None
    if entry.size > max_bytes:
        return None
    suffix = path.suffix.lower()
    if suffix in DOC_SUFFIXES:
        return DOC_SUFFIXES[suffix]
    if config.get("include_code") and (language := language_of(entry.path)):
        return media_type_for(language)
    return None


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8000]:
        return None  # binary
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return remove_invisible(text)


async def _claim(
    sessionmaker: async_sessionmaker[AsyncSession], source_id: uuid.UUID, now: datetime
) -> Source | None:
    async with sessionmaker() as session:
        claimed = await session.scalar(
            update(Source)
            .where(
                Source.id == source_id,
                (Source.status != SourceStatus.SYNCING.value)
                | (Source.sync_started_at < now - STALE_SYNC),
            )
            .values(status=SourceStatus.SYNCING.value, sync_started_at=now)
            .returning(Source)
        )
        await session.commit()
        return claimed


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    result: SyncResult,
    now: datetime,
) -> None:
    async with sessionmaker() as session:
        values: dict[str, Any] = {"last_result": result.summary(), "sync_started_at": None}
        if result.status == "failed":
            values |= {
                "status": SourceStatus.FAILED.value,
                "last_error": result.error,
                "failures": Source.failures + 1,
            }
        else:
            values |= {
                "status": SourceStatus.IDLE.value,
                "last_error": None,
                "failures": 0,
                "last_synced_at": now,
                "last_commit": result.commit,
            }
        await session.execute(update(Source).where(Source.id == source_id).values(**values))
        await session.commit()


async def sync_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    source_id: uuid.UUID,
    http: httpx.AsyncClient,
    store: ObjectStore,
    *,
    force: bool = False,
    attempt: int = 1,
) -> SyncResult:
    now = datetime.now(UTC)
    source = await _claim(sessionmaker, source_id, now)
    if source is None:
        return SyncResult(status="skipped")
    config = source.config
    repository: str = config["repository"]
    ref: str = config["ref"]
    box = SecretBox(settings.integrations_key)
    token = box.decrypt(source.token_encrypted) if source.token_encrypted else None
    github = GitHub(http, settings.github_api_url, token)
    result = SyncResult(status="synced")

    try:
        commit = await github.commit(repository, ref)
        result.commit = commit
        if commit == source.last_commit and not force:
            result.status = "unchanged"
            async with sessionmaker() as session:
                result.unchanged = len(
                    (
                        await session.scalars(
                            select(Document.id).where(Document.source_id == source_id)
                        )
                    ).all()
                )
            await _finish(sessionmaker, source_id, result, now)
            return result

        tree = await github.tree(repository, commit)
        result.truncated = tree.truncated
        selected: dict[str, tuple[TreeEntry, str]] = {}
        for entry in tree.entries:
            media_type = wanted(entry, config, settings.source_max_file_bytes)
            if media_type is None:
                continue
            if len(selected) >= settings.source_max_files:
                result.truncated = True
                break
            selected[entry.path] = (entry, media_type)

        async with sessionmaker() as session:
            existing = {
                row.external_path: row
                for row in (
                    await session.execute(
                        select(
                            Document.id,
                            Document.external_path,
                            Document.external_sha,
                            Document.storage_key,
                        ).where(Document.source_id == source_id)
                    )
                ).all()
            }
        changed = [
            (entry, media_type)
            for path, (entry, media_type) in selected.items()
            if path not in existing or existing[path].external_sha != entry.sha
        ]
        result.unchanged = len(selected) - len(changed)

        limit = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        async def download(entry: TreeEntry) -> bytes:
            async with limit:
                return await github.blob(repository, entry.sha)

        blobs = await asyncio.gather(*(download(entry) for entry, _ in changed))
    except GitHubError as exc:
        if exc.transient:
            await _finish(sessionmaker, source_id, SyncResult(status="failed", error=str(exc)), now)
            raise SyncRetry(str(exc), exc.retry_after) from exc
        result = SyncResult(status="failed", error=str(exc), commit=None)
        await _finish(sessionmaker, source_id, result, now)
        await dead_letters.record_now(
            sessionmaker,
            task=dead_letters.SOURCE_SYNC,
            job_key=f"source:{source_id}",
            payload={"source_id": str(source_id), "force": force},
            error=str(exc),
            attempts=attempt,
            workspace_id=source.workspace_id,
        )
        return result

    # Writing: originals to object storage first (named by content, so nothing is
    # overwritten), then all document changes in one transaction
    default_type = config.get("source_type") or SourceType.RUNBOOK.value
    stale_objects: list[str] = []
    async with sessionmaker() as session:
        for (entry, media_type), data in zip(changed, blobs, strict=True):
            text = _decode(data)
            if text is None or not text.strip():
                result.skipped += 1
                continue
            previous = existing.get(entry.path)
            document_id = previous.id if previous else uuid.uuid4()
            key: str | None = None
            if store.name != "none":
                key = document_key(source.workspace_id, document_id, data)
                try:
                    await store.put(key, data, media_type)
                except StorageError:
                    # The text is what's indexed; the original is a copy worth having, not
                    # worth failing the sync for
                    logger.warning("Couldn't store the original of %s", entry.path)
                    key = None
            path = PurePosixPath(entry.path)
            fields = {
                "title": entry.path[:200],
                "filename": path.name[:255],
                "source_type": SourceType.CODE.value
                if media_type.startswith("text/x-")
                else default_type,
                "media_type": media_type,
                "content": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "size_bytes": len(data),
                "external_sha": entry.sha,
                "storage_key": key,
                "status": DocumentStatus.PENDING.value,
                "attempts": 0,
                "error": None,
                "updated_at": now,
            }
            if previous:
                await session.execute(
                    update(Document).where(Document.id == previous.id).values(**fields)
                )
                if previous.storage_key and previous.storage_key != key:
                    stale_objects.append(previous.storage_key)
                result.updated += 1
            else:
                session.add(
                    Document(
                        id=document_id,
                        workspace_id=source.workspace_id,
                        source_id=source_id,
                        external_path=entry.path,
                        **fields,
                    )
                )
                result.added += 1
            result.queued.append(document_id)

        gone = [row for path, row in existing.items() if path not in selected]
        if gone:
            await session.execute(delete(Document).where(Document.id.in_([row.id for row in gone])))
            stale_objects += [row.storage_key for row in gone if row.storage_key]
            result.deleted = len(gone)
        await session.commit()

    for key in stale_objects:
        try:
            await store.delete(key)
        except StorageError as exc:
            await dead_letters.record_now(
                sessionmaker,
                task=dead_letters.DELETE_OBJECT,
                job_key=f"object:{key}",
                payload={"key": key},
                error=str(exc),
                workspace_id=source.workspace_id,
            )
    await _finish(sessionmaker, source_id, result, now)
    logger.info("Synced source %s at %s: %s", source_id, result.commit, result.summary())
    return result


async def due_sources(session: AsyncSession, settings: Settings, now: datetime) -> list[uuid.UUID]:
    """Sources not synced for a while: the catch-up for webhooks that never arrived."""
    cutoff = now - timedelta(minutes=settings.source_sync_interval_minutes)
    rows = await session.scalars(
        select(Source.id).where(
            Source.status != SourceStatus.SYNCING.value,
            (Source.last_synced_at.is_(None)) | (Source.last_synced_at < cutoff),
            # A source that keeps failing is left for a person (it's in the dead-letter queue)
            Source.failures < 5,
        )
    )
    return list(rows.all())
