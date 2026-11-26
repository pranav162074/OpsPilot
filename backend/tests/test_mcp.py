"""The MCP server, tested the way a client uses it: a real MCP client speaking Streamable
HTTP to /mcp, in-process. Nothing reaches a network or a paid model."""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent

from opspilot.ingestion.service import Outcome
from opspilot.mcp_server import server as mcp_server_module
from tests.conftest import auth, upload
from tests.fakes import ScriptedChatModel, answer_citing, call_tool, reply, reply_using

RUNBOOK = """# Payments API

## Connection pool exhaustion

Requests hang and then fail with ERR_POOL_TIMEOUT. Look for long transactions in
pg_stat_activity, and do not raise the pool size during the incident.

## Disk full on db hosts

Clear old WAL archives with the archive-cleanup job, then check replication lag.
"""


@asynccontextmanager
async def connect(app: FastAPI, key: str | None) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=headers
    )
    async with http, Client(streamable_http_client("http://test/mcp", http_client=http)) as mcp:
        yield mcp


def data(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        # Non-object results are wrapped as {"result": ...}
        return content.get("result", content) if set(content) == {"result"} else content
    first = result.content[0]
    assert isinstance(first, TextContent)
    return json.loads(first.text)


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text


@pytest.fixture
async def workspace(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    ingest: Callable[..., Awaitable[Outcome]],
) -> tuple[str, str]:
    """A workspace with the payments runbook indexed: (admin key, document id)."""
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK, "payments.md", source_type="runbook")).json()
    assert await ingest(doc["id"]) == Outcome.READY
    return key, doc["id"]


async def new_key(client: AsyncClient, admin_key: str, role: str) -> str:
    res = await client.post(
        "/v1/api-keys", json={"name": role, "role": role}, headers=auth(admin_key)
    )
    assert res.status_code == 201, res.text
    return str(res.json()["key"])


async def fire_alert(client: AsyncClient, key: str, **fields: Any) -> str:
    body = {"title": "Payments API p99 latency high", "severity": "medium", **fields}
    res = await client.post("/v1/alerts", json=body, headers=auth(key))
    assert res.status_code == 202, res.text
    return str(res.json()["results"][0]["incident_id"])


async def test_rejects_connections_without_a_valid_key(app: FastAPI) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as http:
        missing = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        wrong = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer ops_notarealkey"},
        )
    for res in (missing, wrong):
        assert res.status_code == 401
        assert res.headers["www-authenticate"].startswith("Bearer")
        assert res.json()["error"]["code"] == "unauthorized"


async def test_lists_the_tools_with_safety_hints_and_the_triage_prompt(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    async with connect(app, key) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        prompts = [p.name for p in (await mcp.list_prompts()).prompts]

    assert set(tools) == {
        "search_docs",
        "list_incidents",
        "get_incident",
        "acknowledge_incident",
        "resolve_incident",
        "investigate",
        "investigate_incident",
        "get_investigation",
        "list_pending_actions",
        "query_metrics",
        "list_metric_names",
        "search_logs",
        "recent_changes",
    }
    for name in (
        "search_docs",
        "list_incidents",
        "get_incident",
        "get_investigation",
        "list_pending_actions",
        "query_metrics",
        "search_logs",
        "recent_changes",
    ):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.read_only_hint is True, name
    acknowledge = tools["acknowledge_incident"].annotations
    assert acknowledge is not None and acknowledge.read_only_hint is False
    # The model chooses by the schema: limits and allowed values are in it
    search_schema = tools["search_docs"].input_schema
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["limit"]["maximum"] == 20
    assert prompts == ["triage_incident"]


async def test_search_returns_cited_passages(app: FastAPI, workspace: tuple[str, str]) -> None:
    key, document_id = workspace
    async with connect(app, key) as mcp:
        result = data(await mcp.call_tool("search_docs", {"query": "ERR_POOL_TIMEOUT", "limit": 3}))

    assert result["keyword_only_fallback"] is False
    top = result["results"][0]
    assert top["document"] == "payments"
    assert top["heading"] == "Payments API > Connection pool exhaustion"
    assert "pg_stat_activity" in top["content"]
    assert top["source_type"] == "runbook"


async def test_invalid_arguments_come_back_as_tool_errors(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    async with connect(app, key) as mcp:
        too_many = await mcp.call_tool("search_docs", {"query": "pool", "limit": 500})
        missing = await mcp.call_tool("get_incident", {"incident_id": str(uuid.uuid4())})

    assert too_many.is_error
    assert "Incident not found" in error_text(missing)


async def test_incident_workflow_from_an_assistant(
    app: FastAPI, client: AsyncClient, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    incident_id = await fire_alert(
        client, key, labels={"service": "payments-api", "region": "ap-south-1"}
    )
    async with connect(app, key) as mcp:
        listed = data(await mcp.call_tool("list_incidents", {"status": "open"}))
        detail = data(await mcp.call_tool("get_incident", {"incident_id": incident_id}))
        acked = data(await mcp.call_tool("acknowledge_incident", {"incident_id": incident_id}))
        again = await mcp.call_tool("acknowledge_incident", {"incident_id": incident_id})
        resolved = data(await mcp.call_tool("resolve_incident", {"incident_id": incident_id}))
        open_now = data(await mcp.call_tool("list_incidents", {"status": "open"}))

    assert [i["id"] for i in listed["incidents"]] == [incident_id]
    assert detail["labels"] == {"service": "payments-api", "region": "ap-south-1"}
    assert [event["type"] for event in detail["timeline"]] == ["opened"]
    assert acked["status"] == "acknowledged"
    assert "can't be marked acknowledged" in error_text(again)
    assert resolved["status"] == "resolved"
    assert open_now["incidents"] == []


async def test_roles_and_workspaces_apply_to_tools(
    app: FastAPI,
    client: AsyncClient,
    workspace: tuple[str, str],
    create_workspace: Callable[..., Any],
) -> None:
    key, _ = workspace
    incident_id = await fire_alert(client, key)
    viewer = await new_key(client, key, "viewer")
    _, outsider = await create_workspace("Other Co")

    async with connect(app, viewer) as mcp:
        # Viewers can look...
        assert data(await mcp.call_tool("get_incident", {"incident_id": incident_id}))
        # ...but not change anything
        denied = await mcp.call_tool("resolve_incident", {"incident_id": incident_id})
    assert "role isn't allowed" in error_text(denied)

    async with connect(app, outsider) as mcp:
        hidden = await mcp.call_tool("get_incident", {"incident_id": incident_id})
        found = data(await mcp.call_tool("search_docs", {"query": "ERR_POOL_TIMEOUT"}))
    assert "Incident not found" in error_text(hidden)
    assert found["results"] == []


async def test_a_revoked_key_stops_working_at_once(
    app: FastAPI, client: AsyncClient, workspace: tuple[str, str]
) -> None:
    admin, _ = workspace
    res = await client.post(
        "/v1/api-keys", json={"name": "assistant", "role": "responder"}, headers=auth(admin)
    )
    key, key_id = res.json()["key"], res.json()["id"]
    async with connect(app, key) as mcp:
        assert data(await mcp.call_tool("list_incidents", {}))
        await client.delete(f"/v1/api-keys/{key_id}", headers=auth(admin))
        # The very next request is turned away at the door, before any tool runs
        with pytest.raises(MCPError):
            await mcp.call_tool("list_incidents", {})


async def test_tool_calls_share_the_keys_rate_limit(
    make_app: Callable[..., FastAPI],
    client: AsyncClient,
    create_workspace: Callable[..., Any],
) -> None:
    _, key = await create_workspace()
    limited = make_app(api_rate_limit_per_minute=2)
    async with limited.router.lifespan_context(limited), connect(limited, key) as mcp:
        assert not (await mcp.call_tool("list_incidents", {})).is_error
        assert not (await mcp.call_tool("list_incidents", {})).is_error
        third = await mcp.call_tool("list_incidents", {})
    assert "Rate limited" in error_text(third)


async def test_investigate_runs_the_agent_and_returns_citations(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, document_id = workspace
    app.state.llm = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="ERR_POOL_TIMEOUT"),
            answer_citing("Look for long transactions; don't raise the pool size."),
            reply("SUPPORTED"),
        ]
    )
    async with connect(app, key) as mcp:
        result = data(
            await mcp.call_tool(
                "investigate", {"question": "Checkout fails with ERR_POOL_TIMEOUT. What now?"}
            )
        )
        fetched = data(
            await mcp.call_tool(
                "get_investigation", {"investigation_id": result["id"], "include_trace": True}
            )
        )

    assert result["status"] == "completed"
    assert result["answer"].startswith("Look for long transactions")
    assert result["citations"][0]["document"] == "payments"
    assert fetched["id"] == result["id"]
    assert fetched["usage"]["llm_calls"] == 3
    assert any(step.get("name") == "search_knowledge_base" for step in fetched["trace"])


async def test_plan_investigations_and_their_proposed_actions_over_mcp(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    actions = [
        {
            "title": "Kill the long transaction",
            "command": "SELECT pg_terminate_backend(42)",
            "risk": "high",
            "rationale": "Holding the pool",
        }
    ]
    app.state.llm = ScriptedChatModel(
        script=[
            reply('{"steps": [{"tool": "search_knowledge_base", "args": {"query": "pool"}}]}'),
            reply(
                '{"hypotheses": [{"statement": "Long transactions", "likelihood": "high",'
                ' "evidence": [], "check": null}]}'
            ),
            reply('{"verdicts": [{"hypothesis": 1, "verdict": "supported", "reason": "runbook"}]}'),
            reply_using(
                lambda ids: (
                    f"Long transactions [c:{ids[0]}].\n```actions\n{json.dumps(actions)}\n```"
                )
            ),
            reply("SUPPORTED"),
        ]
    )
    async with connect(app, key) as mcp:
        result = data(
            await mcp.call_tool(
                "investigate", {"question": "ERR_POOL_TIMEOUT. Why?", "strategy": "plan"}
            )
        )
        pending = data(await mcp.call_tool("list_pending_actions", {}))

    assert result["strategy"] == "plan"
    assert result["hypotheses"] == [
        {"statement": "Long transactions", "verdict": "supported", "reason": "runbook"}
    ]
    assert [(a["title"], a["risk"]) for a in pending] == [("Kill the long transaction", "high")]


async def test_investigate_without_a_model_explains_why(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    app.state.llm = None
    async with connect(app, key) as mcp:
        result = await mcp.call_tool("investigate", {"question": "Why is checkout slow?"})
    assert "GEMINI_API_KEY" in error_text(result)


async def test_investigate_incident_queues_a_background_run(
    app: FastAPI,
    client: AsyncClient,
    workspace: tuple[str, str],
    queued_investigations: list[uuid.UUID],
) -> None:
    key, _ = workspace
    incident_id = await fire_alert(client, key)
    app.state.llm = ScriptedChatModel(script=[])
    async with connect(app, key) as mcp:
        started = data(await mcp.call_tool("investigate_incident", {"incident_id": incident_id}))
        status = data(
            await mcp.call_tool(
                "get_investigation", {"investigation_id": started["investigation_id"]}
            )
        )

    assert started["incident_id"] == incident_id
    assert started["status"] == "running"
    assert queued_investigations == [uuid.UUID(started["investigation_id"])]
    assert status["status"] == "running"


async def test_long_tool_calls_report_progress(
    app: FastAPI, workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow investigation sends progress notifications, so clients don't time it out."""
    key, _ = workspace
    monkeypatch.setattr(mcp_server_module, "HEARTBEAT_SECONDS", 0.05)
    slow_steps = [
        call_tool("search_knowledge_base", query="pool"),
        answer_citing("Look for long transactions."),
        reply("SUPPORTED"),
    ]

    class SlowModel(ScriptedChatModel):
        async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
            import asyncio

            await asyncio.sleep(0.1)
            return await super()._agenerate(messages, *args, **kwargs)

    app.state.llm = SlowModel(script=slow_steps)
    updates: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        updates.append((progress, message))

    async with connect(app, key) as mcp:
        result = await mcp.call_tool(
            "investigate", {"question": "Pool timeouts?"}, progress_callback=on_progress
        )

    assert data(result)["status"] == "completed"
    assert updates, "expected progress notifications"
    assert all(message and message.startswith("Investigating") for _, message in updates)
    assert [value for value, _ in updates] == sorted(value for value, _ in updates)


async def test_the_triage_prompt_names_the_incident(
    app: FastAPI, workspace: tuple[str, str]
) -> None:
    key, _ = workspace
    async with connect(app, key) as mcp:
        prompt = await mcp.get_prompt("triage_incident", {"incident_id": "abc-123"})
    content = prompt.messages[0].content
    assert isinstance(content, TextContent)
    assert "incident abc-123" in content.text
    assert "Don't acknowledge or resolve" in content.text


async def test_mcp_can_be_switched_off(make_app: Callable[..., FastAPI]) -> None:
    disabled = make_app(mcp_enabled=False)
    async with (
        disabled.router.lifespan_context(disabled),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=disabled), base_url="http://test"
        ) as http,
    ):
        res = await http.post("/mcp", json={})
    assert res.status_code == 404
