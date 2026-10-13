"""Per-turn Splunk tool budget and workshop-facing comfort notes.

Context-local so concurrent chat turns do not share flags. run_agent_turn
resets at the start of a turn and wraps the user-visible reply.
"""

from __future__ import annotations

import json
from contextvars import ContextVar

MAX_SPLUNK_QUERIES = 3
MAX_TOOL_RESULT_CHARS = 12000
QUERY_TOOLS = frozenset({"splunk_run_query"})
_QUERY_ARG_KEYS = ("search", "query", "spl")

CONTROL_NOTES = {
    "truncated": (
        "A Splunk result was very large, so I shortened it to keep this turn readable."
    ),
    "query_cap": (
        "I limited how many Splunk searches I ran this turn so we could get you an answer sooner."
    ),
    "repeat": (
        "The same Splunk search started repeating, so I stopped that loop and used the results I already had."
    ),
    "forced_final": (
        "I stopped further tool calls and answered from the Splunk results I already had, "
        "to stay within the workshop tool budget."
    ),
}

_NOTE_ORDER = ("truncated", "query_cap", "repeat", "forced_final")

_used: ContextVar[set[str] | None] = ContextVar("search_controls", default=None)


def reset_controls() -> None:
    _used.set(set())


def note_control(key: str) -> None:
    if key not in CONTROL_NOTES:
        raise ValueError(f"unknown search control {key}")
    used = _used.get()
    if used is None:
        used = set()
        _used.set(used)
    used.add(key)


def used_controls() -> set[str]:
    return set(_used.get() or ())


def with_comfort_notes(text: str) -> str:
    notes = [CONTROL_NOTES[key] for key in _NOTE_ORDER if key in used_controls()]
    if not notes:
        return text
    return f"{text.rstrip()}\n\nNote: {' '.join(notes)}"


def fingerprint(name: str, arguments: dict | None) -> tuple[str, str]:
    args = dict(arguments or {})
    if name in QUERY_TOOLS:
        for key in _QUERY_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str):
                args[key] = " ".join(value.split())
    return name, json.dumps(args, sort_keys=True, default=str)


def is_query_tool(name: str) -> bool:
    return name in QUERY_TOOLS
