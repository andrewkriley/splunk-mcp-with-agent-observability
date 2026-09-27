"""Wire up and verify the Splunk MCP connection for this workshop.

Reads SPLUNK_INSTANCE_URL / SPLUNK_MCP_TOKEN from .env, derives the full MCP
endpoint (Splunk's official MCP Server for Splunk Platform always serves it
at <instance>:8089/services/mcp), does a live JSON-RPC connectivity check by
listing the server's tools, then writes/merges a project-root .mcp.json so
an MCP-aware AI harness (e.g. Claude Code) can see those tools too.

Splunk's management port (8089) serves a self-signed certificate by default,
so both the live check here and the generated Claude Code config disable TLS
verification for this one connection — that's expected, not a misconfig.
"""

import json
import os
import sys
import urllib3
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG_PATH = REPO_ROOT / ".mcp.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_config():
    load_dotenv(REPO_ROOT / ".env")

    instance_url = os.environ.get("SPLUNK_INSTANCE_URL", "").strip().rstrip("/")
    token = os.environ.get("SPLUNK_MCP_TOKEN", "").strip()

    missing = [name for name, value in (("SPLUNK_INSTANCE_URL", instance_url), ("SPLUNK_MCP_TOKEN", token)) if not value]
    if missing:
        print(f"Missing required .env value(s): {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the Splunk section, then re-run this script.")
        sys.exit(1)

    return instance_url, token


def rpc_call(mcp_url: str, token: str, method: str, params: dict, request_id: int):
    response = requests.post(
        mcp_url,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        verify=False,  # noqa: S501 — Splunk's management port uses a self-signed cert by default
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def check_connection(mcp_url: str, token: str):
    server_info = rpc_call(
        mcp_url,
        token,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "splunk-mcp-with-agent-observability", "version": "0.1"}},
        request_id=1,
    )["serverInfo"]

    tools = rpc_call(mcp_url, token, "tools/list", {}, request_id=2)["tools"]
    return server_info, [tool["name"] for tool in tools]


def write_mcp_config(mcp_url: str, token: str):
    config = {}
    if MCP_CONFIG_PATH.exists():
        try:
            config = json.loads(MCP_CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            print(f"Warning: {MCP_CONFIG_PATH} exists but isn't valid JSON — leaving it untouched.")
            return

    config.setdefault("mcpServers", {})
    config["mcpServers"]["splunk"] = {
        "command": "npx",
        "args": ["-y", "mcp-remote", mcp_url, "--header", f"Authorization: Bearer {token}"],
        "env": {"NODE_TLS_REJECT_UNAUTHORIZED": "0"},
    }

    MCP_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Wrote Splunk MCP server config to {MCP_CONFIG_PATH}")
    print("(requires Node.js/npx — mcp-remote is fetched on demand, no install needed)")


def main():
    instance_url, token = load_config()
    mcp_url = f"{instance_url}:8089/services/mcp"

    print(f"Checking connection to Splunk MCP server at {mcp_url} ...")
    try:
        server_info, tool_names = check_connection(mcp_url, token)
    except Exception as exc:  # noqa: BLE001 — this is a diagnostic tool, show the user what broke
        print(f"Could not connect: {exc}")
        print("Double check SPLUNK_INSTANCE_URL and SPLUNK_MCP_TOKEN with the facilitator.")
        sys.exit(1)

    print(f"Connected to {server_info['name']} v{server_info['version']}")
    print(f"Available Splunk MCP tools: {', '.join(tool_names) if tool_names else '(none returned)'}")

    write_mcp_config(mcp_url, token)


if __name__ == "__main__":
    main()
