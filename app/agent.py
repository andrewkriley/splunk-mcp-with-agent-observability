"""Supervisor -> classifier -> scoped worker(s): the agent structure for one chat turn.

For each turn: a "supervisor" agent span wraps the whole thing. Whether the
question needs Splunk is decided by SPLUNK_INTENT_MODE: the static keyword
list, a yes/no call to the selected model, or both (the list wins on a hit,
and the model is asked only when the list misses). Workshop default is
model so misspellings such as noteables still open MCP. If it is not a
Splunk question, the turn is one chat worker
with no tools and Splunk MCP is not opened. If it is, a "classifier" agent
span (the same keyword heuristic, not an LLM call — keeps this deterministic
and free of extra API cost/latency) picks one or more categories
(security/infra/general) — a prompt can touch more than one, e.g.
"compare security notables with PDU power draw". Each matched category gets
its own "react" worker agent span with a scoped system prompt and tool
subset. If more than one category matched, a final synthesis LLM call (no
tools) combines the workers' findings into one answer; with just one
category, that worker's own answer is returned directly, no extra call.
Prior user and assistant turns for this browser conversation are included
in each worker's messages, capped, so a follow-up can refer to the previous
answer. In Splunk Agent Observability (Galileo) this renders as:

    trace -> agent(supervisor) -> [agent(classifier), agent(react) -> [llm, tool, ...], agent(react) -> [...], llm(synthesis)?]

Each loop repeats: call the LLM with the tools on offer -> if it asks for a
tool call, run it via observability.call_splunk_tool (a Splunk Agent Observability (Galileo) `tool`
span) and feed the result back -> otherwise return its final text. Safety
nets, independent of which provider/category is running:
- MAX_TURNS caps the number of rounds; the last round offers no tools so the
  model must answer from results it already has.
- MAX_SPLUNK_QUERIES caps splunk_run_query calls per worker.
- A repeated-call guard (whitespace-normalized SPL) stops identical searches.
- Large tool results are truncated before they re-enter the prompt.
When any of those controls fire, the user-visible reply gets a short comfort
note. Either a hard stop with no answer still sets status_code=1 on the
worker agent span so it is visible in Splunk Agent Observability (Galileo).

The LLM calls (call_openai/call_anthropic/call_gemini) are plain sync
functions, called directly — not via asyncio.to_thread (confirmed broken:
resolves Splunk Agent Observability (Galileo)'s logger to a different object with no active trace,
silently dropping every LLM span) and not via each provider's async client
either (tried while chasing the KNOWN ISSUE below; broke OpenAI outright,
since galileo.openai's wrapper only patches the sync client — confirmed via
a live 500: `AsyncCompletions.create() got an unexpected keyword argument
'name'` — and didn't fix the issue for Anthropic/Gemini anyway). Calling
them synchronously blocks the event loop for the duration of each request,
an acceptable tradeoff for this single-user demo.

See the KNOWN ISSUE comment in observability.py: a real multi-round
conversation still tends to lose most (not all) `llm` spans in Splunk Agent Observability (Galileo) —
every `tool` span and the trace's own input/output are unaffected, and this
has not been root-caused despite extensive investigation. It's an
observability gap only; the chat app's answers are correct regardless.
"""

import json
import logging
import os
import re

from galileo import galileo_context, log

from app import observability, search_controls

MAX_TURNS = 8

BASE_SYSTEM_PROMPT = """\
You are an AI assistant helping a workshop participant explore their Splunk \
environment through Splunk MCP tools. Prefer splunk_run_query with explicit \
SPL over any other search-generation tool. Use at most two or three tool \
calls. Prefer one focused splunk_run_query with `| head 20` or `stats` over \
browsing indexes. After a useful non-empty result, stop and answer. Do not \
keep searching to refine unless the result is empty or an error. Indexes on \
this instance are named in this prompt — you do not need to list them unless \
asked. Be concise and cite concrete numbers or index names from the tool \
results in your answer."""

SECURITY_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + """

You specialize in oidemo_notable: Splunk Enterprise Security notable events \
(security alerts) — brute force, authentication failures, malware, audit \
events. This data is sourcetype=stash: fields like severity aren't cleanly \
extracted — search for the literal uppercase string, e.g. "severity=HIGH" \
(not severity=high), as a raw text match rather than a field filter."""
)

INFRA_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + """

You specialize in oidemo: IT/datacenter operations telemetry — PDU power \
draw (Amps/Volts/W), CRAC cooling unit temperatures, and Windows/Exchange \
Perfmon counters."""
)

GENERAL_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + """

Known indexes on this instance: oidemo (IT/datacenter telemetry),
oidemo_notable (security notable events, sourcetype=stash — search
"severity=HIGH" as literal text, not a field filter), and main (general
sample data). Use whichever fits the question."""
)

CHAT_SYSTEM_PROMPT = """\
You are an AI assistant for a workshop participant. Answer from the \
conversation. Splunk tools are not available for this question, so answer \
directly and do not invent Splunk search results."""

CATEGORY_PROMPTS = {
    "security": SECURITY_SYSTEM_PROMPT,
    "infra": INFRA_SYSTEM_PROMPT,
    "general": GENERAL_SYSTEM_PROMPT,
    "chat": CHAT_SYSTEM_PROMPT,
}

SYNTHESIS_SYSTEM_PROMPT = """\
You are combining findings from multiple specialized Splunk sub-agents into \
one clear, unified answer for the user. Each sub-agent already gathered \
real data for its part of the question — do not call any tools, just \
synthesize their findings into a single well-organized answer that directly \
addresses the user's original question."""

# saia_* tools call a separate Splunk AI Assistant backend that reliably
# returns server errors on this instance (confirmed) — never offered.
EXCLUDED_TOOLS = {"saia_generate_spl", "saia_explain_spl", "saia_ask_splunk_question", "saia_optimize_spl"}
# Account inventory is not part of answering a general data question.
GENERAL_EXCLUDED_TOOLS = EXCLUDED_TOOLS | {"splunk_get_user_list"}

CATEGORY_TOOL_NAMES = {
    "security": {"splunk_run_query", "splunk_get_metadata", "splunk_get_index_info"},
    "infra": {"splunk_run_query", "splunk_get_metadata", "splunk_get_index_info", "splunk_get_kv_store_collections"},
    "general": {"splunk_run_query", "splunk_get_metadata", "splunk_get_index_info"},
}

SECURITY_KEYWORDS = [
    "security", "notable", "notables", "brute force", "attack", "attacks", "malware", "threat", "threats",
    "breach", "intrusion", "suspicious", "audit", "login", "logins", "authentication", "unauthorized",
    "severity", "keylogger", "hack",
]
INFRA_KEYWORDS = [
    "pdu", "power", "cooling", "crac", "temperature", "perfmon", "datacenter", "exchange",
    "infrastructure", "hardware", "amps", "volts",
]
# Security and infra words, plus explicit Splunk words. A question that matches
# none of these, and whose recent user turns also match none, does not open MCP.
SPLUNK_INTENT_KEYWORDS = [
    *SECURITY_KEYWORDS,
    *INFRA_KEYWORDS,
    "splunk",
    "index",
    "indexes",
    "search",
    "oidemo",
    "infra",
    "security",
    "threats",
    "events",
]


INTENT_MODES = ("keywords", "model", "both")
INTENT_ROUTER_SYSTEM = """\
You decide if a workshop question needs a live search of the user's Splunk data.
Reply with one word: yes or no.
Say yes for indexes, events, metrics, security alerts, or notable events, including misspellings such as noteables for notables.
Say no for greetings, questions about you, or anything that does not need their Splunk data.
"""

_intent_log = logging.getLogger("app.llm")


def intent_mode() -> str:
    """keywords, model, or both. An unknown value stays on the static list."""
    mode = os.environ.get("SPLUNK_INTENT_MODE", "keywords").strip().lower()
    return mode if mode in INTENT_MODES else "keywords"


def _matches_any(text: str, keywords: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(keyword)}\b", text, flags=re.IGNORECASE) for keyword in keywords)


def _keyword_needs_splunk(user_message: str, history: list[dict] | None = None) -> bool:
    texts = [user_message]
    for message in history or []:
        if message.get("role") == "user":
            texts.append(message.get("content") or "")
    return any(_matches_any(text, SPLUNK_INTENT_KEYWORDS) for text in texts)


def _parse_yes_no(text: str) -> bool | None:
    match = re.match(r"\s*(yes|no)\b", text, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"\b(yes|no)\b", text, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).lower() == "yes"


def _router_text(provider: str, model: str | None, question: str) -> str:
    """One yes/no completion from the selected provider. No tools."""
    if provider in {"openai", "openai-spec"}:
        settings = observability.openai_client_settings(provider, model)
        response = observability.call_openai(
            [{"role": "user", "content": question}],
            [],
            INTENT_ROUTER_SYSTEM,
            model=settings.get("model"),
            api_key=settings.get("api_key"),
            base_url=settings.get("base_url"),
            name=settings.get("name", "openai"),
        )
        logged = observability._openai_output_message(response.choices[0])
        return logged["content"] or logged.get("reasoning") or ""
    if provider == "gemini":
        from google.genai import types

        response = observability.call_gemini(
            [types.Content(role="user", parts=[types.Part.from_text(text=question)])],
            [],
            INTENT_ROUTER_SYSTEM,
        )
        return response.text or ""
    response = observability.call_anthropic(
        [{"role": "user", "content": question}],
        [],
        INTENT_ROUTER_SYSTEM,
    )
    return observability._anthropic_content_to_log(response.content)


def _model_needs_splunk(
    user_message: str,
    history: list[dict] | None,
    provider: str | None,
    model: str | None,
) -> bool | None:
    selected = (provider or os.environ.get("LLM_PROVIDER", "anthropic")).strip().lower()
    lines = [user_message]
    for message in history or []:
        if message.get("role") == "user" and message.get("content"):
            lines.append(message["content"])
    question = "\n".join(f"User: {text}" for text in lines[:4])
    return _parse_yes_no(_router_text(selected, model, question))


def _log_intent(mode: str, source: str, open_splunk: bool, **detail: str) -> None:
    extra = " ".join(f"{key}={value}" for key, value in detail.items())
    _intent_log.info(
        "Intent source=%s mode=%s decision=%s%s",
        source,
        mode,
        "open" if open_splunk else "skip",
        f" {extra}" if extra else "",
    )


def _needs_splunk(
    user_message: str,
    history: list[dict] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    """True when this question should open Splunk MCP."""
    mode = intent_mode()
    keyword_hit = _keyword_needs_splunk(user_message, history)
    keywords = "hit" if keyword_hit else "miss"
    if mode == "keywords" or (mode == "both" and keyword_hit):
        _log_intent(mode, "keyword-list", keyword_hit, keywords=keywords)
        return keyword_hit
    try:
        decision = _model_needs_splunk(user_message, history, provider, model)
    except Exception as exc:
        _log_intent(mode, "keyword-list", keyword_hit, keywords=keywords, reason="router-error", error=str(exc))
        return keyword_hit
    if decision is None:
        _log_intent(mode, "keyword-list", keyword_hit, keywords=keywords, reason="router-unparsed")
        return keyword_hit
    _log_intent(mode, "model-router", decision, keywords=keywords)
    return decision


@log(span_type="agent", name="classifier", params={"agent_type": "classifier"})
def _classify(user_message: str) -> list[str]:
    categories = []
    if _matches_any(user_message, SECURITY_KEYWORDS):
        categories.append("security")
    if _matches_any(user_message, INFRA_KEYWORDS):
        categories.append("infra")
    return categories or ["general"]


def _scoped_tools(mcp_tools: list[dict], category: str) -> list[dict]:
    if category == "chat":
        return []
    allowed = CATEGORY_TOOL_NAMES.get(category)
    excluded = EXCLUDED_TOOLS if allowed is not None else GENERAL_EXCLUDED_TOOLS
    return [t for t in mcp_tools if t["name"] not in excluded and (allowed is None or t["name"] in allowed)]


async def _run_worker(
    user_message: str,
    mcp_tools: list[dict],
    provider: str,
    category: str,
    history: list[dict],
    model: str | None = None,
) -> tuple[str, int]:
    logger = galileo_context.get_logger_instance()
    system_prompt = CATEGORY_PROMPTS[category]
    scoped_tools = _scoped_tools(mcp_tools, category)

    logger.add_agent_span(input=user_message, name=f"{category}_worker", agent_type="react")
    if provider in {"openai", "openai-spec"}:
        result, status_code = await _openai_loop(
            user_message,
            scoped_tools,
            system_prompt,
            history,
            client_settings=observability.openai_client_settings(provider, model),
        )
    elif provider == "gemini":
        result, status_code = await _gemini_loop(user_message, scoped_tools, system_prompt, history)
    else:
        result, status_code = await _anthropic_loop(user_message, scoped_tools, system_prompt, history)
    logger.conclude(output=result, status_code=status_code)  # closes this worker span
    return result, status_code


async def run_agent_turn(
    user_message: str,
    mcp_tools: list[dict],
    provider: str | None = None,
    history: list[dict] | None = None,
    model: str | None = None,
    use_splunk: bool = True,
) -> str:
    search_controls.reset_controls()
    provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
    history = list(history or [])
    selected_provider, selected_model, endpoint = observability.describe_llm(provider, model)
    observability.log_llm_use("selected", selected_provider, selected_model, endpoint)
    logger = galileo_context.get_logger_instance()

    logger.add_agent_span(input=user_message, name="supervisor", agent_type="supervisor")

    if not use_splunk:
        result, status_code = await _run_worker(user_message, [], provider, "chat", history, model)
        result = search_controls.with_comfort_notes(result)
        logger.conclude(output=result, status_code=status_code)
        return result

    categories = _classify(user_message)

    worker_results: dict[str, str] = {}
    worst_status = 0
    for category in categories:
        result, status_code = await _run_worker(user_message, mcp_tools, provider, category, history, model)
        worker_results[category] = result
        worst_status = max(worst_status, status_code)

    if len(worker_results) == 1:
        final = next(iter(worker_results.values()))
    else:
        final = await _synthesize(user_message, worker_results, provider, history, model)

    final = search_controls.with_comfort_notes(final)
    logger.conclude(output=final, status_code=worst_status)  # closes the supervisor span
    return final


async def _synthesize(
    user_message: str,
    worker_results: dict[str, str],
    provider: str,
    history: list[dict],
    model: str | None = None,
) -> str:
    findings = "\n\n".join(f"## {category.title()} findings\n{text}" for category, text in worker_results.items())
    prompt = f"Original question: {user_message}\n\n{findings}"
    messages = [*history, {"role": "user", "content": prompt}]

    await observability.announce("model")
    if provider in {"openai", "openai-spec"}:
        response = observability.call_openai(
            messages, [], SYNTHESIS_SYSTEM_PROMPT, **observability.openai_client_settings(provider, model)
        )
        return response.choices[0].message.content or findings
    if provider == "gemini":
        contents = _gemini_contents(history, prompt)
        response = observability.call_gemini(contents, [], SYNTHESIS_SYSTEM_PROMPT)
        return response.text or findings
    response = observability.call_anthropic(messages, [], SYNTHESIS_SYSTEM_PROMPT)
    return "".join(block.text for block in response.content if block.type == "text") or findings


def _gemini_contents(history: list[dict], user_message: str) -> list:
    from google.genai import types

    contents = []
    for message in history:
        role = "model" if message["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=message["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))
    return contents


def _tools_for_round(round_i: int, query_count: int, tools: list, had_tool_result: bool) -> list:
    if round_i < MAX_TURNS - 1 and query_count < search_controls.MAX_SPLUNK_QUERIES:
        return tools
    if had_tool_result:
        if query_count >= search_controls.MAX_SPLUNK_QUERIES:
            search_controls.note_control("query_cap")
        if round_i >= MAX_TURNS - 1:
            search_controls.note_control("forced_final")
    return []


async def _invoke_splunk_tools(
    declared: list[tuple[str, dict, object]],
    seen_calls: set[tuple[str, str]],
    query_count: int,
) -> tuple[list[tuple[object, str]], int, str | None]:
    results: list[tuple[object, str]] = []
    for name, arguments, handle in declared:
        call_key = search_controls.fingerprint(name, arguments)
        if call_key in seen_calls:
            search_controls.note_control("repeat")
            return results, query_count, "repeat"
        seen_calls.add(call_key)
        if search_controls.is_query_tool(name):
            if query_count >= search_controls.MAX_SPLUNK_QUERIES:
                search_controls.note_control("query_cap")
                return results, query_count, "query_cap"
            query_count += 1
        await observability.announce("tool", name)
        text = await observability.call_splunk_tool(name, arguments)
        results.append((handle, text))
    return results, query_count, None


def _skipped_tool_content(reason: str) -> str:
    if reason == "repeat":
        return "Skipped: repeated identical Splunk search."
    return "Skipped: Splunk search budget for this turn is used."


async def _openai_final(messages: list[dict], system_prompt: str, client_settings: dict | None) -> tuple[str, int]:
    await observability.announce("model")
    response = observability.call_openai(messages, [], system_prompt, **(client_settings or {}))
    message = response.choices[0].message
    if message.content:
        return message.content, 0
    return _empty_answer_message(), 1


async def _openai_loop(
    user_message: str,
    mcp_tools: list[dict],
    system_prompt: str,
    history: list[dict],
    client_settings: dict | None = None,
) -> tuple[str, int]:
    tools = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in mcp_tools
    ]
    messages = [*history, {"role": "user", "content": user_message}]
    seen_calls: set[tuple[str, str]] = set()
    query_count = 0
    had_tool_result = False

    for round_i in range(MAX_TURNS):
        offer = _tools_for_round(round_i, query_count, tools, had_tool_result)
        await observability.announce("model")
        response = observability.call_openai(messages, offer, system_prompt, **(client_settings or {}))
        message = response.choices[0].message
        if not message.tool_calls:
            if not message.content:
                return _empty_answer_message(), 1
            return message.content, 0
        if not offer:
            if message.content:
                return message.content, 0
            return _empty_answer_message(), 1

        messages.append(message.model_dump(exclude_unset=True))
        declared = []
        for tool_call in message.tool_calls:
            arguments = json.loads(tool_call.function.arguments or "{}")
            declared.append((tool_call.function.name, arguments, tool_call.id))
        results, query_count, stop = await _invoke_splunk_tools(declared, seen_calls, query_count)
        had_tool_result = had_tool_result or bool(results)
        for handle, text in results:
            messages.append({"role": "tool", "tool_call_id": handle, "content": text})
        if stop:
            for name, arguments, handle in declared[len(results) :]:
                messages.append(
                    {"role": "tool", "tool_call_id": handle, "content": _skipped_tool_content(stop)}
                )
            search_controls.note_control("forced_final")
            return await _openai_final(messages, system_prompt, client_settings)

    search_controls.note_control("forced_final")
    return await _openai_final(messages, system_prompt, client_settings)


async def _anthropic_final(messages: list[dict], system_prompt: str) -> tuple[str, int]:
    await observability.announce("model")
    response = observability.call_anthropic(messages, [], system_prompt)
    text = "".join(block.text for block in response.content if block.type == "text")
    if text:
        return text, 0
    return _empty_answer_message(), 1


async def _anthropic_loop(
    user_message: str, mcp_tools: list[dict], system_prompt: str, history: list[dict]
) -> tuple[str, int]:
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in mcp_tools]
    messages = [*history, {"role": "user", "content": user_message}]
    seen_calls: set[tuple[str, str]] = set()
    query_count = 0
    had_tool_result = False

    for round_i in range(MAX_TURNS):
        offer = _tools_for_round(round_i, query_count, tools, had_tool_result)
        await observability.announce("model")
        response = observability.call_anthropic(messages, offer, system_prompt)
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            text = "".join(block.text for block in response.content if block.type == "text")
            if not text:
                return _empty_answer_message(), 1
            return text, 0
        if not offer:
            text = "".join(block.text for block in response.content if block.type == "text")
            if text:
                return text, 0
            return _empty_answer_message(), 1

        messages.append({"role": "assistant", "content": response.content})
        declared = [(block.name, dict(block.input or {}), block.id) for block in tool_uses]
        results, query_count, stop = await _invoke_splunk_tools(declared, seen_calls, query_count)
        had_tool_result = had_tool_result or bool(results)
        tool_results = [{"type": "tool_result", "tool_use_id": handle, "content": text} for handle, text in results]
        if stop:
            for name, arguments, handle in declared[len(results) :]:
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": handle, "content": _skipped_tool_content(stop)}
                )
        messages.append({"role": "user", "content": tool_results})
        if stop:
            search_controls.note_control("forced_final")
            return await _anthropic_final(messages, system_prompt)

    search_controls.note_control("forced_final")
    return await _anthropic_final(messages, system_prompt)


async def _gemini_final(contents: list, system_prompt: str) -> tuple[str, int]:
    await observability.announce("model")
    response = observability.call_gemini(contents, [], system_prompt)
    if response.text:
        return response.text, 0
    return _empty_answer_message(), 1


async def _gemini_loop(
    user_message: str, mcp_tools: list[dict], system_prompt: str, history: list[dict]
) -> tuple[str, int]:
    from google.genai import types

    tools = [
        {"name": t["name"], "description": t["description"], "parameters_json_schema": t["input_schema"]} for t in mcp_tools
    ]
    contents = _gemini_contents(history, user_message)
    seen_calls: set[tuple[str, str]] = set()
    query_count = 0
    had_tool_result = False

    for round_i in range(MAX_TURNS):
        offer = _tools_for_round(round_i, query_count, tools, had_tool_result)
        await observability.announce("model")
        response = observability.call_gemini(contents, offer, system_prompt)
        calls = response.function_calls or []
        if not calls:
            if not response.text:
                return _empty_answer_message(), 1
            return response.text, 0
        if not offer:
            if response.text:
                return response.text, 0
            return _empty_answer_message(), 1

        contents.append(response.candidates[0].content)
        declared = [(call.name, dict(call.args or {}), call.name) for call in calls]
        results, query_count, stop = await _invoke_splunk_tools(declared, seen_calls, query_count)
        had_tool_result = had_tool_result or bool(results)
        result_parts = [
            types.Part.from_function_response(name=handle, response={"result": text}) for handle, text in results
        ]
        if stop:
            for name, arguments, handle in declared[len(results) :]:
                result_parts.append(
                    types.Part.from_function_response(
                        name=handle, response={"result": _skipped_tool_content(stop)}
                    )
                )
        contents.append(types.Content(role="user", parts=result_parts))
        if stop:
            search_controls.note_control("forced_final")
            return await _gemini_final(contents, system_prompt)

    search_controls.note_control("forced_final")
    return await _gemini_final(contents, system_prompt)


def _empty_answer_message() -> str:
    return (
        "The model finished without producing a final answer — this can happen on a demanding "
        "question if it runs out of response budget. Try asking again or narrowing the question."
    )
