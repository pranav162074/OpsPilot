"""Drafting a postmortem from what OpsPilot already knows about an incident: the alert, the
timeline, and what the investigation found. The draft is for a person to finish (unknowns are
marked TODO), and it only becomes a searchable postmortem document when they publish it."""

import re
import time
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.agent.graph import CITATION, text_of
from opspilot.agent.prompts import POSTMORTEM_PROMPT
from opspilot.models import Incident, IncidentEvent, Investigation, InvestigationAction

FENCE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "unknown"


async def postmortem_material(session: AsyncSession, incident: Incident) -> dict[str, str]:
    """Everything the model is shown, as text, and the citations it may keep."""
    events = (
        await session.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident.id)
            .order_by(IncidentEvent.seq)
        )
    ).all()
    labels = ", ".join(f"{k}={v}" for k, v in sorted(incident.labels.items())) or "none"
    about = (
        f"Title: {incident.title}\nSeverity: {incident.severity}\nStatus: {incident.status}\n"
        f"Source: {incident.source}\nLabels: {labels}\nAlerts received: {incident.alert_count}\n"
        f"First alert: {_when(incident.first_alert_at)}\n"
        f"Resolved: {_when(incident.resolved_at)}\n"
        f"Description: {incident.description or '(none)'}"
    )
    timeline = "\n".join(
        f"- {_when(e.created_at)} {e.type}"
        + (
            f" ({', '.join(f'{k}={v}' for k, v in e.data.items() if k != 'investigation_id')})"
            if any(k != "investigation_id" for k in e.data)
            else ""
        )
        for e in events
    )
    findings, sources = "(no investigation)", "(none)"
    if incident.investigation_id:
        investigation = await session.get(Investigation, incident.investigation_id)
        if investigation is not None and investigation.answer:
            findings = investigation.answer
            if investigation.hypotheses:
                findings += "\n\nHypotheses checked:\n" + "\n".join(
                    f"- {h.get('statement')}: {h.get('verdict') or 'unchecked'}"
                    + (f" ({h['reason']})" if h.get("reason") else "")
                    for h in investigation.hypotheses
                )
            actions = (
                await session.scalars(
                    select(InvestigationAction)
                    .where(InvestigationAction.investigation_id == investigation.id)
                    .order_by(InvestigationAction.position)
                )
            ).all()
            if actions:
                findings += "\n\nActions proposed:\n" + "\n".join(
                    f"- {a.title} ({a.status}"
                    + (f" by {a.decided_by}" if a.decided_by else "")
                    + ")"
                    for a in actions
                )
            sources = (
                "\n".join(
                    f"[c:{c['chunk_id']}] {c['document_title']}"
                    + (f" > {c['heading']}" if c.get("heading") else "")
                    for c in investigation.citations
                )
                or "(none)"
            )
    return {
        "incident": about,
        "timeline": timeline or "(empty)",
        "investigation": findings,
        "sources": sources,
    }


def allowed_citations(sources_text: str) -> set[uuid.UUID]:
    return {uuid.UUID(m.group(1)) for m in CITATION.finditer(sources_text)}


async def draft_postmortem(
    llm: BaseChatModel, session: AsyncSession, incident: Incident
) -> tuple[str, dict[str, Any]]:
    """Returns the draft and usage for the call. Citations of anything the investigation
    didn't cite are removed."""
    material = await postmortem_material(session, incident)
    started = time.perf_counter()
    response = await llm.ainvoke([HumanMessage(POSTMORTEM_PROMPT.format(**material))])
    usage = getattr(response, "usage_metadata", None) or {}
    text = text_of(response).strip()
    if match := FENCE.match(text):
        text = match.group(1).strip()
    allowed = allowed_citations(material["sources"])
    text = CITATION.sub(lambda m: m.group(0) if uuid.UUID(m.group(1)) in allowed else "", text)
    return text, {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def with_footnotes(draft: str, citations: list[dict[str, Any]]) -> str:
    """For publishing: [c:<id>] markers become numbered footnotes naming the source, since a
    reader of the document can't look ids up."""
    by_id = {c["chunk_id"]: c for c in citations}
    numbers: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        chunk_id = match.group(1).lower()
        if chunk_id not in by_id:
            return ""
        number = numbers.setdefault(chunk_id, len(numbers) + 1)
        return f"[^{number}]"

    body = CITATION.sub(replace, draft).rstrip()
    if not numbers:
        return body + "\n"
    notes = []
    for chunk_id, number in numbers.items():
        c = by_id[chunk_id]
        where = c["document_title"] + (f" > {c['heading']}" if c.get("heading") else "")
        kind = c.get("kind", "document")
        notes.append(f"[^{number}]: {where}" + ("" if kind == "document" else f" ({kind})"))
    return body + "\n\n" + "\n".join(notes) + "\n"
