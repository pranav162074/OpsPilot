import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update

from opspilot.agent.runner import RunOutcome, execute_investigation, fail_abandoned_investigations
from opspilot.config import Settings
from opspilot.incidents.service import AlertInput, fingerprint_of
from opspilot.ingestion.service import Outcome
from opspilot.models import Incident, Investigation, Severity
from tests.conftest import auth, upload
from tests.fakes import ScriptedChatModel, answer_citing, call_tool, fail, reply

RUNBOOK = """# Payments API

## Connection pool exhaustion

Requests fail with ERR_POOL_TIMEOUT. Look for long transactions in pg_stat_activity.
"""

ALERT = {
    "title": "Payments API error rate above 5%",
    "severity": "critical",
    "description": "ERR_POOL_TIMEOUT in payments-api logs",
    "labels": {"service": "payments-api", "env": "prod"},
}


async def send(client: AsyncClient, key: str, body: dict[str, Any]) -> Any:
    res = await client.post("/v1/alerts", json=body, headers=auth(key))
    assert res.status_code == 202, res.text
    return res.json()["results"]


async def test_repeated_alerts_attach_to_one_incident(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()

    first = await send(client, key, ALERT)
    again = await send(client, key, {**ALERT, "labels": {"env": "prod", "service": "payments-api"}})

    assert first[0]["action"] == "opened"
    assert again[0]["action"] == "deduplicated"  # same labels in a different order
    assert again[0]["incident_id"] == first[0]["incident_id"]
    incident = (
        await client.get(f"/v1/incidents/{first[0]['incident_id']}", headers=auth(key))
    ).json()
    assert incident["alert_count"] == 2
    assert incident["severity"] == "critical"
    assert [e["type"] for e in incident["timeline"]] == ["opened"]


async def test_a_burst_of_identical_alerts_opens_exactly_one_incident(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()

    results = await asyncio.gather(*(send(client, key, ALERT) for _ in range(20)))

    actions = [r[0]["action"] for r in results]
    assert actions.count("opened") == 1
    assert actions.count("deduplicated") == 19
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 1
        assert await session.scalar(select(Incident.alert_count)) == 20


async def test_a_resolved_alert_closes_the_incident_and_a_new_one_reopens(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    opened = await send(client, key, ALERT)

    resolved = await send(client, key, {**ALERT, "status": "resolved"})
    nothing_open = await send(client, key, {**ALERT, "status": "resolved"})
    fires_again = await send(client, key, ALERT)

    assert resolved[0] == {**opened[0], "action": "resolved"}
    assert nothing_open[0]["action"] == "ignored"
    # The old incident stays resolved; the problem coming back is a new incident
    assert fires_again[0]["action"] == "opened"
    assert fires_again[0]["incident_id"] != opened[0]["incident_id"]


async def test_accepts_alertmanager_webhooks(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighLatency", "severity": "warning", "service": "api"},
                "annotations": {"summary": "p99 latency above 2s", "description": "for 10 minutes"},
                "fingerprint": "a1b2c3",
            },
            {
                "status": "firing",
                "labels": {"alertname": "DiskFull", "severity": "page"},
                "annotations": {},
                "fingerprint": "d4e5f6",
            },
        ],
    }

    results = await send(client, key, payload)
    incidents = (await client.get("/v1/incidents", headers=auth(key))).json()

    assert [r["action"] for r in results] == ["opened", "opened"]
    by_title = {i["title"]: i for i in incidents}
    assert by_title["p99 latency above 2s"]["severity"] == "medium"
    assert by_title["p99 latency above 2s"]["source"] == "alertmanager"
    assert by_title["DiskFull"]["severity"] == "critical"  # "page" means wake someone up
    assert by_title["DiskFull"]["fingerprint"] == "d4e5f6"


async def test_rejects_bad_alerts_and_viewers(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    viewer = (
        await client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=auth(key))
    ).json()["key"]

    missing_title = await client.post("/v1/alerts", json={"severity": "high"}, headers=auth(key))
    bad_severity = await client.post(
        "/v1/alerts", json={"title": "x", "severity": "apocalyptic"}, headers=auth(key)
    )
    from_viewer = await client.post("/v1/alerts", json=ALERT, headers=auth(viewer))

    assert missing_title.status_code == 422
    assert missing_title.json()["error"]["details"][0]["field"] == "title"
    assert bad_severity.status_code == 422
    assert from_viewer.status_code == 403


async def test_acknowledge_and_resolve(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    incident_id = (await send(client, key, ALERT))[0]["incident_id"]
    path = f"/v1/incidents/{incident_id}"

    acked = await client.post(f"{path}/acknowledge", headers=auth(key))
    acked_twice = await client.post(f"{path}/acknowledge", headers=auth(key))
    resolved = await client.post(f"{path}/resolve", headers=auth(key))
    resolved_twice = await client.post(f"{path}/resolve", headers=auth(key))

    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_at"] is not None
    assert acked_twice.status_code == 409
    assert resolved.json()["status"] == "resolved"
    assert resolved_twice.status_code == 409
    assert [e["type"] for e in resolved.json()["timeline"]] == [
        "opened",
        "acknowledged",
        "resolved",
    ]
    open_only = (await client.get("/v1/incidents?status=open", headers=auth(key))).json()
    assert open_only == []


async def test_workspaces_cannot_see_each_others_incidents(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, acme = await create_workspace("Acme")
    _, globex = await create_workspace("Globex")
    incident_id = (await send(client, acme, ALERT))[0]["incident_id"]

    # The same alert in another workspace is a different incident
    theirs = await send(client, globex, ALERT)

    assert theirs[0]["action"] == "opened"
    assert (
        await client.get(f"/v1/incidents/{incident_id}", headers=auth(globex))
    ).status_code == 404
    acked = await client.post(f"/v1/incidents/{incident_id}/acknowledge", headers=auth(globex))
    assert acked.status_code == 404


async def test_serious_incidents_get_an_investigation_in_the_background(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    queued_investigations: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> None:
    _, key = await create_workspace()
    doc = (await upload(client, key, RUNBOOK, "payments.md")).json()
    await ingest(doc["id"])
    app.state.llm = ScriptedChatModel(
        script=[
            call_tool("search_knowledge_base", query="ERR_POOL_TIMEOUT"),
            answer_citing("Check pg_stat_activity for long transactions."),
            reply("SUPPORTED"),
        ]
    )

    critical = await send(client, key, ALERT)
    minor = await send(client, key, {**ALERT, "title": "Disk 70% full", "severity": "low"})
    repeat = await send(client, key, ALERT)

    # Only the new critical incident: not the low one, not the repeat
    assert len(queued_investigations) == 1
    incident = (
        await client.get(f"/v1/incidents/{critical[0]['incident_id']}", headers=auth(key))
    ).json()
    assert incident["investigation"]["status"] == "running"
    assert "Payments API error rate above 5%" in (await _question(app, queued_investigations[0]))
    low = (await client.get(f"/v1/incidents/{minor[0]['incident_id']}", headers=auth(key))).json()
    assert low["investigation"] is None
    assert repeat[0]["action"] == "deduplicated"

    # What the worker does with the queued id
    outcome = await execute_investigation(
        investigation_id=queued_investigations[0],
        llm=app.state.llm,
        sessionmaker=app.state.sessionmaker,
        settings=settings,
        embedder=app.state.embedder,
        query_cache=app.state.query_cache,
    )
    incident = (
        await client.get(f"/v1/incidents/{critical[0]['incident_id']}", headers=auth(key))
    ).json()

    assert outcome == RunOutcome.COMPLETED
    assert incident["investigation"]["status"] == "completed"
    assert "pg_stat_activity" in incident["investigation"]["answer"]
    assert incident["investigation"]["citations"][0]["document_id"] == doc["id"]
    assert [e["type"] for e in incident["timeline"]] == [
        "opened",
        "investigation_started",
        "investigation_completed",
    ]


async def _question(app: FastAPI, investigation_id: uuid.UUID) -> str:
    async with app.state.sessionmaker() as session:
        question = await session.scalar(
            select(Investigation.question).where(Investigation.id == investigation_id)
        )
        return question or ""


async def test_no_model_means_no_automatic_investigation(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()

    await send(client, key, ALERT)

    assert queued_investigations == []


async def test_a_task_delivered_twice_runs_once(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    model = ScriptedChatModel(
        script=[reply("Nothing in the docs covers this."), reply("SUPPORTED")]
    )
    app.state.llm = model
    res = await client.post(
        "/v1/investigations",
        json={"question": "Is the CDN down?", "background": True},
        headers=auth(key),
    )
    assert res.status_code == 202
    assert res.json()["status"] == "running"
    [investigation_id] = queued_investigations

    def run() -> Awaitable[RunOutcome]:
        return execute_investigation(
            investigation_id=investigation_id,
            llm=model,
            sessionmaker=app.state.sessionmaker,
            settings=settings,
            embedder=app.state.embedder,
            query_cache=app.state.query_cache,
        )

    outcomes = await asyncio.gather(run(), run())

    assert sorted(outcomes) == sorted([RunOutcome.COMPLETED, RunOutcome.SKIPPED])
    assert len(model.calls) == 2  # one run's worth of model calls, not two
    polled = (await client.get(f"/v1/investigations/{investigation_id}", headers=auth(key))).json()
    assert polled["status"] == "completed"


async def test_failures_and_abandoned_runs_are_recorded_on_the_incident(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    app.state.llm = ScriptedChatModel(script=[fail])
    first = await send(client, key, ALERT)
    second = await send(client, key, {**ALERT, "title": "Checkout down"})

    failed = await execute_investigation(
        investigation_id=queued_investigations[0],
        llm=app.state.llm,
        sessionmaker=app.state.sessionmaker,
        settings=settings,
        embedder=app.state.embedder,
        query_cache=app.state.query_cache,
    )
    # The second one's worker "died": its investigation stays running, long past the timeout
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(Investigation)
            .where(Investigation.id == queued_investigations[1])
            .values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
    abandoned = await fail_abandoned_investigations(
        app.state.sessionmaker, settings, datetime.now(UTC)
    )

    assert failed == RunOutcome.FAILED
    assert abandoned == 1
    for result in (first, second):
        incident = (
            await client.get(f"/v1/incidents/{result[0]['incident_id']}", headers=auth(key))
        ).json()
        assert incident["investigation"]["status"] == "failed"
        assert incident["timeline"][-1]["type"] == "investigation_failed"


async def test_investigating_an_incident_again(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    queued_investigations: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    incident_id = (await send(client, key, {**ALERT, "severity": "low"}))[0]["incident_id"]
    path = f"/v1/incidents/{incident_id}/investigate"

    without_model = await client.post(path, headers=auth(key))
    app.state.llm = ScriptedChatModel(script=[])
    started = await client.post(path, headers=auth(key))

    assert without_model.status_code == 503
    assert started.status_code == 202
    assert started.json()["investigation"]["status"] == "running"
    assert len(queued_investigations) == 1


def test_fingerprints_ignore_label_order_but_not_content() -> None:
    a = AlertInput(title="X", status="firing", labels={"a": "1", "b": "2"})
    b = AlertInput(title="X", status="firing", labels={"b": "2", "a": "1"})
    c = AlertInput(title="X", status="firing", labels={"a": "1", "b": "3"})

    assert fingerprint_of(a) == fingerprint_of(b) != fingerprint_of(c)
    assert fingerprint_of(AlertInput(title="X", status="firing", fingerprint="given")) == "given"
    assert Severity.CRITICAL.rank > Severity.HIGH.rank
