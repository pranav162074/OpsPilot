"""Serving the MCP tools from the API itself, at /mcp (MCP's Streamable HTTP transport).

Clients connect with a normal OpsPilot API key as a bearer token:

    claude mcp add --transport http opspilot http://localhost:8000/mcp \\
        --header "Authorization: Bearer ops_..."

Stateless mode: every MCP request is independent, so any API instance behind a load balancer
can answer it, with nothing to keep in memory between requests.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from opspilot.api.deps import find_active_key
from opspilot.config import Settings
from opspilot.mcp_server.api import OpsPilotApi
from opspilot.mcp_server.server import build_server

MCP_PATH = "/mcp"


def bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


class ApiKeyGate:
    """Turns away requests without a valid OpsPilot API key before any MCP processing, with
    a plain 401 the client can show. Checking the key doesn't spend its rate limit; the tool
    calls behind it do, like any other API request."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        token = bearer_token(request.headers.get("authorization"))
        valid = False
        if token:
            async with request.app.state.sessionmaker() as session:
                valid = await find_active_key(session, token, datetime.now(UTC)) is not None
        if not valid:
            response = JSONResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Send an OpsPilot API key as: Authorization: Bearer <key>",
                    }
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="OpsPilot"'},
            )
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)


class McpEndpoint:
    """The /mcp route and the lifecycle it needs, for one app instance."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self._app = app
        self._client: httpx.AsyncClient | None = None
        self.server = build_server(
            self._api_for, agent_timeout_seconds=settings.agent_timeout_seconds
        )
        # Builds the session manager; the Starlette app it returns isn't used, the route
        # below serves the same manager behind the API-key gate
        self.server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            stateless_http=True,
            # Rebinding protection guards unauthenticated local servers; every request here
            # needs an API key, and a fixed host list would break deployments behind a domain
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self.route = Route(
            MCP_PATH,
            endpoint=ApiKeyGate(StreamableHTTPASGIApp(self.server.session_manager)),
            methods=["GET", "POST", "DELETE"],
        )

    def _api_for(self, ctx: Context) -> OpsPilotApi:
        if self._client is None:
            raise ToolError("OpsPilot is starting up; try again")
        headers = ctx.headers or {}
        token = bearer_token(headers.get("authorization"))
        if token is None:  # the gate makes this unreachable, but never act without a key
            raise ToolError("No API key on this request")
        return OpsPilotApi(self._client, token, request_id=headers.get("x-request-id"))

    async def _serve_sessions(self, started: asyncio.Event, stop: asyncio.Event) -> None:
        async with self.server.session_manager.run():
            started.set()
            await stop.wait()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        # The session manager holds an anyio task group, which must be opened and closed by
        # the same task; the app's lifespan may enter and exit in different ones, so the
        # manager gets a task of its own
        started, stop = asyncio.Event(), asyncio.Event()
        sessions = asyncio.create_task(self._serve_sessions(started, stop))
        await asyncio.wait(
            {sessions, asyncio.ensure_future(started.wait())}, return_when="FIRST_COMPLETED"
        )
        if sessions.done():
            sessions.result()  # it failed to start: raise its error
        # Tool calls go to the API in-process: no network hop, no port to know
        transport = httpx.ASGITransport(app=self._app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://opspilot.internal"
            ) as client:
                self._client = client
                yield
        finally:
            self._client = None
            stop.set()
            await sessions
