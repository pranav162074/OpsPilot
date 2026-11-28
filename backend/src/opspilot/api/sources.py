"""Sources: repositories whose documents OpsPilot keeps in sync by itself."""

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import func, select

from opspilot.api.deps import Admin, Responder, SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.models import Document, DocumentStatus, Source, SourceKind, SourceStatus, SourceType
from opspilot.security.secrets import SecretBox, SecretsUnavailable
from opspilot.sources.github import REPOSITORY, GitHub, GitHubError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sources", tags=["sources"])

# Synced documents get these types; code files are always "code"
DOC_TYPES = {SourceType.RUNBOOK, SourceType.POSTMORTEM, SourceType.ARCHITECTURE, SourceType.OTHER}


class GitHubSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SourceKind = SourceKind.GITHUB
    repository: Annotated[str, Field(examples=["acme/runbooks"])]
    # The branch to follow; the repository's default branch if left out
    ref: Annotated[str | None, Field(max_length=200)] = None
    # Only files under these directories ([] = the whole repository)
    paths: Annotated[list[Annotated[str, Field(max_length=300)]], Field(max_length=20)] = []
    # Also index source code, chunked by function
    include_code: bool = False
    source_type: SourceType = SourceType.RUNBOOK
    name: Annotated[str | None, Field(max_length=200)] = None
    # Needed for private repositories: a fine-grained token with read access to contents
    token: SecretStr | None = None

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        value = value.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if not REPOSITORY.match(value):
            raise ValueError("Must look like owner/name")
        return value

    @field_validator("source_type")
    @classmethod
    def _doc_type(cls, value: SourceType) -> SourceType:
        if value not in DOC_TYPES:
            raise ValueError("Code files are typed as code automatically; pick a document type")
        return value


class SourceOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    repository: str
    ref: str
    paths: list[str]
    include_code: bool
    source_type: str
    status: SourceStatus
    last_synced_at: datetime | None
    last_commit: str | None
    last_error: str | None
    last_result: dict[str, Any] | None
    documents: int
    documents_ready: int
    created_at: datetime


class WebhookSetup(BaseModel):
    # Add this in the repository: Settings → Webhooks → Add webhook, content type
    # application/json, just the push event
    url: str
    secret: str


class CreatedSource(SourceOut):
    # Shown once
    webhook: WebhookSetup


def _box(settings: SettingsDep) -> SecretBox:
    return SecretBox(settings.integrations_key)


async def _out(session: SessionDep, source: Source) -> dict[str, Any]:
    counts = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Document.status == DocumentStatus.READY.value),
            ).where(Document.source_id == source.id)
        )
    ).one()
    return {
        "id": source.id,
        "kind": source.kind,
        "name": source.name,
        "repository": source.config["repository"],
        "ref": source.config["ref"],
        "paths": source.config.get("paths", []),
        "include_code": source.config.get("include_code", False),
        "source_type": source.config.get("source_type", SourceType.RUNBOOK.value),
        "status": SourceStatus(source.status),
        "last_synced_at": source.last_synced_at,
        "last_commit": source.last_commit,
        "last_error": source.last_error,
        "last_result": source.last_result,
        "documents": counts[0],
        "documents_ready": counts[1],
        "created_at": source.created_at,
    }


async def _load(session: SessionDep, workspace_id: uuid.UUID, source_id: uuid.UUID) -> Source:
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.workspace_id == workspace_id)
    )
    if source is None:
        raise ApiError(404, "not_found", "Source not found")
    return source


async def _enqueue(request: Request, source_id: uuid.UUID, force: bool = False) -> None:
    try:
        await run_in_threadpool(request.app.state.enqueue_source_sync, source_id, force)
    except Exception:
        # The periodic catch-up syncs it anyway
        logger.warning("Could not queue a sync of source %s", source_id, exc_info=True)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_source(
    body: GitHubSourceIn,
    request: Request,
    principal: Admin,
    session: SessionDep,
    settings: SettingsDep,
) -> CreatedSource:
    """Connects a GitHub repository and starts the first sync. GitHub is asked about the
    repository first, so a typo or a missing token is an error now, not a failed sync later."""
    try:
        box = _box(settings)
        webhook_secret = secrets.token_urlsafe(32)
        token = body.token.get_secret_value() if body.token else None
        encrypted = (box.encrypt(token) if token else None, box.encrypt(webhook_secret))
    except SecretsUnavailable as exc:
        raise ApiError(503, "secrets_unavailable", str(exc)) from exc
    github = GitHub(request.app.state.telemetry_http, settings.github_api_url, token)
    try:
        ref = body.ref or await github.default_branch(body.repository)
        await github.commit(body.repository, ref)
    except GitHubError as exc:
        if exc.transient:
            raise ApiError(503, "github_unavailable", str(exc)) from exc
        raise ApiError(422, "repository_unreachable", str(exc)) from exc

    source = Source(
        workspace_id=principal.workspace.id,
        kind=body.kind.value,
        name=body.name or body.repository,
        config={
            "repository": body.repository,
            "ref": ref,
            "paths": [p.strip("/") for p in body.paths if p.strip("/")],
            "include_code": body.include_code,
            "source_type": body.source_type.value,
        },
        token_encrypted=encrypted[0],
        webhook_secret_encrypted=encrypted[1],
        status=SourceStatus.IDLE.value,
        failures=0,
    )
    session.add(source)
    await session.commit()
    await _enqueue(request, source.id)
    url = str(request.url_for("github_webhook", source_id=source.id))
    return CreatedSource(
        **await _out(session, source), webhook=WebhookSetup(url=url, secret=webhook_secret)
    )


@router.get("")
async def list_sources(principal: Viewer, session: SessionDep) -> list[SourceOut]:
    rows = await session.scalars(
        select(Source)
        .where(Source.workspace_id == principal.workspace.id)
        .order_by(Source.created_at.desc())
    )
    return [SourceOut(**await _out(session, s)) for s in rows.all()]


@router.get("/{source_id}")
async def get_source(source_id: uuid.UUID, principal: Viewer, session: SessionDep) -> SourceOut:
    return SourceOut(**await _out(session, await _load(session, principal.workspace.id, source_id)))


@router.post("/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_now(
    source_id: uuid.UUID,
    request: Request,
    principal: Responder,
    session: SessionDep,
    force: bool = False,
) -> dict[str, str]:
    """Queues a sync. With ?force=true, every file is compared even at an unchanged commit."""
    source = await _load(session, principal.workspace.id, source_id)
    await _enqueue(request, source.id, force)
    return {"status": "queued"}


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, principal: Admin, session: SessionDep) -> Response:
    """Disconnects it. Its documents (and their chunks) are deleted with it."""
    source = await _load(session, principal.workspace.id, source_id)
    await session.delete(source)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/webhook", name="github_webhook", include_in_schema=True)
async def github_webhook(
    source_id: uuid.UUID, request: Request, session: SessionDep, settings: SettingsDep
) -> Response:
    """GitHub calls this on every push. Authenticated by the signature GitHub computes with
    the source's webhook secret (X-Hub-Signature-256), not by an API key."""
    raw = await request.body()
    source = await session.get(Source, source_id)
    signature = request.headers.get("x-hub-signature-256", "")
    if source is None:
        # Same answer as a bad signature: don't confirm which ids exist
        raise ApiError(401, "invalid_signature", "The signature doesn't match")
    try:
        secret = _box(settings).decrypt(source.webhook_secret_encrypted)
    except SecretsUnavailable as exc:
        raise ApiError(503, "secrets_unavailable", str(exc)) from exc
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ApiError(401, "invalid_signature", "The signature doesn't match")

    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return Response(content='{"ok":true}', media_type="application/json")
    if event != "push":
        return Response(content='{"ignored":"event"}', media_type="application/json")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_json", "The body isn't JSON") from exc
    if payload.get("ref") != f"refs/heads/{source.config['ref']}":
        # A push to another branch
        return Response(content='{"ignored":"ref"}', media_type="application/json")
    await _enqueue(request, source.id)
    return Response(
        status_code=status.HTTP_202_ACCEPTED,
        content='{"queued":true}',
        media_type="application/json",
    )
