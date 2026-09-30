# The chat app

A working reference chat app — run it with:

```
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 and ask it something about `oidemo` or
`oidemo_notable` (see the [main README](../README.md#data-available-via-splunk-mcp)
for what's in them). It needs everything from `.env` filled in (`python
scripts/check_env.py` first if unsure) — no separate MCP config file needed,
it connects directly.

## How it's built

- **`main.py`** — FastAPI app. `GET /` serves `static/index.html`; `GET
  /config` returns `{"providers": [...], "default_provider": "..."}` — only
  providers with an API key set in `.env` are listed, plus `openai-spec` when
  `OPENAI_SPEC_API_KEY`, `OPENAI_SPEC_BASE_URL`, and `OPENAI_SPEC_MODELS` are
  all set. `openai_spec_models` in that response is the model list the page
  shows in the header dropdown. `POST /chat` takes
  `{"message": "...", "conversation_id": "...", "provider": "...", "model": "..."}`
  (provider optional, falls back to `LLM_PROVIDER`; `model` is the OpenAI-spec
  model selected in the UI) and returns `{"reply": "..."}`. An
  unconfigured `provider`, or an OpenAI-spec `model` that is not in
  `OPENAI_SPEC_MODELS`, is rejected with `400`, not a crash. A blank or
  over-long message is rejected with `422`. The handler waits at most 180
  seconds, then returns `504`. Other failures return `502` with a short
  message (secret values redacted) that the chat page displays.
- **`static/index.html`** — a minimal HTML/JS chat page, no build step. A
  provider dropdown in the header is populated from `/config` (so it never
  offers a provider with no key) and sent with every message, letting you
  switch anthropic/openai/gemini/openai-spec per turn without restarting the
  app. Choosing OpenAI spec reveals the model dropdown filled from
  `openai_spec_models`, and the selected model is sent with the message.
  Generates a random `conversation_id` once per page load and sends it with
  every message. The server keeps the last 8 user/assistant turns for that
  id and sends them with the next question, so a follow-up can refer to the
  previous answer. Search stays disabled until the provider list loads, and
  again while a request is in flight. A reload
  starts a fresh Splunk Agent Observability (Galileo) session and a fresh history.
- **`mcp_client.py`** — connects to the Splunk MCP server at
  `<SPLUNK_INSTANCE_URL>:8089/services/mcp` (see `scripts/setup_mcp.py` for
  how that URL is derived) using the `mcp` Python SDK, authenticating with
  `SPLUNK_MCP_TOKEN`. Splunk's management port uses a self-signed cert by
  default, so TLS verification is disabled for this one connection.
- **`agent.py`** — a supervisor/classifier/worker structure, not just a flat
  loop:
  - A **supervisor** agent span (`agent_type="supervisor"`) wraps the whole
    turn.
  - A whole-word keyword check decides whether to open Splunk MCP. Security
    and infra words, plus `splunk`, `index`, `search`, and `oidemo`, count as
    Splunk intent, including when they appear in a recent user turn so a
    follow-up still searches. Anything else is a direct chat reply with no
    tools and no MCP session.
  - A **classifier** agent span (`agent_type="classifier"`) runs only for a
    Splunk question and picks one or more categories — `security`, `infra`,
    or, when nothing matches, `general` — via that same keyword heuristic
    (not an LLM call, to keep this deterministic and free of extra API
    cost/latency). `"power"`
    matches a power question and does not match `"powershell"`. Each matched
    category selects a scoped system prompt (e.g. the security prompt knows
    `oidemo_notable` is `sourcetype=stash` with `severity` embedded as
    literal uppercase text like `severity=HIGH`, not a normalized field) and
    a scoped tool subset. The `saia_*` tools are excluded from every
    category, since they reliably return server errors on this instance
    (confirmed). `splunk_get_user_list` is also withheld from `general`, the
    path a question takes when no keyword matches.
  - A **worker** agent span (`agent_type="react"`) per matched category then
    runs the tool-calling loop for the selected provider
    (anthropic/openai/gemini) with that scoped prompt and tools, plus the
    stored conversation history: call the LLM, execute whatever tool call it
    asks for, feed the result back, repeat until it returns a final answer.
    When more than one category matched, a synthesis LLM call with no tools
    combines the workers' findings into the reply the user sees.

  In Splunk Agent Observability (Galileo) this renders as `trace -> agent(supervisor) ->
  [agent(classifier), agent(react) -> [llm, tool, llm, ...], …]` (verified
  against the real backend). A multi-category turn adds a synthesis `llm`
  span after the workers.

  Two independent safety nets inside each worker loop, since a real LLM can
  get stuck: `MAX_TURNS` caps the round count (8), and a repeated-call guard
  stops if the model calls the exact same tool with the exact same arguments
  again at any point in that worker's turn — a common real loop failure
  mode, faster to catch than waiting for the cap. The guard is a set of
  calls already made, so the repeat does not have to be the immediately
  previous call. Either one tripping sets `status_code=1` on
  the worker's (and supervisor's) agent span when it concludes, so a stuck
  turn is visible/filterable in Splunk Agent Observability (Galileo) instead of just a silent fallback
  message in the chat.

  The LLM calls (`call_openai`/`call_anthropic`/`call_gemini`) are plain
  sync functions, called directly. Two things that look like obvious
  improvements were tried and both regressed:
  - `asyncio.to_thread` — confirmed broken: it resolves Splunk Agent Observability (Galileo)'s logger to
    a different object with no active trace, silently dropping every LLM
    span.
  - Each provider's async client (`AsyncOpenAI`/`AsyncAnthropic`/`.aio`),
    awaited in-line — broke OpenAI outright: `galileo.openai`'s wrapper only
    patches the sync `Completions.create` (confirmed by reading its
    `OPENAI_CLIENT_METHODS` list), so the async client bypasses it entirely
    and forwards Splunk Agent Observability (Galileo)'s `name=` kwarg straight to the real API, which
    rejects it (`TypeError: AsyncCompletions.create() got an unexpected
    keyword argument 'name'` — hit as a live 500 in a running app). It also
    didn't fix the missing-span issue below for Anthropic/Gemini anyway.
  Calling them synchronously blocks the event loop for the duration of each
  request — an acceptable tradeoff for this single-user demo.

  **Known unresolved issue:** a real multi-round conversation still tends to
  lose most (not all) `llm` spans in Splunk Agent Observability (Galileo) — every `tool` span and the
  trace's own input/output are unaffected, and this is purely an
  observability gap, not a functional bug (the chat app's answers are
  correct regardless). Extensively investigated — bounding the logged
  payload size, the async-client attempt above, flushing after every span
  instead of once at the end, and `mode="distributed"` were all tried and
  none fixed it, while several fabricated-data reproductions using the
  identical code path never reproduced it at all. See the `KNOWN ISSUE`
  comment in `observability.py` for the full trail. If you see this during
  the workshop, it's not something wrong with your setup.
- **`observability.py`** — Traces go to `https://app.galileo.ai` unless
  `GALILEO_CONSOLE_URL` is set (for example
  `https://console.multitenant.galileocloud.io`). OpenAI calls go through Splunk Agent Observability (Galileo)'s native
  `galileo.openai` wrapper (auto-logs, no decorator needed), passing
  `name="openai"` (or `name="openai-spec"` for an OpenAI-compatible endpoint)
  so its spans are labeled by provider instead of the
  wrapper's generic default (`"llm"`) — that kwarg is captured by Splunk Agent Observability (Galileo)
  for the span label and stripped before the real API call, never sent to
  OpenAI. Anthropic and Gemini calls build their span by hand via
  `GalileoLogger.add_llm_span(...)` instead of the generic
  `@log(span_type="llm")` decorator — `@log` auto-captures *every* function
  argument as the input (including `system_prompt` as a stray key, since it
  doesn't know Anthropic/Gemini keep the system prompt separate from the
  conversation) and re-stringifies structured outputs it doesn't recognize
  (verified: a response with an Anthropic `thinking` block fell back to a
  raw JSON blob instead of readable text). Calling `add_llm_span` directly
  gives full control: `input` is a clean `[{"role": "system", ...}, ...]`
  list matching OpenAI's shape, `output` is flattened to readable text, and
  `tools`/token counts/duration are passed explicitly (the `tools` list
  matters — without it, Splunk Agent Observability (Galileo)'s `tool_selection_quality` metric can't run
  and reports "not applicable"). Every Splunk MCP tool call still uses
  `@log(span_type="tool")`, which doesn't have this problem since its
  input/output are already simple strings. `run_traced_turn` maps each
  `conversation_id` to a Splunk Agent Observability (Galileo) session (created once via `start_session`,
  cached), explicitly calls `start_trace(input=user_message)` /
  `conclude(output=result)` so the trace shows the real question and answer
  rather than an arbitrary child span's input/output, and wraps it all in
  one `galileo_context(session_id=...)` so every LLM/tool span from that
  turn lands in one trace, and every turn in the conversation lands in one
  session.

If you're building your own version instead of using this one, this is the
same build order: MCP client → LLM adapter/agent loop → Splunk Agent Observability (Galileo) tracing →
chat UI.
