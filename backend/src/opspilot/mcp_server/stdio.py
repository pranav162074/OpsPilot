"""OpsPilot's MCP tools over stdio, for clients that launch servers as a local command
(Claude Desktop, Cursor). It talks to a running OpsPilot API over HTTP:

    OPSPILOT_URL=http://localhost:8000 OPSPILOT_API_KEY=ops_... uv run opspilot-mcp

Claude Desktop (claude_desktop_config.json):

    {"mcpServers": {"opspilot": {
        "command": "uv",
        "args": ["--directory", "/path/to/OpsPilot/backend", "run", "opspilot-mcp"],
        "env": {"OPSPILOT_URL": "http://localhost:8000", "OPSPILOT_API_KEY": "ops_..."}}}}
"""

import asyncio
import logging
import os
import sys

import httpx
from mcp.server.mcpserver import Context

from opspilot.mcp_server.api import OpsPilotApi
from opspilot.mcp_server.server import build_server
from opspilot.security.api_keys import looks_like_api_key


async def serve(base_url: str, api_key: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        api = OpsPilotApi(client, api_key)

        def api_for(_: Context) -> OpsPilotApi:
            return api

        await build_server(api_for).run_stdio_async()


def main() -> None:
    # stdout carries the protocol: anything else printed there would corrupt it
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    base_url = os.environ.get("OPSPILOT_URL", "http://localhost:8000").rstrip("/")
    api_key = os.environ.get("OPSPILOT_API_KEY", "")
    if not looks_like_api_key(api_key):
        sys.exit("Set OPSPILOT_API_KEY to an OpsPilot API key (ops_...)")
    asyncio.run(serve(base_url, api_key))


if __name__ == "__main__":
    main()
