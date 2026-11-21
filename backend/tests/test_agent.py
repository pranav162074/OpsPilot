import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage

from opspilot.agent.graph import cited_ids, text_of
from opspilot.agent.tools import Retrieved, build_tools
from opspilot.config import Settings
from opspilot.embeddings import HashingEmbedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.ingestion.service import Outcome
from tests.conftest import auth, upload
from tests.fakes import ScriptedChatModel, answer_citing, call_tool, fail, reply

RUNBOOK = """# Payments API

## Connection pool exhaustion

Requests hang and then fail with ERR_POOL_TIMEOUT. Look for long transactions in
pg_stat_activity, and do not raise the pool size during the incident.

## Gateway timeouts

Payments move to pending_confirmation; the reconciliation job resolves them.
"""


async def setup_runbook(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    ingest: Callable[..., Awaitable[Outcome]],
) -> tuple[str, str]:
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK, "payments.md", source_type="runbook")).json()
    assert await ingest(doc["id"]) == Outcome.READY
    return key, doc["id"]


async def ask(
    client: AsyncClient,
    key: str,
    question: str = "Payments are timing out with ERR_POOL_TIMEOUT. What do I do?",
) -> Any:
    return await client.post("/v1/investigations", json={"question": question}, headers=auth(key))


async def test_answers_with_citations_and_a_full_trace(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, document_id = await setup_runbook(client, create_workspace, ingest)
    model = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="ERR_POOL_TIMEOUT connection pool"),
            answer_citing(
                "Check pg_stat_activity for long transactions; don't raise the pool size."
            ),
            reply("SUPPORTED"),
        ]
    )
    app.state.llm = model

    res = await ask(client, key)

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "completed"
    assert "pg_stat_activity" in body["answer"]
    assert [c["document_id"] for c in body["citations"]] == [document_id]
    assert body["citations"][0]["heading"] == "Payments API > Connection pool exhaustion"
    assert body["invalid_citations"] == 0
    assert body["usage"] == {
        "llm_calls": 3,
        "tool_calls": 1,
        "input_tokens": 300,
        "output_tokens": 60,
        "latency_ms": body["usage"]["latency_ms"],
    }
    assert [(e["type"], e.get("node") or e.get("name")) for e in body["trace"]] == [
        ("llm", "agent"),
        ("tool", "search_knowledge_base"),
        ("llm", "agent"),
        ("llm", "review"),
    ]
    # The model saw the system prompt first, and the tool's passages before answering
    first_call = model.calls[0]
    assert "OpsPilot" in text_of(first_call[0])
    assert "ERR_POOL_TIMEOUT" in text_of(model.calls[1][-1])

    stored = (await client.get(f"/v1/investigations/{body['id']}", headers=auth(key))).json()
    listed = (await client.get("/v1/investigations", headers=auth(key))).json()
    assert stored["trace"] == body["trace"]
    assert [i["id"] for i in listed] == [body["id"]]
    assert "trace" not in listed[0]


async def test_removes_citations_to_passages_it_never_retrieved(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)
    made_up = uuid.uuid4()
    app.state.llm = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="pool"),
            reply(f"Restart the database [c:{made_up}]."),
            reply("SUPPORTED"),
        ]
    )

    body = (await ask(client, key)).json()

    assert body["invalid_citations"] == 1
    assert body["citations"] == []
    assert str(made_up) not in body["answer"]


async def test_a_rejected_draft_is_revised_once(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)
    model = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="pool"),
            answer_citing("Double the pool size to 40."),
            reply("UNSUPPORTED: the source says not to raise the pool size"),
            answer_citing("Don't raise the pool size; look for long transactions."),
        ]
    )
    app.state.llm = model

    body = (await ask(client, key)).json()

    assert body["answer"].startswith("Don't raise the pool size")
    assert body["usage"]["llm_calls"] == 4
    # The reviewer's notes went back to the agent
    feedback = model.calls[3][-1]
    assert isinstance(feedback, HumanMessage)
    assert "the source says not to raise the pool size" in text_of(feedback)


async def test_stops_searching_at_the_step_limit(
    make_app: Callable[..., FastAPI],
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)

    async with started(make_app(agent_max_steps=2)) as (other_app, http):
        other_app.state.llm = ScriptedChatModel(
            script=[
                call_tool("search_knowledge_base", query="pool"),
                call_tool("search_knowledge_base", query="timeouts"),
                answer_citing("Here is what the runbook says."),
                reply("SUPPORTED"),
            ]
        )
        body = (await ask(http, key)).json()

    nodes = [e.get("node") for e in body["trace"] if e["type"] == "llm"]
    assert nodes == ["agent", "agent", "force_answer", "review"]
    assert body["usage"]["tool_calls"] == 1  # the second search was never run
    assert body["citations"]


async def test_unknown_tools_are_reported_back_to_the_model(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)
    model = ScriptedChatModel(
        script=[
            call_tool("drop_database", confirm=True),
            reply("I can't do that. The documents don't cover it."),
            reply("SUPPORTED"),
        ]
    )
    app.state.llm = model

    body = (await ask(client, key)).json()

    tool_event = next(e for e in body["trace"] if e["type"] == "tool")
    assert tool_event == {**tool_event, "name": "drop_database", "ok": False}
    assert "Unknown tool" in text_of(model.calls[1][-1])
    assert body["status"] == "completed"


async def test_tools_only_see_their_own_workspace(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    await setup_runbook(client, create_workspace, ingest)
    other_workspace, _ = await create_workspace("Globex")
    retrieved = Retrieved()
    search, read = build_tools(
        sessionmaker=app.state.sessionmaker,
        settings=settings,
        embedder=HashingEmbedder(),
        query_cache=QueryEmbeddingCache(app.state.redis, 60),
        workspace_id=uuid.UUID(other_workspace["id"]),
        retrieved=retrieved,
    )
    async with app.state.sessionmaker() as session:
        from sqlalchemy import select

        from opspilot.models import Chunk

        acme_chunk = (await session.scalars(select(Chunk.id))).first()

    assert (
        await search.ainvoke({"query": "ERR_POOL_TIMEOUT"})
        == "No matching passages. Try different words."
    )
    assert await read.ainvoke({"chunk_id": str(acme_chunk)}) == "No such passage."
    assert retrieved.sources == {}


async def test_permissions_and_configuration(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)
    viewer = (
        await client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=auth(key))
    ).json()["key"]

    # No model configured (no GEMINI_API_KEY in tests)
    unavailable = await ask(client, key)
    app.state.llm = ScriptedChatModel(script=[reply("x")])
    forbidden = await ask(client, viewer)
    too_short = await ask(client, key, question="?")

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "agent_unavailable"
    assert forbidden.status_code == 403
    assert too_short.status_code == 422
    assert (await client.get("/v1/investigations", headers=auth(viewer))).status_code == 200


async def test_model_failures_and_timeouts_are_recorded(
    make_app: Callable[..., FastAPI],
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)
    app.state.llm = ScriptedChatModel(script=[fail])

    failed = await ask(client, key)

    async with started(make_app(agent_timeout_seconds=0.2)) as (slow_app, http):
        slow_app.state.llm = ScriptedChatModel(script=[reply("late")], delay=2)
        timed_out = await ask(http, key)

    assert failed.status_code == 502
    assert timed_out.status_code == 504
    listed = (await client.get("/v1/investigations", headers=auth(key))).json()
    assert {(i["status"], i["error"]) for i in listed} == {
        ("failed", "The agent failed: RuntimeError"),
        ("failed", "The investigation took too long and was stopped"),
    }


async def test_investigations_are_rate_limited_per_workspace(
    make_app: Callable[..., FastAPI],
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, _ = await setup_runbook(client, create_workspace, ingest)

    async with started(make_app(investigations_per_hour=1)) as (limited, http):
        limited.state.llm = ScriptedChatModel(
            script=[reply("No documents cover this."), reply("SUPPORTED")]
        )
        first = await ask(http, key)
        second = await ask(http, key)

    assert first.status_code == 201
    assert second.status_code == 429


def test_citation_parsing_and_message_text() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    assert cited_ids(f"x [c:{a}] y [c:{b}] z [c:{a}] [c:not-a-uuid]") == [a, b]
    assert (
        text_of(
            AIMessage(content=[{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}])
        )
        == "Hello"
    )


@asynccontextmanager
async def started(application: FastAPI) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """Runs another app (with different settings) against the same test database."""
    async with application.router.lifespan_context(application):
        application.state.enqueue_ingestion = lambda _: None
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield application, http
