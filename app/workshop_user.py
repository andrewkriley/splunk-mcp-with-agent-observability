"""Email local-part helpers for Galileo chat names. Stdlib only."""
from __future__ import annotations

import re

_SAFE = re.compile(r"[^a-z0-9._-]+")


def workshop_user_from_value(value: str | None) -> str:
    """Local-part of an email, or the value itself. Never keeps the domain."""
    raw = (value or "").strip().split("@", 1)[0].strip().lower()
    return _SAFE.sub("", raw)[:64]


def galileo_session_name(conversation_id: str, user: str = "") -> str:
    cid = (conversation_id or "chat").strip() or "chat"
    prefix = workshop_user_from_value(user)
    base = f"workshop-chat-{cid}"
    return f"{prefix}-{base}" if prefix else base
