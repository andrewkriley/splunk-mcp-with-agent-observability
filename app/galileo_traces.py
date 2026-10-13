"""Read Galileo sessions for the Buttercup traces panel.

The browser never receives GALILEO_API_KEY. Filter by X-Mcp-Lab-User
local-part only. GALILEO_TRACES_PANEL=0 disables the panel.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from app.workshop_user import workshop_user_from_value

HttpJson = Callable[..., tuple[int, Any]]
_FALSE = frozenset({"0", "false", "no", "off"})


def traces_panel_enabled() -> bool:
    flag = os.environ.get("GALILEO_TRACES_PANEL", "1").strip().lower()
    if flag in _FALSE:
        return False
    return bool(os.environ.get("GALILEO_API_KEY", "").strip())


def session_prefix(user: str) -> str:
    local = workshop_user_from_value(user)
    if not local:
        return ""
    return f"{local}-workshop-chat-"


def session_belongs(name: str, user: str) -> bool:
    prefix = session_prefix(user)
    return bool(prefix) and (name or "").startswith(prefix)


def filter_sessions(records: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
    prefix = session_prefix(user)
    if not prefix:
        return []
    matched = [row for row in records if session_belongs(str(row.get("name") or ""), user)]
    return sorted(matched, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def api_base_url() -> str:
    explicit = os.environ.get("GALILEO_API_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    console = os.environ.get("GALILEO_CONSOLE_URL", "").strip().rstrip("/")
    if "console." in console:
        return console.replace("console.", "api.", 1)
    if console.startswith("https://app."):
        return "https://api." + console[len("https://app.") :]
    return "https://api.galileo.ai"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = os.environ.get("GALILEO_API_KEY", "").strip()
    if key:
        headers["Galileo-API-Key"] = key
    return headers


def http_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: object = None,
    timeout: float = 15,
) -> tuple[int, Any]:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method=method.upper())
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    except OSError as exc:
        raise RuntimeError("Galileo API unreachable") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode())
    except json.JSONDecodeError:
        return status, None


def _project_path() -> str:
    ident = (
        os.environ.get("GALILEO_PROJECT_ID") or os.environ.get("GALILEO_PROJECT") or ""
    ).strip()
    return urllib.parse.quote(ident, safe="")


def _records(body: object) -> list[dict[str, Any]]:
    raw: object = None
    if isinstance(body, Mapping):
        raw = body.get("records") or body.get("sessions") or body.get("data")
    elif isinstance(body, list):
        raw = body
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for rec in raw:
        if not isinstance(rec, Mapping):
            continue
        sid = rec.get("id") or rec.get("session_id")
        if not sid:
            continue
        out.append(
            {
                "id": str(sid),
                "name": str(rec.get("name") or ""),
                "created_at": str(rec.get("created_at") or rec.get("updated_at") or ""),
                "traces": rec.get("traces") if isinstance(rec.get("traces"), list) else [],
            }
        )
    return out


def public_session(row: Mapping[str, Any]) -> dict[str, Any]:
    traces_out = []
    for trace in row.get("traces") or []:
        if not isinstance(trace, Mapping):
            continue
        spans = []
        for span in trace.get("spans") or []:
            if not isinstance(span, Mapping):
                continue
            spans.append(
                {
                    "type": str(span.get("type") or span.get("span_type") or ""),
                    "name": str(span.get("name") or "")[:120],
                }
            )
        traces_out.append(
            {
                "name": str(trace.get("name") or "")[:120],
                "spans": spans,
            }
        )
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "created_at": str(row.get("created_at") or ""),
        "traces": traces_out,
    }


def fetch_sessions(user: str, http: HttpJson | None = None) -> list[dict[str, Any]]:
    prefix = session_prefix(user)
    if not prefix:
        return []
    post = http or http_json
    project = _project_path()
    if not project:
        return []
    url = f"{api_base_url()}/v2/projects/{project}/sessions/search"
    body: dict[str, Any] = {
        "filters": [
            {
                "type": "text",
                "column_id": "name",
                "operator": "contains",
                "value": prefix,
            }
        ],
        "limit": 50,
        "starting_token": 0,
        "sort": {
            "column_id": "created_at",
            "ascending": False,
            "sort_type": "column",
        },
    }
    stream = os.environ.get("GALILEO_LOG_STREAM_ID", "").strip()
    if stream:
        body["log_stream_id"] = stream
    status, payload = post("POST", url, headers=_headers(), body=body)
    if status not in (200, 201):
        raise RuntimeError("Galileo session search failed")
    return filter_sessions(_records(payload), user)


def fetch_session(session_id: str, http: HttpJson | None = None) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not sid or "/" in sid or ".." in sid:
        return None
    post = http or http_json
    project = _project_path()
    if not project:
        return None
    encoded = urllib.parse.quote(sid, safe="")
    url = f"{api_base_url()}/v2/projects/{project}/sessions/{encoded}"
    status, payload = post("GET", url, headers=_headers(), body=None)
    if status not in (200, 201) or not isinstance(payload, Mapping):
        return None
    records = _records({"records": [payload]}) or [
        {
            "id": str(payload.get("id") or sid),
            "name": str(payload.get("name") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "traces": payload.get("traces") if isinstance(payload.get("traces"), list) else [],
        }
    ]
    return records[0] if records else None
