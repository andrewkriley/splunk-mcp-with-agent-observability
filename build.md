# Workshop workflow

Follow these steps in order. Where a step says "ask your AI harness," you can
paste the instruction into Claude Code (or whichever AI coding assistant
you're using) and let it run the commands for you.

## 1. Clone the repo

```
git clone https://github.com/andrewkriley/splunk-mcp-with-agent-observability.git
cd splunk-mcp-with-agent-observability
```

## 2. Note your Observability Cloud realm

Traces go to Agent Observability in Splunk Observability Cloud. The realm is
the label in `https://app.<realm>.observability.splunkcloud.com`, for example
`au0`. The SDK builds the console, API, and ingest hosts from that realm.

## 3. Create an Observability Cloud access token

1. In Observability Cloud, open Settings → Access tokens.
2. Create a token with ingest permission and API access. One token is enough
   when it has both scopes. The `agent_observability_admin` role lets the SDK
   create the project and Agent Stream.
3. Copy the token somewhere safe — it is only shown once.
4. Note (or create) a project name you'll use for this workshop, e.g.
   `splunk-mcp-with-agent-observability`.

Details: https://agent-observability-docs.splunk.com/references/faqs/find-keys#saas

## 4. Set up your `.env` file

```
cp .env.example .env
```

Open `.env` and fill in Observability Cloud:

```
SPLUNK_AO_REALM=<realm from app.<realm>.observability.splunkcloud.com>
SPLUNK_AO_O11Y_TOKEN=<Observability Cloud access token with ingest and API>
SPLUNK_AO_PROJECT=splunk-mcp-with-agent-observability
SPLUNK_AO_AGENT_STREAM=default
```

Add `SPLUNK_AO_O11Y_API_TOKEN` only when project calls need a different token.
For realm `au0`, traces go to
`https://ingest.au0.observability.splunkcloud.com/v2/trace/otlp`. Put the
realm in `SPLUNK_AO_REALM`. The ingest host is not a separate setting.

## 5. Get your Splunk details from the facilitator

The facilitator will hand out, per participant:

- A Splunk Cloud instance URL and login
- A Splunk MCP token

Log in to the Splunk Cloud instance in your browser first, to confirm your
credentials work.

Then fill in the Splunk section of `.env` — just the base instance URL, no
port or path:

```
SPLUNK_INSTANCE_URL=<base instance URL the facilitator gave you>
SPLUNK_MCP_TOKEN=<token the facilitator gave you>
```

`scripts/setup_mcp.py` (step 9) derives the full MCP endpoint for you —
Splunk's MCP Server for Splunk Platform always serves it at
`<instance>:8089/services/mcp`.

## 6. Bring your own LLM API key

Get an API key from **one** of:

- Anthropic: https://console.anthropic.com
- OpenAI: https://platform.openai.com
- Gemini: https://aistudio.google.com/apikey — Google's Gemini API has a free
  tier with free input/output tokens on several models, so this is the
  quickest option if you don't already have a billed key. See
  [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) for
  which models and rate limits are included.

  > Create this key while signed in with a plain **gmail.com** personal
  > account. A legacy/grandfathered Google Workspace account can end up on a
  > Cloud project that's denied access to free-tier generation calls
  > (`403 PERMISSION_DENIED`) even though the key itself looks valid.

If you go with Anthropic or OpenAI, this must be a billed API key, not a
Claude.ai/ChatGPT/Cursor subscription — the app calls the API directly and
pays per token.

Fill in the LLM section of `.env`, setting `LLM_PROVIDER` to match whichever
key you got:

```
LLM_PROVIDER=anthropic          # anthropic | openai | gemini | openai-spec
ANTHROPIC_API_KEY=<your key>    # if using Anthropic
OPENAI_API_KEY=<your key>       # if using OpenAI
GEMINI_API_KEY=<your key>       # if using Gemini
OPENAI_SPEC_API_KEY=<your key>  # if using an OpenAI-spec endpoint
OPENAI_SPEC_BASE_URL=https://your-endpoint.example/v1
OPENAI_SPEC_MODELS=model-a, model-b
```

An OpenAI-spec endpoint is any server that speaks `/chat/completions` (vLLM,
Ollama, LiteLLM, or a private gateway). It is offered in the chat UI only
when the key, base URL, and model list are all set. The models from
`OPENAI_SPEC_MODELS` show in a dropdown next to the provider.

Got more than one key? Fill in all of them — the chat app has a provider
dropdown that lets you switch between anthropic/openai/gemini/openai-spec per
message, without editing `.env` or restarting anything.

## 7. Create a virtual environment and install dependencies

A venv doesn't install Python for you — it just wraps whatever `python3`
already resolves to on your machine. Check that's 3.11+ first:

```
python3 --version
```

If it's older than 3.11 (common on macOS, where the system `python3` is
often 3.9), install a current one before creating the venv:

```
brew install python@3.11          # macOS
```

Then create the venv with that specific interpreter (safer than the bare
`python3` if you have more than one Python installed):

```
python3.11 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Keeping this in a venv means the workshop's dependencies (and Python version
requirement) won't clash with anything else on your machine, and you can
delete `.venv/` afterwards to clean up.

Remember to run `source .venv/bin/activate` again any time you open a new
terminal for the rest of the workshop.

## 8. Check your `.env` is ready

```
python scripts/check_env.py
```

This confirms you have at least one LLM key set and matching `LLM_PROVIDER`,
an Observability Cloud realm and token, and a Splunk instance URL + MCP token — before you go any
further.

## 9. Wire up your Splunk MCP connection

Run the setup script — it derives the full MCP endpoint from
`SPLUNK_INSTANCE_URL`, does a live connectivity check (listing the server's
tools), and writes/updates a project-scoped `.mcp.json` so your AI harness
can use those same tools:

```
python scripts/setup_mcp.py
```

This requires Node.js/npx (see [Prerequisites](./README.md#prerequisites)) —
your AI harness runs the Splunk MCP connection through the `mcp-remote`
proxy, fetched on demand via `npx`, no separate install needed.

If it fails, see [Troubleshooting](#troubleshooting).

## 10. Check MCP access

Ask your AI harness (e.g. Claude Code) to list the available Splunk MCP tools
and run a simple query, for example:

> "List the Splunk MCP tools available to you, then run a sample search
> against the Splunk instance to confirm you can read data."

If it returns real results from your Splunk instance, you're ready to build.

## 11. Run the app

```
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and ask it something about your Splunk data (see
[what's available](./README.md#data-available-via-splunk-mcp)). If you filled
in more than one LLM key, use the provider dropdown in the top-right of the
chat header to switch between them and compare answers/traces side by side.

See [`app/README.md`](./app/README.md) for how it's built if you want to
modify it or build your own version from the same pieces (LLM adapter, MCP
client, Splunk Agent Observability (Galileo) tracing, chat UI).

## Troubleshooting

- **`pip install -r requirements.txt` fails with `Could not find a version
  that satisfies the requirement mcp`** — your venv was created with Python
  <3.10 (`mcp` requires 3.10+). Check with `.venv/bin/python3 --version`,
  then delete and recreate `.venv` using a 3.11+ interpreter as shown in
  [step 7](#7-create-a-virtual-environment-and-install-dependencies).
- **Not sure what's missing from `.env`** — run `python scripts/check_env.py`
  for a full readiness report (LLM key/provider match, Observability Cloud, Splunk MCP).
- **`scripts/setup_mcp.py` reports missing env vars** — double check `.env`
  has `SPLUNK_INSTANCE_URL` and `SPLUNK_MCP_TOKEN` filled in (not left blank
  from `.env.example`), and that `SPLUNK_INSTANCE_URL` is just the base URL
  (no port or path).
- **MCP connection fails / times out** — confirm you're on the workshop
  network/VPN if one is required, and that `SPLUNK_INSTANCE_URL` doesn't have
  a trailing slash or extra path.
- **SSL/certificate warnings when connecting to Splunk MCP** — expected.
  Splunk's management port (8089) uses a self-signed certificate by default;
  `scripts/setup_mcp.py` and the generated `.mcp.json` both disable TLS
  verification for this one connection on purpose.
- **`scripts/setup_mcp.py` step works but your AI harness still can't reach
  Splunk MCP** — confirm Node.js/npx is installed (`node --version`); the
  generated `.mcp.json` runs the connection through `npx -y mcp-remote`.
- **The chat app says it couldn't fetch data / hit a "server error" for a
  security-related question** — the `saia_*` tools (`saia_generate_spl`,
  `saia_ask_splunk_question`, etc.) call a separate Splunk AI Assistant
  backend on the instance that can return its own `500` errors, independent
  of this app. The reference agent's system prompt already steers it toward
  `splunk_run_query` with explicit SPL instead — if you're building your own
  agent, do the same rather than relying on `saia_*`.
- **LLM API calls fail with an auth error** — check you copied the full key
  with no extra whitespace, and that it's an API key (starts with `sk-ant-`
  for Anthropic, `sk-` for OpenAI, or `AIza` for Gemini), not a session token.
- **Gemini key works for listing models but every generation call returns
  `403 PERMISSION_DENIED: "Your project has been denied access"`** — this is
  a Cloud project access issue, not a bad key. Create a new key at
  https://aistudio.google.com/apikey while signed in with a plain gmail.com
  account instead of a legacy/grandfathered Google Workspace account.
- **No traces show up** — confirm `SPLUNK_AO_REALM` and `SPLUNK_AO_O11Y_TOKEN`
  are set, and look under Agent Observability in that realm, not on the
  ingest host. Traces appear after a completed request. The server log line
  `AO target=` names the ingest host for that turn.
- **Metrics (e.g. `groundedness`, `tool_selection_quality`) stay stuck on
  "queued"/"pending" on every trace, even old ones** — this isn't caused by
  the app or `.env`. Confirmed via the Splunk Agent Observability (Galileo) API: the account's scoring
  integration (Settings → Integrations) shows green/healthy, and every trace
  correctly has both metrics attached, but the scoring jobs never complete
  regardless of how long you wait. This points to a Splunk Agent Observability (Galileo) backend issue
  (stuck scoring queue, or automated scoring throttled on some account
  tiers), not something fixable from this repo. It doesn't block the
  workshop — traces, spans, and sessions all log and display correctly;
  only the auto-computed quality scores are affected. If it matters for your
  session, check Splunk Agent Observability (Galileo)'s status page or contact their support.
- **A multi-round conversation's trace is missing some of its `llm` spans**
  (tool spans and the trace's own input/output all look correct, just some
  LLM calls in the middle are absent) — a known, unresolved issue, not
  something wrong with your setup. Extensively investigated (see the
  `KNOWN ISSUE` comment in `app/observability.py`): bounding logged payload
  size, switching to each provider's async client, flushing after every
  span instead of once at the end, and `mode="distributed"` were all tried
  and none fixed it. Purely a Splunk Agent Observability (Galileo) observability gap — the chat app's
  answers remain correct regardless. (The async-client attempt was reverted
  outright, separately from this issue — it broke OpenAI, since
  `splunk_ao.openai`'s wrapper doesn't patch the async client. The app now
  uses each provider's sync client, called directly.)
