"""Splunk MCP client — same derived endpoint and self-signed-cert handling as scripts/setup_mcp.py."""

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# Own handler so this shows in the uvicorn process even when the root logger
# is left at WARNING. Lines name the endpoint host and the tool request only.
_mcp_log = logging.getLogger("app.mcp")
_mcp_log.setLevel(logging.INFO)
if not _mcp_log.handlers:
    _mcp_handler = logging.StreamHandler()
    _mcp_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _mcp_log.addHandler(_mcp_handler)
_mcp_log.propagate = False

_MAX_ARGUMENT_CHARS = 500


def _mcp_url() -> str:
    instance_url = os.environ["SPLUNK_INSTANCE_URL"].rstrip("/")
    return f"{instance_url}:8089/services/mcp"


def _endpoint() -> str:
    parsed = urlsplit(_mcp_url())
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{host}{port}{parsed.path}"


def _redact(text: str) -> str:
    token = os.environ.get("SPLUNK_MCP_TOKEN", "").strip()
    if token:
        text = text.replace(token, "[redacted]")
    return text


def log_mcp_skipped() -> None:
    _mcp_log.info("MCP skipped: no Splunk intent in this question or recent turns")


def log_mcp_request(kind: str, detail: str = "") -> None:
    message = f"MCP {kind} endpoint={_endpoint()}"
    if detail:
        message = f"{message} {detail}"
    _mcp_log.info("%s", _redact(message))


@asynccontextmanager
async def splunk_mcp_session():
    token = os.environ["SPLUNK_MCP_TOKEN"]
    log_mcp_request("session")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        verify=False,  # Splunk's management port uses a self-signed cert by default
    ) as http_client:
        async with streamable_http_client(_mcp_url(), http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def list_splunk_tools(session: ClientSession) -> list[dict]:
    log_mcp_request("list_tools")
    result = await session.list_tools()
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in result.tools]


async def call_tool(session: ClientSession, name: str, arguments: dict) -> str:
    payload = json.dumps(arguments, default=str, separators=(",", ":"))
    if len(payload) > _MAX_ARGUMENT_CHARS:
        payload = payload[:_MAX_ARGUMENT_CHARS] + "…"
    log_mcp_request("call", f"tool={name} arguments={payload}")
    result = await session.call_tool(name, arguments)
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))
