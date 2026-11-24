"""The MCP tools talk to OpsPilot through its own REST API, never around it.

So every tool call gets exactly what an HTTP client would: API-key authentication, role
checks (a viewer key can search but not resolve incidents), per-key rate limits, input
validation and workspace isolation. Nothing is reimplemented, so nothing can drift.

Inside the API server the calls stay in-process (an ASGI transport, no network hop). The
stdio server for desktop clients makes the same calls over HTTP to a running OpsPilot.
"""

import json
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_TIMEOUT = httpx.Timeout(30.0)


def _message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except (json.JSONDecodeError, AttributeError):
        error = {}
    message = error.get("message") or response.reason_phrase or "Request failed"
    code = error.get("code")
    details = error.get("details")
    text = f"{message} ({code})" if code else str(message)
    if details:
        text += f": {json.dumps(details)}"
    return text


class OpsPilotApi:
    """A thin client for one API key. Errors come back as ToolError, which the model reads."""

    def __init__(self, client: httpx.AsyncClient, api_key: str, request_id: str | None = None):
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}
        if request_id:
            # The same ID in the MCP request's log lines and the API's
            self._headers["X-Request-ID"] = request_id

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers=self._headers,
                timeout=timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise ToolError("OpsPilot took too long to answer; try again") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach OpsPilot: {exc}") from exc

        if response.status_code == 401:
            raise ToolError("OpsPilot rejected the API key (revoked, expired or mistyped)")
        if response.status_code == 403:
            raise ToolError(f"This API key's role isn't allowed to do that: {_message(response)}")
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "a few")
            raise ToolError(f"Rate limited by OpsPilot; try again in {retry} seconds")
        if response.status_code >= 400:
            raise ToolError(_message(response))
        return response.json() if response.content else None
