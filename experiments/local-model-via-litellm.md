# Experiment: a locally-hosted model as a fourth chat provider, via LiteLLM

**Status: design only, not implemented.** This describes how the reference
app (`app/observability.py`, `app/agent.py`, `app/main.py`) could add a
`local` provider backed by a LiteLLM proxy serving a locally-hosted model,
while still sending telemetry to Splunk Agent Observability (Galileo). No code changes have been made —
this is a plan to build from later, not a working feature.

Not to be confused with [galileo-litellm-custom-provider.md](./galileo-litellm-custom-provider.md),
which is about a different thing: pointing *Splunk Agent Observability (Galileo)'s own scoring backend*
at LiteLLM over the internet. This experiment is about *our app* calling a
LiteLLM proxy directly, which is why `localhost` is perfectly fine here —
the FastAPI app and the LiteLLM proxy can run on the same machine.

## Why this doesn't need a fourth agent loop

LiteLLM's proxy speaks the OpenAI API dialect (`/chat/completions`, same
request/response shape, same `tools`/`tool_choice` fields). `call_openai` in
`app/observability.py` already goes through the real `openai` Python SDK
client — the only thing that makes it hit `api.openai.com` is the client's
default `base_url`. Point that at the LiteLLM proxy instead
(`base_url="http://localhost:4000"`) and everything downstream —
`_openai_loop`'s tool-schema conversion, its `message.tool_calls` handling,
the multi-round loop in `app/agent.py` — works unchanged, because it's the
same wire format. So `local` becomes a fourth *provider value* that reuses
the OpenAI code path with different client config, not a new loop.

## Where the telemetry falls out for free

`call_openai` already routes through Splunk Agent Observability (Galileo)'s native `galileo.openai`
wrapper (`from galileo.openai import openai`), which intercepts
`chat.completions.create` regardless of `base_url` — it's patched at the
method level, not tied to a specific host. A local-model call through that
same wrapped client keeps auto-logging exactly like it does for real
OpenAI: no `@log` decorator needed. Two things to override per-call so the
trace doesn't misrepresent what actually ran:

- `name="local"` (or `"litellm"`) instead of `"openai"`, so it's visually
  distinct in Splunk Agent Observability (Galileo)'s session/trace list from real OpenAI calls (same
  trick already used to label the anthropic/openai/gemini spans).
- `model=<the litellm model_name>` (e.g. `"llama-3.1-70b-instruct"`), so the
  model column shows the real local model. This is automatically satisfied
  here since the OpenAI wrapper already reads `model` from the call kwargs
  — no `params={"model": ...}` workaround needed (that was only necessary
  for the `@log`-decorated Anthropic/Gemini spans).

## Concrete shape of the change

- Generalize `call_openai` (or add a thin sibling) to accept `base_url`,
  `api_key`, and `model` as parameters instead of hardcoding
  `OPENAI_MODEL`/`os.environ["OPENAI_API_KEY"]`. Real OpenAI becomes one
  call site with the default base URL; `local` becomes another with the
  LiteLLM base URL. `_openai_loop` stays as-is — only the provider dispatch
  in `run_agent_turn` (`app/agent.py`) picks which config to construct the
  client with.
- New `.env` vars: `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY` (LiteLLM's
  master/virtual key), `LOCAL_LLM_MODEL` (must exactly match a `model_name`
  in LiteLLM's `config.yaml`).
- `app/main.py`'s `PROVIDER_KEY_ENV_VARS`/`/config` gains a `"local"` entry,
  gated on `LOCAL_LLM_BASE_URL` being set rather than an API-key presence
  check — a configured local endpoint is the meaningful "is this usable"
  signal here, not a key. The existing provider-dropdown mechanism in
  `static/index.html` picks it up with no frontend changes.
- Untouched: session grouping (`start_session`/`galileo_context(session_id=
  ...)`), `start_trace`/`conclude`, the `call_splunk_tool` tool span,
  `MAX_TURNS`. All of that lives above the provider-specific call and
  doesn't care which backend served the LLM call.

## Caveats

- Tool-calling quality depends entirely on the locally-hosted model actually
  supporting function calling well. LiteLLM passes `tools`/`tool_choice`
  through unmodified to whatever's behind it (vLLM, Ollama, llama.cpp
  server, etc.) — it doesn't add tool-calling support a model doesn't have.
  A small/weak local model may need a higher `MAX_TURNS`, or may not
  reliably call tools at all.
- Latency/quality of local inference is entirely dependent on the hardware
  running it — not something to demo live at a conference without testing
  the specific model/hardware combo first.

## How to actually verify this

1. Stand up a LiteLLM proxy pointed at a local model server (Ollama, vLLM,
   etc.), confirm `curl http://localhost:4000/chat/completions` works with
   a plain (no-tools) request first.
2. Confirm `tools=[...]` in the request actually produces `tool_calls` in
   the response for the specific local model — don't assume it does.
3. Implement the `call_openai` generalization described above, wire up the
   new `.env` vars and `/config` entry, and run one real Splunk MCP question
   through it end to end (matching the manual verification pattern used for
   the other three providers).
4. Check the resulting Splunk Agent Observability (Galileo) trace: confirm the `llm` span shows
   `name="local"` and the correct local model name, not `"openai"`/`gpt-4o`.

Nothing here has been run — treat this as a starting point for
implementation, not a confirmed working feature.
