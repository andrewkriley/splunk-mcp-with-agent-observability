"""Splunk Agent Observability (Galileo) instrumentation for the workshop chat agent.

Splunk Agent Observability (Galileo) only ships a native import-swap wrapper for OpenAI (`galileo.openai`)
— it auto-logs every call, no decorator needed. Anthropic and Gemini have no
such wrapper, so those calls build their span by hand via
`GalileoLogger.add_llm_span(...)` — not the `@log(span_type="llm")`
decorator, which generically dumps every function argument (including
`system_prompt` as a stray key, and provider-specific response shapes like
Anthropic's `thinking` blocks) into the span rather than a clean message
list. Splunk MCP tool calls still use `@log(span_type="tool")`, which
doesn't have this problem — its input/output are already simple. The whole
turn is wrapped in one `galileo_context` so every LLM/tool span lands in a
single trace.
"""

import contextvars
import json
import logging
import os
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from galileo import galileo_context, log, start_session
from galileo.constants import DEFAULT_CONSOLE_URL

from app import mcp_client

# Task-local so two overlapping /chat requests cannot share a Splunk session.
_mcp_session: contextvars.ContextVar = contextvars.ContextVar("splunk_mcp_session")
_galileo_sessions: dict[str, str] = {}  # conversation_id -> Splunk Agent Observability (Galileo) session_id, so every
                                          # turn in one browser conversation lands in one session

# Prior user/assistant text only. Tool transcripts stay inside the turn that made them.
MAX_HISTORY_TURNS = 8
MAX_CONVERSATIONS = 100
_history: OrderedDict[str, list[dict]] = OrderedDict()


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


def galileo_console_url() -> str:
    """Console to log to. Blank GALILEO_CONSOLE_URL keeps the SDK default."""
    configured = os.environ.get("GALILEO_CONSOLE_URL", "").strip()
    if not configured:
        return str(DEFAULT_CONSOLE_URL).rstrip("/")

    value = configured if "://" in configured else f"https://{configured}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("GALILEO_CONSOLE_URL must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("GALILEO_CONSOLE_URL must not include a username or password")
    return value.rstrip("/")


def apply_galileo_console_url() -> str:
    """Publish a validated override before the SDK reads GALILEO_CONSOLE_URL."""
    url = galileo_console_url()
    if os.environ.get("GALILEO_CONSOLE_URL", "").strip():
        os.environ["GALILEO_CONSOLE_URL"] = url
    return url


def _galileo_session_id(conversation_id: str) -> str:
    if conversation_id not in _galileo_sessions:
        _galileo_sessions[conversation_id] = start_session(name=f"workshop-chat-{conversation_id}")
    return _galileo_sessions[conversation_id]


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
    for secret in [os.environ.get(name, "").strip() for name in _SECRET_ENV_VARS]:
        if secret:
            text = text.replace(secret, "[redacted]")
    for secret in extra:
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
    from galileo.openai import openai  # auto-logs every call, no decorator needed

    # Sync client on purpose: galileo.openai's wrapper only patches
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
    galileo_context.get_logger_instance().add_llm_span(
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
    galileo_context.get_logger_instance().add_llm_span(
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
    user_message: str, conversation_id: str, provider: str | None = None, model: str | None = None
) -> str:
    from app.agent import _needs_splunk, run_agent_turn

    apply_galileo_console_url()
    history = prior_turns(conversation_id)
    use_splunk = _needs_splunk(user_message, history)
    with galileo_context(
        project=os.environ.get("GALILEO_PROJECT", "splunk-mcp-with-agent-observability"),
        log_stream=os.environ.get("GALILEO_LOG_STREAM", "default"),
        session_id=_galileo_session_id(conversation_id),
    ):
        # Without an explicit start_trace/conclude, Splunk Agent Observability (Galileo) lazily creates the
        # trace from whichever child span happens to log first — so the trace's
        # own input/output end up being an arbitrary tool call or LLM message
        # list instead of the actual user question and final answer.
        logger = galileo_context.get_logger_instance()
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
        galileo_context.flush()
        remember_turn(conversation_id, user_message, result)
        return result
