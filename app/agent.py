"""Supervisor -> classifier -> scoped worker(s): the agent structure for one chat turn.

For each turn: a "supervisor" agent span wraps the whole thing. Inside it, a
"classifier" agent span (a fast keyword heuristic, not an LLM call — keeps
this deterministic and free of extra API cost/latency) picks one or more
categories (security/infra/general) — a prompt can touch more than one, e.g.
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
span) and feed the result back -> otherwise return its final text. Two
safety nets, independent of which provider/category is running:
- MAX_TURNS caps the number of rounds.
- A repeated-identical-tool-call guard stops if the model calls the exact
  same tool with the exact same arguments again later in the same turn — a
  common real loop failure mode, faster to catch than waiting for the turn cap.
Either one tripping sets status_code=1 on the worker's agent span when it
concludes, so a stuck turn is visible/filterable in Splunk Agent Observability (Galileo) rather than
just a silent fallback message in the chat.

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
import os
import re

from galileo import galileo_context, log

from app import observability

MAX_TURNS = 8

BASE_SYSTEM_PROMPT = """\
You are an AI assistant helping a workshop participant explore their Splunk \
environment through Splunk MCP tools. Prefer splunk_run_query with explicit \
SPL over any other search-generation tool. Be concise and cite concrete \
numbers or index names from the tool results in your answer."""

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

CATEGORY_PROMPTS = {"security": SECURITY_SYSTEM_PROMPT, "infra": INFRA_SYSTEM_PROMPT, "general": GENERAL_SYSTEM_PROMPT}

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
    "general": None,  # every tool except EXCLUDED_TOOLS
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


def _matches_any(text: str, keywords: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(keyword)}\b", text, flags=re.IGNORECASE) for keyword in keywords)


@log(span_type="agent", name="classifier", params={"agent_type": "classifier"})
def _classify(user_message: str) -> list[str]:
    categories = []
    if _matches_any(user_message, SECURITY_KEYWORDS):
        categories.append("security")
    if _matches_any(user_message, INFRA_KEYWORDS):
        categories.append("infra")
    return categories or ["general"]


def _scoped_tools(mcp_tools: list[dict], category: str) -> list[dict]:
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
) -> str:
    provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
    history = list(history or [])
    logger = galileo_context.get_logger_instance()

    logger.add_agent_span(input=user_message, name="supervisor", agent_type="supervisor")

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

    for _ in range(MAX_TURNS):
        response = observability.call_openai(messages, tools, system_prompt, **(client_settings or {}))
        message = response.choices[0].message
        if not message.tool_calls:
            if not message.content:
                return _empty_answer_message(), 1
            return message.content, 0

        messages.append(message.model_dump(exclude_unset=True))
        for tool_call in message.tool_calls:
            arguments = json.loads(tool_call.function.arguments or "{}")
            call_key = (tool_call.function.name, json.dumps(arguments, sort_keys=True))
            if call_key in seen_calls:
                return _repeated_call_message(tool_call.function.name), 1
            seen_calls.add(call_key)

            result = await observability.call_splunk_tool(tool_call.function.name, arguments)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})

    return _turn_limit_message(), 1


async def _anthropic_loop(
    user_message: str, mcp_tools: list[dict], system_prompt: str, history: list[dict]
) -> tuple[str, int]:
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in mcp_tools]
    messages = [*history, {"role": "user", "content": user_message}]
    seen_calls: set[tuple[str, str]] = set()

    for _ in range(MAX_TURNS):
        response = observability.call_anthropic(messages, tools, system_prompt)
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            text = "".join(block.text for block in response.content if block.type == "text")
            if not text:
                return _empty_answer_message(), 1
            return text, 0

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_uses:
            call_key = (block.name, json.dumps(block.input, sort_keys=True))
            if call_key in seen_calls:
                return _repeated_call_message(block.name), 1
            seen_calls.add(call_key)

            result = await observability.call_splunk_tool(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    return _turn_limit_message(), 1


async def _gemini_loop(
    user_message: str, mcp_tools: list[dict], system_prompt: str, history: list[dict]
) -> tuple[str, int]:
    from google.genai import types

    tools = [
        {"name": t["name"], "description": t["description"], "parameters_json_schema": t["input_schema"]} for t in mcp_tools
    ]
    contents = _gemini_contents(history, user_message)
    seen_calls: set[tuple[str, str]] = set()

    for _ in range(MAX_TURNS):
        response = observability.call_gemini(contents, tools, system_prompt)
        calls = response.function_calls or []
        if not calls:
            if not response.text:
                return _empty_answer_message(), 1
            return response.text, 0

        contents.append(response.candidates[0].content)
        result_parts = []
        for call in calls:
            args = dict(call.args or {})
            call_key = (call.name, json.dumps(args, sort_keys=True))
            if call_key in seen_calls:
                return _repeated_call_message(call.name), 1
            seen_calls.add(call_key)

            result = await observability.call_splunk_tool(call.name, args)
            result_parts.append(types.Part.from_function_response(name=call.name, response={"result": result}))
        contents.append(types.Content(role="user", parts=result_parts))

    return _turn_limit_message(), 1


def _empty_answer_message() -> str:
    return (
        "The model finished without producing a final answer — this can happen on a demanding "
        "question if it runs out of response budget. Try asking again or narrowing the question."
    )


def _repeated_call_message(tool_name: str) -> str:
    return (
        f"Stopped after detecting a repeated identical call to {tool_name} — the model may be stuck. "
        "Try rephrasing your question."
    )


def _turn_limit_message() -> str:
    return "Reached the tool-call limit for this turn without a final answer."
