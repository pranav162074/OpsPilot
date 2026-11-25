"""OpsPilot as an MCP server: its search, incidents and investigation agent as tools that
Claude, Cursor or any MCP client can call while helping an engineer on call.

Tools return compact JSON: what a model needs to reason and cite, without the API's
bookkeeping fields. Each tool's description says what it's for and when to use it, since
that's all the model has to choose by.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from opspilot import __version__
from opspilot.mcp_server.api import OpsPilotApi
from opspilot.models import SourceType

ApiFactory = Callable[[Context], OpsPilotApi]


INSTRUCTIONS = """\
OpsPilot is this team's on-call assistant. It holds their runbooks, postmortems and
architecture notes, the incidents raised by their monitoring, and an agent that investigates
problems with citations.

Typical flow while helping someone on call:
1. list_incidents / get_incident to see what's firing and its timeline.
2. search_docs for the runbook or a past postmortem about the symptom (exact error codes and
   hostnames work well as queries, so do plain descriptions).
   query_metrics, search_logs and recent_changes show what is actually happening and what
   changed, when the workspace has its Prometheus, Loki and deploys connected.
3. investigate for a cited, reviewed answer when the fix isn't obvious from one passage.
4. acknowledge_incident when someone takes it, resolve_incident when it's fixed. These change
   shared state the whole team sees: only do them when the user asks.
Always cite document titles and headings from the results; never invent runbook steps."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CHANGES_STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

# How often a long-running tool tells the client it's still alive
HEARTBEAT_SECONDS = 5.0


async def with_heartbeat[T](ctx: Context, work: Awaitable[T], message: str) -> T:
    """Awaits `work`, sending progress notifications meanwhile, so clients that time out
    silent tools (an investigation can take a minute) know it's still running."""
    task = asyncio.ensure_future(work)
    elapsed = 0.0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if done:
                return task.result()
            elapsed += HEARTBEAT_SECONDS
            await ctx.report_progress(elapsed, None, f"{message} ({int(elapsed)}s)")
    finally:
        if not task.done():
            task.cancel()


def _incident_brief(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": incident["id"],
        "title": incident["title"],
        "severity": incident["severity"],
        "status": incident["status"],
        "source": incident["source"],
        "alert_count": incident["alert_count"],
        "first_alert_at": incident["first_alert_at"],
        "last_alert_at": incident["last_alert_at"],
        "investigation_id": incident["investigation_id"],
    }


def _investigation_brief(row: dict[str, Any], include_trace: bool = False) -> dict[str, Any]:
    brief = {
        "id": row["id"],
        "question": row["question"],
        "status": row["status"],
        "answer": row["answer"],
        "citations": [
            {
                "document": c["document_title"],
                "heading": c["heading"],
                "chunk_id": c["chunk_id"],
                "kind": c.get("kind", "document"),
            }
            for c in row["citations"]
        ],
        "error": row["error"],
    }
    if include_trace:
        brief["trace"] = row.get("trace", [])
        brief["usage"] = row["usage"]
    return brief


def build_server(api_for: ApiFactory, *, agent_timeout_seconds: float = 90.0) -> MCPServer:
    """`api_for` gives each tool call a client acting with the caller's API key."""
    server = MCPServer(
        name="opspilot",
        title="OpsPilot",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    async def search_docs(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1000,
                description="What to look for: a symptom, an error code, a service or host name",
            ),
        ],
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=20, description="How many passages")] = 5,
        source_types: Annotated[
            list[SourceType] | None,
            Field(description="Only these kinds of document, e.g. ['runbook']"),
        ] = None,
    ) -> dict[str, Any]:
        """Search the team's runbooks, postmortems and architecture notes. Hybrid search:
        matches meaning and exact tokens like error codes. Returns passages with their
        document title and heading, best first."""
        body: dict[str, Any] = {"query": query, "limit": limit}
        if source_types:
            body["source_types"] = source_types
        data = await api_for(ctx).call("POST", "/v1/search", json_body=body)
        return {
            "query": data["query"],
            # True when semantic search was down and only keyword matching ran
            "keyword_only_fallback": data["degraded"],
            "results": [
                {
                    "document": r["document_title"],
                    "source_type": r["source_type"],
                    "heading": r["heading"],
                    "content": r["content"],
                    "chunk_id": r["chunk_id"],
                    "score": r["score"],
                }
                for r in data["results"]
            ],
        }

    @server.tool(annotations=READ_ONLY)
    async def list_incidents(
        ctx: Context,
        status: Annotated[
            Literal["open", "acknowledged", "resolved"] | None,
            Field(description="Only incidents in this state; omit for all"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """List recent incidents raised by monitoring alerts, newest first. Use status='open'
        to see what's currently firing and nobody has taken yet."""
        rows = await api_for(ctx).call(
            "GET", "/v1/incidents", params={"status": status, "limit": limit}
        )
        return {"incidents": [_incident_brief(row) for row in rows]}

    @server.tool(annotations=READ_ONLY)
    async def get_incident(incident_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
        """One incident in full: description, alert labels (service, host, region...), the
        timeline (alerts, acknowledgements, re-fires) and its investigation's answer if any."""
        row = await api_for(ctx).call("GET", f"/v1/incidents/{incident_id}")
        investigation = row["investigation"]
        return {
            **_incident_brief(row),
            "description": row["description"],
            "labels": row["labels"],
            "acknowledged_at": row["acknowledged_at"],
            "resolved_at": row["resolved_at"],
            "timeline": row["timeline"],
            "investigation": (
                {
                    "id": investigation["id"],
                    "status": investigation["status"],
                    "answer": investigation["answer"],
                    "citations": [
                        {"document": c["document_title"], "heading": c["heading"]}
                        for c in investigation["citations"]
                    ],
                    "error": investigation["error"],
                }
                if investigation
                else None
            ),
        }

    @server.tool(annotations=CHANGES_STATE)
    async def acknowledge_incident(incident_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
        """Mark an open incident as acknowledged: someone is on it. The whole team sees this,
        so only do it when the user says they're taking the incident. Needs a responder key."""
        row = await api_for(ctx).call("POST", f"/v1/incidents/{incident_id}/acknowledge")
        return _incident_brief(row)

    @server.tool(annotations=CHANGES_STATE)
    async def resolve_incident(incident_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
        """Mark an incident resolved. Only when the user confirms it's fixed: a resolved
        incident that fires again opens a new incident. Needs a responder key."""
        row = await api_for(ctx).call("POST", f"/v1/incidents/{incident_id}/resolve")
        return _incident_brief(row)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    async def investigate(
        question: Annotated[
            str,
            Field(
                min_length=3,
                max_length=2000,
                description="The problem, with the symptoms and any error messages verbatim",
            ),
        ],
        ctx: Context,
        source_types: Annotated[
            list[SourceType] | None, Field(description="Only search these kinds of document")
        ] = None,
    ) -> dict[str, Any]:
        """Run OpsPilot's investigation agent: it searches the documents in several steps,
        writes an answer citing the passages it used, and has the answer checked against
        those sources. Slower than search_docs (up to a minute and a half); use it when the
        answer needs several documents put together. Needs a responder key."""
        body: dict[str, Any] = {"question": question}
        if source_types:
            body["source_types"] = source_types
        call = api_for(ctx).call(
            "POST",
            "/v1/investigations",
            json_body=body,
            # The API stops the agent after its own timeout; give it a margin to answer
            timeout_seconds=agent_timeout_seconds + 15,
        )
        row = await with_heartbeat(ctx, call, "Investigating")
        return _investigation_brief(row)

    @server.tool(annotations=CHANGES_STATE)
    async def investigate_incident(incident_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
        """Start a fresh background investigation of an incident (after new runbooks were
        added, say). Returns at once; check get_investigation with the returned id for the
        answer. Needs the OpsPilot worker running. Needs a responder key."""
        row = await api_for(ctx).call("POST", f"/v1/incidents/{incident_id}/investigate")
        investigation = row["investigation"]
        return {
            "incident_id": row["id"],
            "investigation_id": investigation["id"] if investigation else None,
            "status": investigation["status"] if investigation else None,
        }

    @server.tool(annotations=READ_ONLY)
    async def get_investigation(
        investigation_id: uuid.UUID,
        ctx: Context,
        include_trace: Annotated[
            bool,
            Field(description="Also return every model and tool call the agent made"),
        ] = False,
    ) -> dict[str, Any]:
        """An investigation's status, answer and citations. Status 'running' means it isn't
        finished: check again shortly."""
        row = await api_for(ctx).call("GET", f"/v1/investigations/{investigation_id}")
        return _investigation_brief(row, include_trace)

    @server.tool(annotations=READ_ONLY)
    async def query_metrics(
        promql: Annotated[str, Field(min_length=1, max_length=500, description="PromQL")],
        ctx: Context,
        minutes: Annotated[int, Field(ge=1, le=360, description="How far back")] = 60,
    ) -> dict[str, Any]:
        """Query the workspace's Prometheus over a recent window. Returns each series' last,
        min, max and average plus a few samples over time, the series that moved most first.
        Use list_metric_names first if unsure of a metric's name."""
        return dict(
            await api_for(ctx).call(
                "POST", "/v1/telemetry/metrics", json_body={"query": promql, "minutes": minutes}
            )
        )

    @server.tool(annotations=READ_ONLY)
    async def list_metric_names(
        ctx: Context,
        contains: Annotated[str | None, Field(max_length=100)] = None,
    ) -> dict[str, Any]:
        """Metric names known to the workspace's Prometheus, optionally filtered by a substring."""
        names = await api_for(ctx).call(
            "GET", "/v1/telemetry/metric-names", params={"contains": contains}
        )
        return {"metric_names": names}

    @server.tool(annotations=READ_ONLY)
    async def search_logs(
        service: Annotated[str, Field(min_length=1, max_length=100)],
        ctx: Context,
        contains: Annotated[
            str | None, Field(max_length=200, description="Only lines containing this text")
        ] = None,
        minutes: Annotated[int, Field(ge=1, le=360)] = 30,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """A service's most recent log lines from the workspace's Loki, newest first."""
        data = await api_for(ctx).call(
            "POST",
            "/v1/telemetry/logs",
            json_body={
                "service": service,
                "contains": contains,
                "minutes": minutes,
                "limit": limit,
            },
        )
        return {"query": data["query"], "lines": [f"{x['at']} {x['line']}" for x in data["lines"]]}

    @server.tool(annotations=READ_ONLY)
    async def recent_changes(
        ctx: Context,
        hours: Annotated[int, Field(ge=1, le=336)] = 24,
        service: Annotated[str | None, Field(max_length=100)] = None,
    ) -> dict[str, Any]:
        """Recent deploys (reported by CI) and commits (when GitHub is connected), newest first.
        Problems that start right after a change are often caused by it."""
        return dict(
            await api_for(ctx).call(
                "GET", "/v1/telemetry/changes", params={"hours": hours, "service": service}
            )
        )

    @server.prompt(title="Triage an incident")
    def triage_incident(incident_id: str) -> str:
        """Walk through an incident the way an experienced on-call engineer would."""
        return (
            f"Help me triage OpsPilot incident {incident_id}.\n"
            "1. Call get_incident and summarise what is firing, since when, how often, and on "
            "which service/host (from the labels).\n"
            "2. Call search_docs with the alert's symptom and any error codes to find the "
            "runbook and similar past postmortems. If the incident already has an "
            "investigation answer, start from it.\n"
            "3. Give me the likely cause and the concrete next steps from the runbook, citing "
            "each document title and heading you rely on. Say plainly when the documents "
            "don't cover something.\n"
            "Don't acknowledge or resolve the incident unless I ask you to."
        )

    return server
