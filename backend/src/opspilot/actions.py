"""Approved actions leave OpsPilot through one door: the workspace's "actions" webhook.

The agent can only suggest an action. A person with the responder role approves it, and only
then is it POSTed, signed, to the URL the workspace's admin configured (a runbook automation
service, a ChatOps bot, a CI job). Without that webhook an approved action is simply marked
approved, for the engineer to carry out by hand.

The receiver verifies the request the same way Stripe and GitHub webhooks are verified:

    X-OpsPilot-Timestamp: <unix seconds>
    X-OpsPilot-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with the secret>

The timestamp is signed too, so the receiver can refuse replays older than a few minutes.
"""

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings
from opspilot.models import Integration, IntegrationKind, InvestigationAction
from opspilot.security.secrets import SecretBox, SecretsUnavailable
from opspilot.telemetry.urls import UnsafeUrl, ensure_public_host

# Enough of the receiver's answer to show what happened, not a whole HTML error page
RESULT_BODY_CHARS = 1000


@dataclass(frozen=True)
class ActionsWebhook:
    url: str
    secret: str


class WebhookUnavailable(Exception):
    """The webhook is configured, but its signing secret can't be decrypted."""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """What a receiver does (and what the tests use)."""
    if not timestamp.isdigit():
        return False
    return hmac.compare_digest(sign(secret, int(timestamp), body), signature)


async def load_webhook(
    session: AsyncSession, workspace_id: uuid.UUID, settings: Settings
) -> ActionsWebhook | None:
    row = await session.scalar(
        select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.kind == IntegrationKind.ACTIONS.value,
        )
    )
    if row is None:
        return None
    if not row.secret_encrypted:
        raise WebhookUnavailable("The actions webhook has no signing secret")
    try:
        secret = SecretBox(settings.integrations_key).decrypt(row.secret_encrypted)
    except SecretsUnavailable as exc:
        raise WebhookUnavailable(str(exc)) from exc
    return ActionsWebhook(url=row.url, secret=secret)


def payload(action: InvestigationAction, incident_id: uuid.UUID | None) -> dict[str, Any]:
    return {
        "type": "action.approved",
        "action": {
            "id": str(action.id),
            "title": action.title,
            "command": action.command,
            "risk": action.risk,
            "verified": action.verified,
            "flags": action.flags,
            "rationale": action.rationale,
            "approved_by": action.decided_by,
            "note": action.decision_note,
        },
        "investigation_id": str(action.investigation_id),
        "incident_id": str(incident_id) if incident_id else None,
    }


async def send(
    http: httpx.AsyncClient,
    webhook: ActionsWebhook,
    body: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """POSTs one signed event. Returns what happened; never raises for the receiver's sake."""
    if not settings.allow_private_integration_urls:
        try:
            await ensure_public_host(webhook.url)
        except UnsafeUrl as exc:
            return {"ok": False, "error": str(exc)}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(time.time())
    try:
        response = await http.post(
            webhook.url,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OpsPilot-Actions/1",
                "X-OpsPilot-Timestamp": str(timestamp),
                "X-OpsPilot-Signature": sign(webhook.secret, timestamp, raw),
            },
            timeout=settings.action_webhook_timeout_seconds,
        )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "error": f"No answer within {settings.action_webhook_timeout_seconds:g}s",
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Unreachable ({type(exc).__name__})"}
    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "body": response.text[:RESULT_BODY_CHARS],
    }
