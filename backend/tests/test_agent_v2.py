"""Agent v2: the "plan" strategy, the token budget, resumable runs, human approval of actions,
and postmortem drafts. The model is scripted and telemetry is an in-process fake: nothing
here calls a paid API or the network."""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import AsyncClient
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field
from sqlalchemy import select, update

from opspilot.actions import verify
from opspilot.agent.planner import parse_json, split_actions
from opspilot.agent.prompts import BUDGET_PROMPT
from opspilot.agent.runner import (
    RunOutcome,
    execute_investigation,
    recover_abandoned_investigations,
)
from opspilot.config import Settings
from opspilot.ingestion.service import Outcome
from opspilot.models import Document, Investigation
from tests.conftest import auth, upload
from tests.fakes import (
    USAGE,
    ScriptedChatModel,
    call_tool,
    evidence_ids,
    fail,
    reply,
    reply_using,
)

RUNBOOK = """# Payments API

## Connection pool exhaustion

Requests hang and then fail with ERR_POOL_TIMEOUT. Look for long transactions in
pg_stat_activity, and do not raise the pool size during the incident.

## Rolling back

Roll back with `kubectl rollout undo deployment/payments-api`.
"""

ALERT = {"title": "Payments API error rate above 5%", "severity": "critical"}

PLAN = json.dumps(
    {
        "steps": [
            {"tool": "search_knowledge_base", "args": {"query": "ERR_POOL_TIMEOUT"}, "why": "x"},
            {"tool": "search_knowledge_base", "args": {"query": "roll back"}, "why": "y"},
            {"tool": "no_such_tool", "args": {}, "why": "ignored"},
        ]
    }
)


def hypotheses(check: dict[str, Any] | None = None) -> Callable[[list[str]], str]:
    def make(ids: list[str]) -> str:
        return json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": "A bad deploy",
                        "likelihood": "medium",
                        "evidence": [],
                        "check": {"tool": "search_knowledge_base", "args": {"query": "deploy"}},
                    },
                    {
                        "statement": "Long transactions exhaust the pool",
                        "likelihood": "high",
                        "evidence": [f"c:{ids[0]}"],
                        "check": check,
                    },
                ]
            }
        )

    return make


VERDICTS = json.dumps(
    {
        "verdicts": [
            {"hypothesis": 1, "verdict": "refuted", "reason": "no deploy"},
            {"hypothesis": 2, "verdict": "supported", "reason": "matches the runbook"},
        ]
    }
)


def answer_with_actions(ids: list[str]) -> str:
    actions = [
        {
            "title": "Roll back payments-api",
            "command": "kubectl rollout undo deployment/payments-api",
            "risk": "medium",
            "rationale": "The runbook's rollback",
            "citations": [ids[0], str(uuid.uuid4())],
        },
        {"title": "Raise the pool size", "risk": "extreme"},
    ]
    return (
        f"Long transactions are exhausting the pool [c:{ids[0]}]. A deploy was ruled out.\n\n"
        f"```actions\n{json.dumps(actions)}\n```"
    )


def full_plan_script() -> list[Any]:
    return [
        reply(PLAN),
        reply_using(hypotheses()),
        reply(VERDICTS),
        reply_using(answer_with_actions),
        reply("SUPPORTED"),
    ]


async def setup(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    ingest: Callable[..., Awaitable[Outcome]],
) -> str:
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK, "payments.md", source_type="runbook")).json()
    assert await ingest(doc["id"]) == Outcome.READY
    return key


def run(app: FastAPI, settings: Settings, investigation_id: uuid.UUID, model: Any) -> Any:
    return execute_investigation(
        investigation_id=investigation_id,
        llm=model,
        sessionmaker=app.state.sessionmaker,
        settings=settings,
        embedder=app.state.embedder,
        query_cache=app.state.query_cache,
        telemetry_http=app.state.telemetry_http,
    )


async def queue(client: AsyncClient, key: str, strategy: str = "plan") -> str:
    res = await client.post(
        "/v1/investigations",
        json={"question": "Payments time out. Why?", "background": True, "strategy": strategy},
        headers=auth(key),
    )
    assert res.status_code == 202, res.text
    return str(res.json()["id"])


# ------------------------------------------------------------------------- plan strategy --


async def test_plan_strategy_gathers_hypothesizes_verifies_and_answers(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    model = ScriptedChatModel(script=full_plan_script())
    app.state.llm = model

    res = await client.post(
        "/v1/investigations",
        json={"question": "Payments time out with ERR_POOL_TIMEOUT. Why?", "strategy": "plan"},
        headers=auth(key),
    )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["strategy"] == "plan"
    assert body["status"] == "completed"
    assert body["attempts"] == 1
    assert body["budget_exhausted"] is False
    assert body["answer"].startswith("Long transactions are exhausting the pool [c:")
    assert "```actions" not in body["answer"]
    assert len(body["citations"]) == 1
    # Each stage in order; the plan's two valid steps ran, the unknown tool was dropped
    assert [(e["type"], e.get("node") or e.get("name")) for e in body["trace"]] == [
        ("llm", "plan"),
        ("tool", "search_knowledge_base"),
        ("tool", "search_knowledge_base"),
        ("llm", "hypothesize"),
        ("tool", "search_knowledge_base"),  # the check for hypothesis 1
        ("llm", "verify"),
        ("llm", "answer"),
        ("llm", "review"),
    ]
    assert [e["args"] for e in body["trace"] if e["type"] == "tool"][:2] == [
        {"query": "ERR_POOL_TIMEOUT"},
        {"query": "roll back"},
    ]
    # Supported first, refuted last
    assert [(h["statement"], h["verdict"]) for h in body["hypotheses"]] == [
        ("Long transactions exhaust the pool", "supported"),
        ("A bad deploy", "refuted"),
    ]
    # The answer stage saw the evidence, the check results and the verdicts
    answer_prompt = str(model.calls[3][0].content)
    assert "ERR_POOL_TIMEOUT" in answer_prompt
    assert "→ refuted: no deploy" in answer_prompt

    actions = (
        await client.get(f"/v1/investigations/{body['id']}/actions", headers=auth(key))
    ).json()
    assert [(a["title"], a["risk"], a["status"]) for a in actions] == [
        ("Roll back payments-api", "medium", "proposed"),
        # An unknown risk level is treated as the highest
        ("Raise the pool size", "high", "proposed"),
    ]
    # Only citations of evidence the agent actually saw are kept
    assert actions[0]["citations"] == [body["citations"][0]["chunk_id"]]
    assert actions[0]["command"] == "kubectl rollout undo deployment/payments-api"


async def test_an_unusable_plan_still_searches_the_runbooks(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    app.state.llm = ScriptedChatModel(
        script=[
            reply("I think we should look at the database."),  # not JSON
            reply('{"hypotheses": []}'),
            reply_using(lambda ids: f"Check pg_stat_activity. [c:{ids[0]}]"),
            reply("SUPPORTED"),
        ]
    )

    res = await client.post(
        "/v1/investigations",
        json={"question": "ERR_POOL_TIMEOUT everywhere", "strategy": "plan"},
        headers=auth(key),
    )

    body = res.json()
    assert body["status"] == "completed", body
    tools = [e for e in body["trace"] if e["type"] == "tool"]
    assert [(t["name"], t["args"]) for t in tools] == [
        ("search_knowledge_base", {"query": "ERR_POOL_TIMEOUT everywhere"})
    ]
    # No hypotheses: verify has nothing to ask the model
    assert [e.get("node") for e in body["trace"] if e["type"] == "llm"] == [
        "plan",
        "hypothesize",
        "answer",
        "review",
    ]
    assert body["hypotheses"] == []


class SlowTelemetry:
    """Prometheus and Loki that take a while to answer, counting overlapping requests."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.in_flight = 0
        self.most_at_once = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.most_at_once = max(self.most_at_once, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        kind = "streams" if "/loki/" in request.url.path else "matrix"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": kind, "result": []}}
        )


async def connect_telemetry(app: FastAPI, client: AsyncClient, key: str, fake: Any) -> None:
    app.state.telemetry_http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    for kind, url in (("prometheus", "http://prom.test"), ("loki", "http://loki.test")):
        res = await client.put(f"/v1/integrations/{kind}", json={"url": url}, headers=auth(key))
        assert res.status_code == 200, res.text


TELEMETRY_PLAN = json.dumps(
    {
        "steps": [
            {"tool": "query_metrics", "args": {"promql": "up", "minutes": 30}},
            {"tool": "query_metrics", "args": {"promql": "rate(errors[5m])", "minutes": 30}},
            {"tool": "search_logs", "args": {"service": "payments", "minutes": 30}},
        ]
    }
)


async def test_planned_tools_run_at_the_same_time(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    fake = SlowTelemetry(delay=0.3)
    await connect_telemetry(app, client, key, fake)
    model = ScriptedChatModel(
        script=[
            reply(TELEMETRY_PLAN),
            reply('{"hypotheses": []}'),
            reply("Nothing is wrong that the telemetry shows."),
        ]
    )
    app.state.llm = model
    investigation_id = await queue(client, key)

    started = asyncio.get_running_loop().time()
    outcome = await run(
        app,
        settings.model_copy(update={"agent_reflection": False}),
        queued_investigations[0],
        model,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert outcome == RunOutcome.COMPLETED
    assert fake.most_at_once == 3
    assert elapsed < 0.85  # three 0.3s calls, not one after another
    body = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert [e["name"] for e in body["trace"] if e["type"] == "tool"] == [
        "query_metrics",
        "query_metrics",
        "search_logs",
    ]


async def test_a_slow_tool_is_stopped_and_reported_as_evidence(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    await connect_telemetry(app, client, key, SlowTelemetry(delay=2))
    model = ScriptedChatModel(
        script=[
            reply(TELEMETRY_PLAN),
            reply('{"hypotheses": []}'),
            reply("Prometheus and Loki didn't answer; check them first."),
        ]
    )
    app.state.llm = model
    investigation_id = await queue(client, key)

    outcome = await run(
        app,
        settings.model_copy(update={"agent_reflection": False, "agent_tool_timeout_seconds": 0.1}),
        queued_investigations[0],
        model,
    )

    assert outcome == RunOutcome.COMPLETED
    body = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    tools = [e for e in body["trace"] if e["type"] == "tool"]
    assert [t["ok"] for t in tools] == [False, False, False]
    assert "took longer than 0.1s" in tools[0]["output"]
    assert "took longer than 0.1s" in str(model.calls[1][0].content)


# -------------------------------------------------------------------------- token budget --


async def test_the_plan_strategy_answers_early_when_out_of_tokens(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    # Every scripted call uses 120 tokens: plan + hypothesize = 240, past a budget of 200
    model = ScriptedChatModel(
        script=[
            reply(PLAN),
            reply_using(hypotheses()),
            reply_using(lambda ids: f"Probably long transactions [c:{ids[0]}]; unchecked."),
        ]
    )
    app.state.llm = model
    investigation_id = await queue(client, key)

    outcome = await run(
        app,
        settings.model_copy(update={"agent_token_budget": 200}),
        queued_investigations[0],
        model,
    )

    assert outcome == RunOutcome.COMPLETED
    body = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert body["budget_exhausted"] is True
    # Verify (and its checks) and the review were skipped
    assert [e.get("node") for e in body["trace"] if e["type"] == "llm"] == [
        "plan",
        "hypothesize",
        "answer",
    ]
    assert BUDGET_PROMPT in str(model.calls[-1][0].content)
    assert [h["verdict"] for h in body["hypotheses"]] == [None, None]
    assert body["usage"]["input_tokens"] + body["usage"]["output_tokens"] == 360


async def test_the_react_strategy_stops_searching_when_out_of_tokens(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    model = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="pool"),
            # Out of budget after the first call: the model has to answer, without tools
            reply("Look at pg_stat_activity."),
        ]
    )
    app.state.llm = model
    investigation_id = await queue(client, key, strategy="react")

    outcome = await run(
        app,
        settings.model_copy(update={"agent_token_budget": 100}),
        queued_investigations[0],
        model,
    )

    assert outcome == RunOutcome.COMPLETED
    body = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert body["budget_exhausted"] is True
    assert [e.get("node") for e in body["trace"] if e["type"] == "llm"] == [
        "agent",
        "force_answer",
    ]
    assert BUDGET_PROMPT in str(model.calls[-1][-1].content)


# ----------------------------------------------------------------------- resumable runs --


class Hangs(ScriptedChatModel):
    """Plays its script, then hangs on the next call: the worker gets killed mid-call."""

    hung: asyncio.Event = Field(default_factory=asyncio.Event)

    async def _agenerate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        if len(self.calls) >= len(self.script):
            self.hung.set()
            await asyncio.sleep(3600)
        return await super()._agenerate(messages, *args, **kwargs)


async def test_a_run_whose_worker_died_resumes_from_its_last_stage(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    first = Hangs(script=[reply(PLAN), reply_using(hypotheses()), reply(VERDICTS)])
    app.state.llm = first
    investigation_id = await queue(client, key)
    [queued] = queued_investigations

    worker = asyncio.create_task(run(app, settings, queued, first))
    await asyncio.wait_for(first.hung.wait(), 10)  # in the answer call, after verify's checkpoint
    worker.cancel()  # the process is killed: nothing gets to write "failed"
    with contextlib.suppress(asyncio.CancelledError):
        await worker

    # It died while writing the answer: still running, checkpointed after "verify"
    during = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert during["status"] == "running", during["error"]
    assert during["stage"] == "answer"
    assert during["attempts"] == 1

    # Not yet silent for long enough: left alone
    early = await recover_abandoned_investigations(
        app.state.sessionmaker, settings, datetime.now(UTC)
    )
    assert early.resumed == []
    later = datetime.now(UTC) + timedelta(seconds=settings.agent_timeout_seconds * 4)
    recovery = await recover_abandoned_investigations(app.state.sessionmaker, settings, later)
    assert recovery.resumed == [queued]
    assert recovery.failed == 0

    second = ScriptedChatModel(script=[reply_using(answer_with_actions), reply("SUPPORTED")])
    outcome = await run(app, settings, queued, second)

    assert outcome == RunOutcome.COMPLETED
    # Only the remaining stages were paid for again
    assert len(second.calls) == 2
    # The resumed answer could still cite what the first run retrieved
    assert evidence_ids(second.calls[0])
    body = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert body["status"] == "completed"
    assert body["attempts"] == 2
    assert body["stage"] is None
    assert len(body["citations"]) == 1
    # One trace across both runs, and both runs' tokens
    assert [e.get("node") for e in body["trace"] if e["type"] == "llm"] == [
        "plan",
        "hypothesize",
        "verify",
        "answer",
        "review",
    ]
    assert body["usage"]["llm_calls"] == 5


async def test_a_lost_task_is_queued_again_and_a_dead_one_fails_after_its_attempts(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    app.state.llm = ScriptedChatModel(script=[])
    never_started = await queue(client, key)
    keeps_dying = await queue(client, key)
    later = datetime.now(UTC) + timedelta(seconds=settings.agent_timeout_seconds * 4)
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(Investigation)
            .where(Investigation.id == uuid.UUID(keeps_dying))
            .values(started_at=datetime.now(UTC), attempts=settings.agent_max_attempts)
        )
        await session.commit()

    recovery = await recover_abandoned_investigations(app.state.sessionmaker, settings, later)

    assert recovery.resumed == [uuid.UUID(never_started)]
    assert recovery.failed == 1
    dead = (await client.get(f"/v1/investigations/{keeps_dying}", headers=auth(key))).json()
    assert dead["status"] == "failed"
    assert "ran out of attempts" in dead["error"]
    # Recovering again changes nothing: the released one isn't silent any more
    assert (
        await recover_abandoned_investigations(app.state.sessionmaker, settings, later)
    ).failed == 0


# ------------------------------------------------------------------------------ actions --


async def investigated(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    ingest: Callable[..., Awaitable[Outcome]],
) -> tuple[str, list[dict[str, Any]]]:
    key = await setup(client, create_workspace, ingest)
    app.state.llm = ScriptedChatModel(script=full_plan_script())
    res = await client.post(
        "/v1/investigations",
        json={"question": "Payments time out. Why?", "strategy": "plan"},
        headers=auth(key),
    )
    investigation_id = res.json()["id"]
    actions = (
        await client.get(f"/v1/investigations/{investigation_id}/actions", headers=auth(key))
    ).json()
    return key, actions


async def make_key(client: AsyncClient, admin: str, role: str) -> str:
    res = await client.post("/v1/api-keys", json={"name": role, "role": role}, headers=auth(admin))
    assert res.status_code == 201, res.text
    return str(res.json()["key"])


async def test_actions_need_a_person_to_approve_them(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, (rollback, raise_pool) = await investigated(app, client, create_workspace, ingest)
    viewer = await make_key(client, key, "viewer")
    _, other_workspace = await create_workspace("Globex")

    queue_ = (await client.get("/v1/actions?status=proposed", headers=auth(viewer))).json()
    denied = await client.post(f"/v1/actions/{rollback['id']}/approve", headers=auth(viewer))
    foreign = await client.post(
        f"/v1/actions/{rollback['id']}/approve", headers=auth(other_workspace)
    )
    approved = await client.post(
        f"/v1/actions/{rollback['id']}/approve",
        json={"note": "Confirmed with the payments team"},
        headers=auth(key),
    )
    again = await client.post(f"/v1/actions/{rollback['id']}/approve", headers=auth(key))
    rejected = await client.post(
        f"/v1/actions/{raise_pool['id']}/reject",
        json={"note": "The runbook says not to"},
        headers=auth(key),
    )

    assert {a["id"] for a in queue_} == {raise_pool["id"], rollback["id"]}
    assert denied.status_code == 403
    assert foreign.status_code == 404
    assert approved.status_code == 200, approved.text
    # No actions webhook: approved, for the engineer to carry out by hand
    assert approved.json()["status"] == "approved"
    assert approved.json()["decision_note"] == "Confirmed with the payments team"
    assert approved.json()["decided_by"]
    assert approved.json()["result"] is None
    assert again.status_code == 409
    assert rejected.json()["status"] == "rejected"
    assert (await client.get("/v1/actions?status=proposed", headers=auth(key))).json() == []


async def test_two_approvals_at_once_send_the_action_once(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, (rollback, _) = await investigated(app, client, create_workspace, ingest)

    results = await asyncio.gather(
        *(client.post(f"/v1/actions/{rollback['id']}/approve", headers=auth(key)) for _ in range(5))
    )

    assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]


class Receiver:
    """An actions webhook receiver that checks signatures the way the docs tell users to."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.received: list[dict[str, Any]] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        ok = verify(
            self.secret,
            request.headers["x-opspilot-timestamp"],
            request.content,
            request.headers["x-opspilot-signature"],
        )
        if not ok:
            return httpx.Response(401, text="bad signature")
        self.received.append(json.loads(request.content))
        return httpx.Response(self.status, text="queued" if self.status < 400 else "broken")


async def test_approved_actions_are_sent_signed_to_the_actions_webhook(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key, (rollback, raise_pool) = await investigated(app, client, create_workspace, ingest)
    receiver = Receiver("whsec_test_123")
    app.state.telemetry_http = httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler))

    no_secret = await client.put(
        "/v1/integrations/actions", json={"url": "http://hooks.test/opspilot"}, headers=auth(key)
    )
    saved = await client.put(
        "/v1/integrations/actions",
        json={"url": "http://hooks.test/opspilot", "token": receiver.secret},
        headers=auth(key),
    )
    check = await client.post("/v1/integrations/actions/check", headers=auth(key))

    executed = await client.post(f"/v1/actions/{rollback['id']}/approve", headers=auth(key))

    assert no_secret.status_code == 422
    assert saved.status_code == 200
    assert check.json() == {"kind": "actions", "ok": True, "message": "Answered HTTP 200"}
    assert executed.json()["status"] == "executed"
    assert executed.json()["result"] == {"ok": True, "status_code": 200, "body": "queued"}
    assert receiver.received[0] == {"type": "ping"}
    sent = receiver.received[1]
    assert sent["type"] == "action.approved"
    assert sent["action"]["command"] == "kubectl rollout undo deployment/payments-api"
    assert sent["action"]["id"] == rollback["id"]

    # The receiver is down: the approval stands, the send is marked failed, and can be retried
    receiver.status = 500
    failed = await client.post(f"/v1/actions/{raise_pool['id']}/approve", headers=auth(key))
    assert failed.json()["status"] == "failed"
    assert failed.json()["result"]["status_code"] == 500
    receiver.status = 200
    retried = await client.post(f"/v1/actions/{raise_pool['id']}/retry", headers=auth(key))
    assert retried.json()["status"] == "executed"
    not_failed = await client.post(f"/v1/actions/{rollback['id']}/retry", headers=auth(key))
    assert not_failed.status_code == 409


def test_signatures_cover_the_timestamp_and_body() -> None:
    from opspilot.actions import sign

    signature = sign("s3cret", 1700000000, b'{"a":1}')
    assert verify("s3cret", "1700000000", b'{"a":1}', signature)
    assert not verify("s3cret", "1700000001", b'{"a":1}', signature)
    assert not verify("s3cret", "1700000000", b'{"a":2}', signature)
    assert not verify("other", "1700000000", b'{"a":1}', signature)
    assert not verify("s3cret", "soon", b'{"a":1}', signature)


# ---------------------------------------------------------------------------- postmortem --


async def test_drafting_editing_and_publishing_a_postmortem(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    queued_investigations: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    key = await setup(client, create_workspace, ingest)
    enqueued.clear()
    app.state.llm = ScriptedChatModel(script=full_plan_script())
    sent = (await client.post("/v1/alerts", json=ALERT, headers=auth(key))).json()
    incident_id = sent["results"][0]["incident_id"]
    assert await run(app, settings, queued_investigations[0], app.state.llm) == (
        RunOutcome.COMPLETED
    )
    incident = (await client.get(f"/v1/incidents/{incident_id}", headers=auth(key))).json()
    cited = incident["investigation"]["citations"][0]["chunk_id"]
    made_up = uuid.uuid4()
    model = ScriptedChatModel(
        script=[
            lambda messages: AIMessage(
                content=(
                    "```markdown\n# Payments API error rate above 5%\n\n## Summary\n"
                    f"Long transactions exhausted the pool [c:{cited}] [c:{made_up}].\n\n"
                    "## Impact\nTODO: how many payments failed\n```"
                ),
                usage_metadata=USAGE,  # type: ignore[arg-type]
            ),
        ]
    )
    app.state.llm = model
    path = f"/v1/incidents/{incident_id}/postmortem"

    drafted = await client.post(path, headers=auth(key))

    assert drafted.status_code == 200, drafted.text
    draft = drafted.json()["draft"]
    assert draft.startswith("# Payments API error rate above 5%")  # code fence removed
    assert f"[c:{cited}]" in draft
    assert str(made_up) not in draft  # not something the investigation cited
    prompt = str(model.calls[0][0].content)
    assert "Long transactions exhaust the pool: supported" in prompt
    assert "Roll back payments-api (proposed)" in prompt
    assert "investigation_completed" in prompt

    edited = await client.put(
        path,
        json={"draft": draft.replace("TODO: how many payments failed", "312 payments")},
        headers=auth(key),
    )
    assert "312 payments" in edited.json()["draft"]

    unresolved = await client.post(f"{path}/publish", headers=auth(key))
    assert unresolved.status_code == 409
    await client.post(f"/v1/incidents/{incident_id}/resolve", headers=auth(key))
    published = await client.post(f"{path}/publish", headers=auth(key))
    twice = await client.post(f"{path}/publish", headers=auth(key))
    redraft = await client.post(path, headers=auth(key))

    assert published.status_code == 201, published.text
    document_id = published.json()["document_id"]
    assert twice.status_code == 409
    assert redraft.status_code == 409
    assert enqueued == [uuid.UUID(document_id)]
    async with app.state.sessionmaker() as session:
        document = await session.scalar(
            select(Document).where(Document.id == uuid.UUID(document_id))
        )
    assert document is not None
    assert document.source_type == "postmortem"
    assert document.title == "Postmortem: Payments API error rate above 5%"
    # Ids become footnotes a reader can follow
    assert "[c:" not in document.content
    assert "pool [^1]" in document.content
    assert "[^1]: payments > Payments API > Connection pool exhaustion" in document.content
    timeline = (await client.get(f"/v1/incidents/{incident_id}", headers=auth(key))).json()
    assert [e["type"] for e in timeline["timeline"]][-3:] == [
        "postmortem_drafted",
        "resolved",
        "postmortem_published",
    ]
    assert timeline["postmortem_document_id"] == document_id


async def test_postmortems_need_a_model_and_survive_its_failure(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
) -> None:
    _, key = await create_workspace()
    sent = (
        await client.post("/v1/alerts", json={**ALERT, "severity": "low"}, headers=auth(key))
    ).json()
    path = f"/v1/incidents/{sent['results'][0]['incident_id']}/postmortem"

    without_model = await client.post(path, headers=auth(key))
    app.state.llm = ScriptedChatModel(script=[fail])
    failed = await client.post(path, headers=auth(key))
    nothing = await client.post(f"{path}/publish", headers=auth(key))

    assert without_model.status_code == 503
    assert failed.status_code == 502
    assert (await client.get(path, headers=auth(key))).json()["draft"] is None
    assert nothing.status_code == 409


# --------------------------------------------------------------------------------- units --


def test_parsing_the_models_json_and_actions() -> None:
    assert parse_json('Sure! ```json\n{"steps": [1]}\n``` hope that helps') == {"steps": [1]}
    assert parse_json('{not json} then {"a": 2}') == {"a": 2}
    assert parse_json("no json here") is None

    text, actions = split_actions(
        'Do this.\n```actions\n[{"title": "Restart redis", "risk": "low"}, {"risk": "low"}]\n```'
    )
    assert text == "Do this."
    assert actions == [
        {
            "title": "Restart redis",
            "command": None,
            "risk": "low",
            "rationale": None,
            "citations": [],
        }
    ]
    assert split_actions("No block.") == ("No block.", [])
    assert split_actions("Bad.\n```actions\nnot json\n```") == ("Bad.", [])
