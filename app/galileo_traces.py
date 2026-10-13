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
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app.workshop_user import workshop_user_from_value

HttpJson = Callable[..., tuple[int, Any]]
_FALSE = frozenset({"0", "false", "no", "off"})
_PROJECT_IDS: dict[str, str] = {}
_STREAM_IDS: dict[tuple[str, str], str] = {}


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


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _project_rows(body: object) -> list[Mapping[str, Any]]:
    if isinstance(body, Mapping):
        raw = body.get("projects") or body.get("records") or body.get("data")
    elif isinstance(body, list):
        raw = body
    else:
        raw = None
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def resolve_project_id(http: HttpJson | None = None) -> str:
    explicit = (os.environ.get("GALILEO_PROJECT_ID") or "").strip()
    if _looks_like_uuid(explicit):
        return explicit
    name = (os.environ.get("GALILEO_PROJECT") or "").strip()
    if _looks_like_uuid(name):
        return name
    if not name:
        return ""
    cached = _PROJECT_IDS.get(name)
    if cached:
        return cached
    post = http or http_json
    token: object = 0
    for _ in range(10):
        status, payload = post(
            "POST",
            f"{api_base_url()}/v2/projects/paginated",
            headers=_headers(),
            body={"limit": 50, "starting_token": token, "filters": []},
        )
        if status not in (200, 201):
            break
        for row in _project_rows(payload):
            if str(row.get("name") or "") != name:
                continue
            pid = str(row.get("id") or "")
            if _looks_like_uuid(pid):
                _PROJECT_IDS[name] = pid
                return pid
        nxt = payload.get("next_starting_token") if isinstance(payload, Mapping) else None
        if nxt is None or nxt == token:
            break
        token = nxt
    return ""


def resolve_log_stream_id(project_id: str, http: HttpJson | None = None) -> str:
    explicit = (os.environ.get("GALILEO_LOG_STREAM_ID") or "").strip()
    if _looks_like_uuid(explicit):
        return explicit
    name = (os.environ.get("GALILEO_LOG_STREAM") or "").strip()
    if not project_id or not name:
        return ""
    cache_key = (project_id, name)
    cached = _STREAM_IDS.get(cache_key)
    if cached:
        return cached
    post = http or http_json
    encoded = urllib.parse.quote(project_id, safe="")
    status, payload = post(
        "GET",
        f"{api_base_url()}/v2/projects/{encoded}/log_streams",
        headers=_headers(),
        body=None,
    )
    if status not in (200, 201):
        return ""
    if isinstance(payload, Mapping):
        raw = payload.get("log_streams") or payload.get("records") or payload.get("data")
    else:
        raw = payload
    if not isinstance(raw, list):
        return ""
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("name") or "") != name:
            continue
        sid = str(row.get("id") or "")
        if sid:
            _STREAM_IDS[cache_key] = sid
            return sid
    return ""


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
    project = resolve_project_id(http=post)
    if not project:
        return []
    encoded = urllib.parse.quote(project, safe="")
    url = f"{api_base_url()}/v2/projects/{encoded}/sessions/search"
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
    stream = resolve_log_stream_id(project, http=post)
    if stream:
        body["log_stream_id"] = stream
    status, payload = post("POST", url, headers=_headers(), body=body)
    if status not in (200, 201):
        raise RuntimeError(f"Galileo session search failed ({status})")
    return filter_sessions(_records(payload), user)


def fetch_session(session_id: str, http: HttpJson | None = None) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not sid or "/" in sid or ".." in sid:
        return None
    post = http or http_json
    project = resolve_project_id(http=post)
    if not project:
        return None
    encoded = urllib.parse.quote(sid, safe="")
    project_path = urllib.parse.quote(project, safe="")
    url = f"{api_base_url()}/v2/projects/{project_path}/sessions/{encoded}"
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
