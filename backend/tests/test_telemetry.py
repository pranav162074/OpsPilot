"""Telemetry: integrations, deploys, metric/log/change queries, and the agent using them.
Prometheus, Loki and GitHub are played by an in-process fake behind httpx.MockTransport, so
nothing here touches a network."""

import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from opspilot.ingestion.service import Outcome
from opspilot.security.secrets import SecretBox
from tests.conftest import TEST_INTEGRATIONS_KEY, auth, upload
from tests.fakes import ScriptedChatModel, answer_citing, call_tool, reply, retrieved_ids

PROM = "http://prometheus.test:9090"
LOKI = "http://loki.test:3100"


class FakeTelemetry:
    """Plays Prometheus, Loki and GitHub's API, and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.series: list[dict[str, Any]] = []
        self.prom_status = 200
        self.log_streams: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.github_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "prometheus.test":
            if self.prom_status != 200:
                return httpx.Response(
                    self.prom_status, json={"status": "error", "error": "unauthorized"}
                )
            if path == "/api/v1/query_range":
                query = request.url.params["query"]
                if "(" in query and not query.endswith(")"):
                    return httpx.Response(
                        400,
                        json={"status": "error", "error": "parse error: unclosed left parenthesis"},
                    )
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"resultType": "matrix", "result": self.series},
                    },
                )
            if path == "/api/v1/label/__name__/values":
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": ["http_request_duration_seconds_bucket", "redis_up", "up"],
                    },
                )
            if path == "/api/v1/status/buildinfo":
                return httpx.Response(200, json={"status": "success", "data": {"version": "3.5.0"}})
        if host == "loki.test" and path == "/loki/api/v1/query_range":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "streams", "result": self.log_streams},
                },
            )
        if host == "api.github.com":
            if self.github_status != 200:
                return httpx.Response(
                    self.github_status, json={"message": "API rate limit exceeded"}
                )
            if path.endswith("/commits"):
                return httpx.Response(200, json=self.commits)
            return httpx.Response(200, json={"full_name": "acme/shop"})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake(app: FastAPI) -> FakeTelemetry:
    telemetry = FakeTelemetry()
    app.state.telemetry_http = httpx.AsyncClient(transport=httpx.MockTransport(telemetry.handler))
    return telemetry


async def connect(client: AsyncClient, key: str, kind: str, **body: Any) -> httpx.Response:
    return await client.put(f"/v1/integrations/{kind}", json=body, headers=auth(key))


async def connected_workspace(
    client: AsyncClient, create_workspace: Callable[..., Any], name: str = "Acme"
) -> str:
    _, key = await create_workspace(name)
    assert (
        await connect(client, key, "prometheus", url=PROM, token="prom-token")
    ).status_code == 200
    assert (await connect(client, key, "loki", url=LOKI)).status_code == 200
    res = await connect(client, key, "github", url="https://api.github.com", repository="acme/shop")
    assert res.status_code == 200
    return key


def matrix(labels: dict[str, str], values: list[float], step: int = 60) -> dict[str, Any]:
    start = time.time() - step * len(values)
    return {
        "metric": labels,
        "values": [[start + i * step, str(v)] for i, v in enumerate(values)],
    }


# ---------------------------------------------------------------------- integrations --


async def test_integrations_store_tokens_encrypted_and_never_return_them(
    client: AsyncClient, create_workspace: Callable[..., Any], app: FastAPI
) -> None:
    _, key = await create_workspace()
    res = await connect(client, key, "prometheus", url=PROM + "/", token="s3cret-token")
    assert res.status_code == 200
    assert res.json()["url"] == PROM  # trailing slash dropped
    assert res.json()["has_token"] is True
    assert "s3cret" not in res.text

    listed = await client.get("/v1/integrations", headers=auth(key))
    assert [i["kind"] for i in listed.json()] == ["prometheus"]
    assert "s3cret" not in listed.text

    async with app.state.sessionmaker() as session:
        stored = await session.scalar(text("SELECT secret_encrypted FROM integrations"))
    assert stored and "s3cret" not in stored
    assert (
        SecretBox(type("K", (), {"get_secret_value": lambda _: TEST_INTEGRATIONS_KEY})()).decrypt(
            stored
        )
        == "s3cret-token"
    )

    # Saving again replaces it (upsert), still one row
    again = await connect(client, key, "prometheus", url="http://prometheus.test:9091")
    assert again.json() == {
        **again.json(),
        "url": "http://prometheus.test:9091",
        "has_token": False,
    }
    assert len((await client.get("/v1/integrations", headers=auth(key))).json()) == 1


async def test_integration_input_is_validated(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, key = await create_workspace()
    for body, code in [
        ({"url": "ftp://prometheus.test"}, "invalid_url"),
        ({"url": "http://user:pass@prometheus.test"}, "invalid_url"),
        ({"url": "http://prometheus.test?x=1"}, "invalid_url"),
        ({"url": PROM, "repository": "acme/shop"}, "invalid_request"),
    ]:
        res = await connect(client, key, "prometheus", **body)
        assert res.status_code == 422 and res.json()["error"]["code"] == code, body
    res = await connect(client, key, "github", url="https://api.github.com", repository="nope")
    assert res.json()["error"]["code"] == "invalid_repository"
    assert (await connect(client, key, "grafana", url=PROM)).status_code == 422


async def test_only_admins_manage_integrations(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, admin = await create_workspace()
    res = await client.post(
        "/v1/api-keys", json={"name": "r", "role": "responder"}, headers=auth(admin)
    )
    responder = res.json()["key"]
    assert (await connect(client, responder, "prometheus", url=PROM)).status_code == 403
    assert (await client.get("/v1/integrations", headers=auth(responder))).status_code == 200


async def test_tokens_need_an_integrations_key(
    make_app: Callable[..., FastAPI], create_workspace: Callable[..., Any], client: AsyncClient
) -> None:
    _, key = await create_workspace()
    keyless = make_app(integrations_key=None)
    async with (
        keyless.router.lifespan_context(keyless),
        AsyncClient(transport=httpx.ASGITransport(app=keyless), base_url="http://test") as other,
    ):
        with_token = await connect(other, key, "prometheus", url=PROM, token="t")
        without = await connect(other, key, "prometheus", url=PROM)
    assert with_token.status_code == 409
    assert with_token.json()["error"]["code"] == "secrets_unavailable"
    assert without.status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9090",
        "http://localhost:9090",
        "http://10.0.0.5",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://[::1]:9090",
    ],
)
async def test_private_addresses_are_refused_when_not_allowed(
    make_app: Callable[..., FastAPI],
    create_workspace: Callable[..., Any],
    url: str,
) -> None:
    _, key = await create_workspace()
    strict = make_app(integrations_allow_private_urls=False)
    async with (
        strict.router.lifespan_context(strict),
        AsyncClient(transport=httpx.ASGITransport(app=strict), base_url="http://test") as other,
    ):
        res = await connect(other, key, "prometheus", url=url)
    assert res.status_code == 422
    assert "private or local" in res.json()["error"]["message"]


async def test_check_reports_whether_the_connection_works(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    ok = await client.post("/v1/integrations/prometheus/check", headers=auth(key))
    assert ok.json() == {"kind": "prometheus", "ok": True, "message": "Prometheus 3.5.0"}
    assert fake.requests[-1].headers["authorization"] == "Bearer prom-token"

    fake.prom_status = 401
    bad = await client.post("/v1/integrations/prometheus/check", headers=auth(key))
    assert bad.json()["ok"] is False
    assert "rejected the credentials" in bad.json()["message"]

    await client.delete("/v1/integrations/loki", headers=auth(key))
    missing = await client.post("/v1/integrations/loki/check", headers=auth(key))
    assert missing.status_code == 404


# ------------------------------------------------------------------------------ metrics --


async def test_metrics_are_summarised_with_the_series_that_moved_most_first(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    fake.series = [
        matrix({"route": "/health"}, [0.01, 0.01, 0.01]),
        matrix({"route": "/:code"}, [0.02, 0.05, 2.4, 2.5]),
        matrix({"route": "/api/links"}, [0.1, float("nan"), 0.3]),
    ]
    res = await client.post(
        "/v1/telemetry/metrics",
        json={"query": "histogram_quantile(0.95, rate(x[5m]))", "minutes": 30},
        headers=auth(key),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_series"] == 3
    first = body["series"][0]
    assert first["labels"] == {"route": "/:code"}
    assert (first["min"], first["max"], first["last"]) == (0.02, 2.5, 2.5)
    nan_series = body["series"][1]
    assert nan_series["avg"] == pytest.approx(0.2)  # NaN samples ignored, and JSON-safe

    sent = fake.requests[-1]
    params = parse_qs(sent.url.query.decode())
    assert params["query"] == ["histogram_quantile(0.95, rate(x[5m]))"]
    window = float(params["end"][0]) - float(params["start"][0])
    assert window == pytest.approx(30 * 60, abs=1)
    assert params["step"] == ["30"]  # about 60 points, never under 15s


async def test_bad_queries_and_missing_integrations_are_explained(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    bad = await client.post(
        "/v1/telemetry/metrics", json={"query": "rate(x[5m]"}, headers=auth(key)
    )
    assert bad.status_code == 502
    assert bad.json()["error"]["code"] == "telemetry_error"
    assert "unclosed left parenthesis" in bad.json()["error"]["message"]

    too_long = await client.post(
        "/v1/telemetry/metrics", json={"query": "up", "minutes": 10_000}, headers=auth(key)
    )
    assert too_long.status_code == 422

    _, other = await create_workspace("Bare")
    none = await client.post("/v1/telemetry/metrics", json={"query": "up"}, headers=auth(other))
    assert "No Prometheus is connected" in none.json()["error"]["message"]


async def test_metric_names_can_be_filtered(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    res = await client.get(
        "/v1/telemetry/metric-names", params={"contains": "REDIS"}, headers=auth(key)
    )
    assert res.json() == ["redis_up"]


# --------------------------------------------------------------------------------- logs --


async def test_logs_newest_first_with_safely_built_queries(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    now_ns = int(time.time() * 1e9)
    fake.log_streams = [
        {
            "stream": {"service": "kuturl"},
            "values": [
                [str(now_ns - 2_000_000_000), '{"msg":"older"}'],
                [str(now_ns - 1_000_000_000), '{"msg":"newest", "pad":"' + "x" * 1000 + '"}'],
            ],
        }
    ]
    res = await client.post(
        "/v1/telemetry/logs",
        json={"service": "kuturl", "contains": 'say "hi" \\ bye', "minutes": 15},
        headers=auth(key),
    )
    body = res.json()
    assert body["query"] == '{service="kuturl"} |= "say \\"hi\\" \\\\ bye"'
    assert [line["line"][:16] for line in body["lines"]] == ['{"msg":"newest",', '{"msg":"older"}']
    assert body["lines"][0]["line"].endswith("[...]")  # long lines are cut

    params = parse_qs(fake.requests[-1].url.query.decode())
    assert params["direction"] == ["backward"]

    # A service name that could break out of the selector never reaches Loki
    sneaky = await client.post(
        "/v1/telemetry/logs", json={"service": 'x"} or {job=~".+'}, headers=auth(key)
    )
    assert sneaky.status_code == 422


# ------------------------------------------------------------------- deploys and changes --


async def test_deploys_are_recorded_by_responders_and_scoped_to_the_workspace(
    client: AsyncClient, create_workspace: Callable[..., Any]
) -> None:
    _, key = await create_workspace()
    viewer = (
        await client.post("/v1/api-keys", json={"name": "v", "role": "viewer"}, headers=auth(key))
    ).json()["key"]
    body = {"service": "kuturl", "version": "v1.4.0", "commit_sha": "a1b2c3d", "deployed_by": "ci"}
    assert (await client.post("/v1/deploys", json=body, headers=auth(viewer))).status_code == 403
    created = await client.post("/v1/deploys", json=body, headers=auth(key))
    assert created.status_code == 201
    assert created.json()["environment"] == "production"

    bad = await client.post(
        "/v1/deploys", json={**body, "commit_sha": "not-a-sha!"}, headers=auth(key)
    )
    assert bad.status_code == 422

    _, other = await create_workspace("Other")
    assert (await client.get("/v1/deploys", headers=auth(other))).json() == []
    listed = await client.get("/v1/deploys", headers=auth(key))
    assert [d["version"] for d in listed.json()] == ["v1.4.0"]


async def test_changes_merge_deploys_and_commits_newest_first(
    client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    now = datetime.now(UTC)
    for minutes, version in [(90, "v1"), (10, "v2")]:
        await client.post(
            "/v1/deploys",
            json={
                "service": "kuturl",
                "version": version,
                "deployed_at": (now - timedelta(minutes=minutes)).isoformat(),
            },
            headers=auth(key),
        )
    fake.commits = [
        {
            "sha": "f" * 40,
            "html_url": "https://github.com/acme/shop/commit/fff",
            "commit": {
                "message": "Rotate CODE_SECRET\n\nlonger body",
                "author": {"name": "Pranav", "date": (now - timedelta(minutes=30)).isoformat()},
            },
        }
    ]
    res = await client.get("/v1/telemetry/changes", params={"hours": 3}, headers=auth(key))
    body = res.json()
    assert [(c["kind"], c.get("version") or c["summary"]) for c in body["changes"]] == [
        ("deploy", "v2"),
        ("commit", "Rotate CODE_SECRET"),
        ("deploy", "v1"),
    ]
    assert body["warning"] is None

    fake.github_status = 403
    degraded = (await client.get("/v1/telemetry/changes", headers=auth(key))).json()
    assert [c["version"] for c in degraded["changes"]] == ["v2", "v1"]
    assert degraded["warning"] == "GitHub's rate limit is used up; a token raises it"


# ----------------------------------------------------------------------- the agent's view --


async def test_the_agent_checks_metrics_and_cites_what_it_saw(
    app: FastAPI,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    ingest: Callable[..., Any],
    fake: FakeTelemetry,
) -> None:
    key = await connected_workspace(client, create_workspace)
    doc = (
        await upload(
            client,
            key,
            "# Kuturl\n\n## Redis down\n\nIf redis_up is 0, restart the Redis container.\n",
            "kuturl.md",
            source_type="runbook",
        )
    ).json()
    assert await ingest(doc["id"]) == Outcome.READY
    fake.series = [matrix({"__name__": "redis_up", "instance": "redis:6379"}, [1, 1, 0, 0])]

    model = ScriptedChatModel(
        script=[
            call_tool("query_metrics", promql="redis_up", minutes=15),
            call_tool("search_knowledge_base", query="redis down"),
            # Cites the metrics observation (first id) and the runbook passage (second)
            lambda messages: answer_citing(
                f"redis_up dropped to 0 [c:{retrieved_ids(messages)[0]}]. "
                "Restart the Redis container.",
                which=1,
            )(messages),
            reply("SUPPORTED"),
        ]
    )
    app.state.llm = model
    res = await client.post(
        "/v1/investigations", json={"question": "Kuturl redirects fail. Why?"}, headers=auth(key)
    )
    assert res.status_code == 201, res.text
    body = res.json()
    kinds = {c["kind"]: c for c in body["citations"]}
    assert set(kinds) == {"metrics", "document"}
    assert kinds["metrics"]["document_title"] == "Prometheus"
    assert kinds["metrics"]["heading"] == "redis_up"
    assert kinds["metrics"]["document_id"] is None
    assert body["invalid_citations"] == 0

    # The model saw the metric's summary, and the reviewer was shown it as a source
    tool_output = str(model.calls[1][-1].content)
    assert "redis_up" in tool_output and "last 0" in tool_output and "max 1" in tool_output
    review_prompt = str(model.calls[3][0].content)
    assert "last 0" in review_prompt


async def test_the_agent_only_gets_tools_for_connected_integrations(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    _, key = await create_workspace()
    seen: list[list[str]] = []

    class Recording(ScriptedChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "Recording":  # type: ignore[override]
            seen.append([t.name for t in tools])
            return self

    app.state.llm = Recording(script=[reply("Nothing to go on."), reply("SUPPORTED")])
    await client.post("/v1/investigations", json={"question": "Anything odd?"}, headers=auth(key))
    assert seen == [["search_knowledge_base", "read_section", "recent_changes"]]

    await connect(client, key, "prometheus", url=PROM)
    app.state.llm = Recording(script=[reply("Nothing to go on."), reply("SUPPORTED")])
    await client.post("/v1/investigations", json={"question": "Anything odd?"}, headers=auth(key))
    assert "query_metrics" in seen[-1] and "search_logs" not in seen[-1]


async def test_telemetry_failures_reach_the_model_as_text_not_crashes(
    app: FastAPI, client: AsyncClient, create_workspace: Callable[..., Any], fake: FakeTelemetry
) -> None:
    key = await connected_workspace(client, create_workspace)
    fake.prom_status = 503
    model = ScriptedChatModel(
        script=[
            call_tool("query_metrics", promql="up"),
            reply("Prometheus is unavailable, so I can't confirm anything."),
            reply("SUPPORTED"),
        ]
    )
    app.state.llm = model
    res = await client.post(
        "/v1/investigations", json={"question": "Is the API up?"}, headers=auth(key)
    )
    assert res.status_code == 201
    assert "Metrics query failed: Prometheus refused the request" in str(model.calls[1][-1].content)


def test_summaries_handle_empty_and_nan_series() -> None:
    from opspilot.telemetry.clients import Series
    from opspilot.telemetry.service import summarise

    empty = summarise(Series(labels={}, points=[]))
    assert empty.last is None and empty.points == []
    nans = summarise(Series(labels={}, points=[(0.0, math.nan), (60.0, math.nan)]))
    assert nans.average is None
    many = summarise(Series(labels={}, points=[(float(i), float(i)) for i in range(100)]))
    assert len(many.points) == 8 and many.points[0][1] == 0 and many.points[-1][1] == 99
    assert json.dumps(many.average) == "49.5"
