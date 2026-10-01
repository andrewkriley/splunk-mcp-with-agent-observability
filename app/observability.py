"""Splunk Agent Observability instrumentation for the workshop chat agent.

The SDK ships a native import-swap wrapper for OpenAI (`splunk_ao.openai`)
— it auto-logs every call, no decorator needed. Anthropic and Gemini have no
such wrapper, so those calls build their span by hand via
`add_llm_span(...)` — not the `@log(span_type="llm")`
decorator, which generically dumps every function argument (including
`system_prompt` as a stray key, and provider-specific response shapes like
Anthropic's `thinking` blocks) into the span rather than a clean message
list. Splunk MCP tool calls still use `@log(span_type="tool")`, which
doesn't have this problem — its input/output are already simple. The whole
turn is wrapped in one `splunk_ao_context` so every LLM/tool span lands in a
single trace.

Traces go to Splunk Observability Cloud Agent Observability (realm + ingest
token). The SDK still treats `GALILEO_*` and standalone `SPLUNK_AO_API_KEY`
values as a second destination, including a blank console URL, so each turn
hides those names and publishes only the Observability Cloud settings.
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from splunk_ao import log, splunk_ao_context, start_session
from splunk_ao.config import SplunkAOConfig

from app import mcp_client

# Task-local so two overlapping /chat requests cannot share a Splunk session.
_mcp_session: contextvars.ContextVar = contextvars.ContextVar("splunk_mcp_session")
# Optional queue for the chat page: "model" while the LLM runs, "tool" on a Splunk call.
_activity: contextvars.ContextVar[asyncio.Queue | None] = contextvars.ContextVar("chat_activity", default=None)
# Conversations whose named Observability Cloud session was created in this process.
_ao_sessions: set[str] = set()
# Held for a whole turn. The SDK reads its destination from the process
# environment, so two overlapping chats cannot rewrite it mid-flight.
_o11y_lock = asyncio.Lock()
_remembered_secrets: list[str] = []

# Prior user/assistant text only. Tool transcripts stay inside the turn that made them.
MAX_HISTORY_TURNS = 8
MAX_CONVERSATIONS = 100
_history: OrderedDict[str, list[dict]] = OrderedDict()


def bind_activity(queue: asyncio.Queue):
    return _activity.set(queue)


def reset_activity(token) -> None:
    _activity.reset(token)


async def announce(phase: str, detail: str = "") -> None:
    """Tell the chat page whether this moment is a model call or a Splunk tool call.

    Yields once so a streaming response can flush the status before a sync LLM
    call blocks the event loop.
    """
    queue = _activity.get()
    if queue is None:
        return
    await queue.put({"phase": phase, "detail": detail})
    await asyncio.sleep(0)


def set_mcp_session(session):
    _mcp_session.set(session)


def prior_turns(conversation_id: str) -> list[dict]:
    turns = _history.get(conversation_id)
    return list(turns) if turns else []


def remember_turn(conversation_id: str, user_message: str, reply: str) -> None:
    if conversation_id in _history:
        _history.move_to_end(conversation_id)
    turns = _history.setdefault(conversation_id, [])
    turns.append({"role": "user", "content": user_message})
    turns.append({"role": "assistant", "content": reply})
    overflow = len(turns) - MAX_HISTORY_TURNS * 2
    if overflow > 0:
        del turns[:overflow]
    while len(_history) > MAX_CONVERSATIONS:
        _history.popitem(last=False)


_REALM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
_O11Y_ENV = ("SPLUNK_AO_REALM", "SPLUNK_AO_O11Y_TOKEN", "SPLUNK_AO_O11Y_API_TOKEN")
_SHARED_ENV = ("SPLUNK_AO_PROJECT", "SPLUNK_AO_AGENT_STREAM")
# Names the SDK would treat as a Galileo or standalone deployment.
_HIDDEN_ENV = (
    "SPLUNK_AO_API_KEY",
    "SPLUNK_AO_CONSOLE_URL",
    "SPLUNK_AO_API_URL",
    "SPLUNK_AO_DESTINATION",
    "GALILEO_API_KEY",
    "GALILEO_API_URL",
    "GALILEO_CONSOLE_URL",
    "GALILEO_PROJECT",
    "GALILEO_PROJECT_ID",
    "GALILEO_LOG_STREAM",
    "GALILEO_LOG_STREAM_ID",
)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _remember_secret(value: str) -> None:
    if value and value not in _remembered_secrets:
        _remembered_secrets.append(value)


def _http_url(value: str, label: str) -> str:
    raw = value.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not include a username or password")
    return raw.rstrip("/")


def ao_project() -> str:
    """Project name in Observability Cloud."""
    return _env("SPLUNK_AO_PROJECT") or "splunk-mcp-with-agent-observability"


def ao_agent_stream() -> str:
    """Agent Stream name in Observability Cloud."""
    return _env("SPLUNK_AO_AGENT_STREAM") or "default"


def _o11y_settings() -> tuple[dict[str, str] | None, str | None]:
    """Env to publish for Observability Cloud, or None when that destination is not configured."""
    realm = _env("SPLUNK_AO_REALM")
    token = _env("SPLUNK_AO_O11Y_TOKEN")
    api_token = _env("SPLUNK_AO_O11Y_API_TOKEN")
    if not realm and not token and not api_token:
        return None, None
    if not realm or not _REALM_RE.fullmatch(realm):
        return None, "SPLUNK_AO_REALM must be the Observability Cloud realm, such as au0"
    if not token:
        return None, "Observability Cloud trace export needs SPLUNK_AO_O11Y_TOKEN"
    published = {"SPLUNK_AO_REALM": realm, "SPLUNK_AO_O11Y_TOKEN": token}
    # A second copy of the same token does not change auth. Publish it only when it differs.
    if api_token and api_token != token:
        published["SPLUNK_AO_O11Y_API_TOKEN"] = api_token
        _remember_secret(api_token)
    _remember_secret(token)
    return published, None


def o11y_configured() -> bool:
    settings, error = _o11y_settings()
    return bool(settings) and not error


def o11y_errors() -> list[str]:
    settings, error = _o11y_settings()
    if error:
        return [error]
    if not settings:
        return ["Observability Cloud needs SPLUNK_AO_REALM and SPLUNK_AO_O11Y_TOKEN"]
    return []


def o11y_target() -> str:
    """Ingest host for the configured realm. No secrets."""
    settings, _error = _o11y_settings()
    realm = (settings or {}).get("SPLUNK_AO_REALM", "")
    return f"ingest.{realm}.observability.splunkcloud.com" if realm else ""


@contextmanager
def activate_o11y() -> Iterator[None]:
    """Publish Observability Cloud settings and hide Galileo names for this turn."""
    settings, error = _o11y_settings()
    if not settings:
        raise ValueError(error or "Observability Cloud is not configured in .env")
    project = ao_project()
    agent_stream = ao_agent_stream()
    target = o11y_target()
    managed = (*_O11Y_ENV, *_SHARED_ENV, *_HIDDEN_ENV)
    previous = {name: os.environ.get(name) for name in managed}
    try:
        for name in managed:
            os.environ.pop(name, None)
        os.environ.update(settings)
        os.environ["SPLUNK_AO_PROJECT"] = project
        os.environ["SPLUNK_AO_AGENT_STREAM"] = agent_stream
        SplunkAOConfig._instance = None
        _llm_log.info("AO target=%s", target)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _ao_session_id(conversation_id: str) -> str:
    """Conversation id used as the session external id, so traces join that session.

    Observability Cloud matches gen_ai.conversation.id to a session external id.
    The id returned by start_session is a different value, and exporting it
    creates a second session named "session".
    """
    if conversation_id not in _ao_sessions:
        start_session(name=f"workshop-chat-{conversation_id}", external_id=conversation_id)
        _ao_sessions.add(conversation_id)
    return conversation_id


OPENAI_MODEL = "gpt-4o"
ANTHROPIC_MODEL = "claude-sonnet-5"
GEMINI_MODEL = "gemini-3.6-flash"

# Own handler so this shows in the uvicorn process even when the root logger
# is left at WARNING. Lines name the provider and the message text only.
_llm_log = logging.getLogger("app.llm")
_llm_log.setLevel(logging.INFO)
if not _llm_log.handlers:
    _llm_handler = logging.StreamHandler()
    _llm_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _llm_log.addHandler(_llm_handler)
_llm_log.propagate = False

_SECRET_ENV_VARS = (
    "SPLUNK_MCP_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GALILEO_API_KEY",
    "OPENAI_SPEC_API_KEY",
    "SPLUNK_AO_API_KEY",
    "SPLUNK_AO_O11Y_TOKEN",
    "SPLUNK_AO_O11Y_API_TOKEN",
)
_MAX_LOGGED_MESSAGE_CHARS = 4000


def describe_llm(provider: str, model: str | None = None) -> tuple[str, str, str]:
    """Provider id, model name, and endpoint host that this turn will call."""
    if provider == "openai-spec":
        settings = openai_client_settings(provider, model)
        return "openai-spec", settings["model"], urlsplit(settings["base_url"]).hostname or ""
    if provider == "openai":
        return "openai", model or OPENAI_MODEL, "api.openai.com"
    if provider == "gemini":
        return "gemini", GEMINI_MODEL, "generativelanguage.googleapis.com"
    return "anthropic", ANTHROPIC_MODEL, "api.anthropic.com"


def log_llm_use(kind: str, provider: str, model: str, endpoint: str) -> None:
    _llm_log.info("LLM %s provider=%s model=%s endpoint=%s", kind, provider, model, endpoint)


def _redact_secrets(text: str, extra: tuple[str, ...] = ()) -> str:
    secrets = [os.environ.get(name, "").strip() for name in _SECRET_ENV_VARS]
    secrets.extend(_remembered_secrets)
    secrets.extend(extra)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _clip(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    text = _redact_secrets(text, extra_secrets)
    if len(text) > _MAX_LOGGED_MESSAGE_CHARS:
        return text[:_MAX_LOGGED_MESSAGE_CHARS] + "…"
    return text


def _message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "tool_result":
                    parts.append(f"[tool_result: {item.get('content', '')}]")
                else:
                    parts.append(json.dumps(item, default=str))
            else:
                text = getattr(item, "text", None)
                parts.append(text if isinstance(text, str) else str(item))
        return "\n".join(parts)
    return str(content)


def _render_message(message: dict, extra_secrets: tuple[str, ...] = ()) -> str:
    role = message.get("role", "unknown")
    pieces = [_message_text(message.get("content"))]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        pieces.append(f"[tool_call: {function.get('name')}({function.get('arguments')})]")
    if message.get("reasoning"):
        pieces.append(f"[reasoning: {message['reasoning']}]")
    body = "\n".join(piece for piece in pieces if piece) or "(empty)"
    return f"{role}: {_clip(body, extra_secrets)}"


def log_llm_messages(
    kind: str,
    provider: str,
    messages: list[dict],
    *,
    finish_reason: str | None = None,
    extra_secrets: tuple[str, ...] = (),
) -> None:
    header = f"LLM {kind} provider={provider}"
    if finish_reason:
        header += f" finish_reason={finish_reason}"
    body = "\n".join(_render_message(message, extra_secrets) for message in messages) or "(empty)"
    _llm_log.info("%s\n%s", header, body)


def _openai_output_message(choice) -> dict:
    message = choice.message
    extra = getattr(message, "model_extra", None) or {}
    reasoning = getattr(message, "reasoning_content", None) or extra.get("reasoning_content") or extra.get("reasoning")
    tool_calls = []
    for call in message.tool_calls or []:
        tool_calls.append(
            {"function": {"name": call.function.name, "arguments": call.function.arguments}}
        )
    return {"role": "assistant", "content": message.content or "", "tool_calls": tool_calls, "reasoning": reasoning}


def _http_base_url(value: str) -> str:
    raw = value.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("OPENAI_SPEC_BASE_URL must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("OPENAI_SPEC_BASE_URL must not include a username or password")
    return raw.rstrip("/")


def openai_spec_config() -> dict | None:
    """OpenAI-compatible endpoint. None unless key, base URL, and models are all set."""
    api_key = os.environ.get("OPENAI_SPEC_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_SPEC_BASE_URL", "").strip()
    models = [part.strip() for part in os.environ.get("OPENAI_SPEC_MODELS", "").split(",") if part.strip()]
    if not api_key or not base_url or not models:
        return None
    return {"api_key": api_key, "base_url": _http_base_url(base_url), "models": models}


def openai_client_settings(provider: str, model: str | None = None) -> dict:
    if provider != "openai-spec":
        return {}
    spec = openai_spec_config()
    if spec is None:
        raise ValueError("OpenAI-spec provider needs OPENAI_SPEC_API_KEY, OPENAI_SPEC_BASE_URL, and OPENAI_SPEC_MODELS")
    chosen = model if model in spec["models"] else spec["models"][0]
    return {"model": chosen, "api_key": spec["api_key"], "base_url": spec["base_url"], "name": "openai-spec"}

# KNOWN ISSUE (unresolved): in a real multi-round tool-calling conversation,
# most (not all) `llm` spans for a worker silently never reach Splunk Agent Observability (Galileo) —
# verified repeatedly against the real backend: a 4-6 round conversation
# typically ends up with only 1 surviving `llm` span, while every `tool`
# span and the trace's own input/output are unaffected. Investigated over
# many controlled reproductions (bounding logged payload size, switching to
# each provider's async client, flushing after every span instead of once
# at the end, mode="distributed" instead of the default "batch") — none of
# them fixed it, and several fabricated-data repros using the exact same
# code path never reproduced it at all, so the precise trigger is still
# unknown. `_MAX_LOGGED_TURNS` below bounds what gets logged per span
# regardless, since a growing multi-round payload is bad practice on its
# own merits even though it turned out not to be the cause here — the full
# conversation is reconstructable from the sequence of spans in the trace.
# If you hit this, it's not something wrong with your setup.
_MAX_LOGGED_TURNS = 6


def call_openai(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    name: str = "openai",
):
    from splunk_ao.openai import openai  # auto-logs every call, no decorator needed

    # Sync client on purpose: splunk_ao.openai's wrapper only patches
    # `openai.resources.chat.completions.Completions.create` (confirmed by
    # reading its OPENAI_CLIENT_METHODS list) — there's no entry for
    # AsyncCompletions at all in this installed version. Using AsyncOpenAI
    # here was tried while chasing an unrelated Anthropic issue and was a
    # real regression: unpatched, it forwards `name=` straight to the real
    # API, which rejects it outright (`TypeError: unexpected keyword
    # argument 'name'`) — confirmed via a live 500 in the running app.
    used_key = api_key or os.environ["OPENAI_API_KEY"]
    client_kwargs = {"api_key": used_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    resolved_model = model or OPENAI_MODEL
    endpoint = urlsplit(base_url).hostname if base_url else ("api.openai.com" if name == "openai" else "")
    full_messages = [{"role": "system", "content": system_prompt}, *messages]
    log_llm_use("call", name, resolved_model, endpoint or "")
    log_llm_messages("input", name, full_messages, extra_secrets=(used_key,))
    client = openai.OpenAI(**client_kwargs)
    # `name` is captured by Splunk Agent Observability (Galileo)'s wrapper for the span label and stripped
    # before the real API call — it's not forwarded to OpenAI. The wrapper
    # already reads `model` from these same kwargs for the span's model field.
    response = client.chat.completions.create(
        model=resolved_model, messages=full_messages, tools=tools or None, name=name
    )
    choice = response.choices[0]
    log_llm_messages(
        "output",
        name,
        [_openai_output_message(choice)],
        finish_reason=choice.finish_reason,
        extra_secrets=(used_key,),
    )
    return response


def _anthropic_content_to_log(blocks) -> str:
    """Flatten Anthropic content blocks (text/tool_use/thinking) into readable text.

    @log(span_type="llm")'s generic argument/return-value capture doesn't
    understand Anthropic's block types — a response with a `thinking` block
    fell back to a raw stringified blob instead of a readable message
    (verified against real Splunk Agent Observability (Galileo) trace data). add_llm_span's `output` only
    renders cleanly as a plain string — passing a dict with a list `content`
    gets silently re-stringified inside a wrapper instead of displayed, so
    this returns text, not a structured value.
    """
    parts = []
    for block in blocks:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "tool_use":
            parts.append(f"[tool_use: {block.name}({json.dumps(block.input)})]")
        elif block.type == "thinking" and block.thinking:
            parts.append(f"[thinking: {block.thinking}]")
    return "\n".join(parts)


def call_anthropic(messages: list[dict], tools: list[dict], system_prompt: str):
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    log_llm_use("call", "anthropic", ANTHROPIC_MODEL, "api.anthropic.com")
    log_llm_messages("input", "anthropic", [{"role": "system", "content": system_prompt}, *messages])
    start = time.time()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        # 1024 was too tight: found a real case where a worker's final round
        # (no tool_use, should be the answer text) came back completely
        # empty on a demanding synthesis-style question, which fed an empty
        # string into that worker's result. Sonnet 5's own reasoning before
        # answering can eat into a small budget before any visible text is
        # emitted; 4096 gives real headroom.
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
        tools=tools or [],
    )

    # Logged by hand via add_llm_span (the same primitive @log calls
    # internally) instead of @log(span_type="llm"): that gave input as one
    # big stringified dict of every function argument (including
    # system_prompt as a stray key, not as part of the conversation) rather
    # than a clean message list, since it doesn't know Anthropic keeps
    # `system` separate from `messages`. The real API call above still gets
    # the full `messages` history; only the logged copy is bounded.
    splunk_ao_context.get_logger_instance().add_llm_span(
        input=[{"role": "system", "content": system_prompt}, *messages[-_MAX_LOGGED_TURNS:]],
        output=_anthropic_content_to_log(response.content),
        model=ANTHROPIC_MODEL,
        name="anthropic",
        tools=tools or None,
        num_input_tokens=response.usage.input_tokens,
        num_output_tokens=response.usage.output_tokens,
        duration_ns=int((time.time() - start) * 1e9),
    )
    log_llm_messages(
        "output",
        "anthropic",
        [{"role": "assistant", "content": _anthropic_content_to_log(response.content)}],
    )
    return response


def _gemini_part_to_text(part) -> str:
    if part.text is not None:
        return part.text
    if part.function_call is not None:
        return f"[function_call: {part.function_call.name}({json.dumps(dict(part.function_call.args or {}))})]"
    if part.function_response is not None:
        return f"[function_response: {part.function_response.name} -> {json.dumps(dict(part.function_response.response or {}))}]"
    return f"[{type(part).__name__}]"


def _gemini_content_text(content) -> str:
    # add_llm_span's message-list schema wants {"role": ..., "content": <str>}
    # per entry — verified against real Splunk Agent Observability (Galileo) data that {"role": ...,
    # "parts": [...]} isn't recognized: every entry's role silently collapsed
    # to "user" and the whole dict got re-stringified into a "content" field
    # instead of being displayed. Flattening parts to text up front avoids
    # that entirely, for both input entries and the final output.
    if content is None:
        return ""
    return "\n".join(_gemini_part_to_text(part) for part in content.parts)


def call_gemini(contents: list, tools: list[dict], system_prompt: str):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    log_llm_use("call", "gemini", GEMINI_MODEL, "generativelanguage.googleapis.com")
    log_llm_messages(
        "input",
        "gemini",
        [
            {"role": "system", "content": system_prompt},
            *[
                {"role": "assistant" if item.role == "model" else item.role, "content": _gemini_content_text(item)}
                for item in contents
            ],
        ],
    )
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=tools)] if tools else None,
    )
    start = time.time()
    response = client.models.generate_content(model=GEMINI_MODEL, contents=contents, config=config)

    # Same reasoning as call_anthropic: log by hand so system_prompt shows up
    # as part of the conversation (Gemini keeps it out of `contents` too) and
    # so the response renders as text/function-call parts instead of an
    # opaque blob. Gemini's own role name for its turns is "model" — not a
    # role Splunk Agent Observability (Galileo) recognizes (verified: that entry alone got wrapped and
    # re-stringified, role silently defaulted to "user") — mapped to the
    # conventional "assistant" here.
    # The real API call above gets the full `contents` history; only the
    # logged copy is bounded (see _MAX_LOGGED_TURNS comment above).
    logged_input = [
        {"role": "system", "content": system_prompt},
        *[
            {"role": "assistant" if c.role == "model" else c.role, "content": _gemini_content_text(c)}
            for c in contents[-_MAX_LOGGED_TURNS:]
        ],
    ]
    logged_output = _gemini_content_text(response.candidates[0].content) if response.candidates else (response.text or "")
    usage = response.usage_metadata
    splunk_ao_context.get_logger_instance().add_llm_span(
        input=logged_input,
        output=logged_output,
        model=GEMINI_MODEL,
        name="gemini",
        tools=tools or None,
        num_input_tokens=usage.prompt_token_count if usage else None,
        num_output_tokens=usage.candidates_token_count if usage else None,
        duration_ns=int((time.time() - start) * 1e9),
    )
    log_llm_messages("output", "gemini", [{"role": "assistant", "content": logged_output}])
    return response


@log(span_type="tool")
async def call_splunk_tool(tool_name: str, arguments: dict) -> str:
    return await mcp_client.call_tool(_mcp_session.get(), tool_name, arguments)


async def run_traced_turn(
    user_message: str,
    conversation_id: str,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    from app.agent import _needs_splunk, run_agent_turn

    history = prior_turns(conversation_id)
    use_splunk = _needs_splunk(user_message, history)
    async with _o11y_lock:
        with activate_o11y():
            with splunk_ao_context(
                project=ao_project(),
                agent_stream=ao_agent_stream(),
                session_id=_ao_session_id(conversation_id),
            ):
                # Without an explicit start_trace/conclude, the SDK lazily creates the
                # trace from whichever child span happens to log first — so the trace's
                # own input/output end up being an arbitrary tool call or LLM message
                # list instead of the actual user question and final answer.
                logger = splunk_ao_context.get_logger_instance()
                logger.start_trace(input=user_message)

                if use_splunk:
                    async with mcp_client.splunk_mcp_session() as session:
                        set_mcp_session(session)
                        tools = await mcp_client.list_splunk_tools(session)
                        result = await run_agent_turn(
                            user_message,
                            tools,
                            provider=provider,
                            history=history,
                            model=model,
                        )
                else:
                    mcp_client.log_mcp_skipped()
                    result = await run_agent_turn(
                        user_message,
                        [],
                        provider=provider,
                        history=history,
                        model=model,
                        use_splunk=False,
                    )

                logger.conclude(output=result)
                splunk_ao_context.flush()
                remember_turn(conversation_id, user_message, result)
                return result
