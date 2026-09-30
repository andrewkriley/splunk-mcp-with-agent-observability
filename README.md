# splunk-mcp-with-agent-observability

## Overview

A hands-on project for building an AI agent that queries live Splunk data
through [MCP](https://modelcontextprotocol.io) and reports agent observability
signals to Splunk Agent Observability (Galileo).

Dependencies:

1. [Splunk Show workshop](https://show.splunk.com/template/946/)
2. Splunk Agent Observability (Galileo)
3. An inference provider (Anthropic, OpenAI, OpenAI spec)

## What you'll build

A small web app with a chat interface, backed by an AI agent that:

1. Takes a user's question in the chat UI. Earlier user and assistant turns
   from the same page load are included with the new question, so a follow-up
   can refer to the previous answer. Reloading the page starts a fresh
   conversation.
2. Decides whether the question needs Splunk. A whole-word check looks at
   this message and recent user turns for security, infra, or explicit Splunk
   words (`splunk`, `index`, `search`, `oidemo`, `infra`, `security`, `threats`,
   `events`). A match opens Splunk MCP and
   classifies the question as security, infra, both, or general. Each match
   gets its own worker with a scoped system prompt and tool subset. When more
   than one category matches, a synthesis call with no tools combines their
   findings into one answer. A question with no Splunk intent skips MCP and
   gets a direct answer from the model.
3. Each worker calls an LLM API (Anthropic, OpenAI, Gemini, or an OpenAI-spec
   endpoint — your own key) to reason about the question. A dropdown in the
   chat UI switches between whichever of those you have configured, per turn.
   Anthropic, OpenAI, and Gemini each use one model hardcoded in
   `app/observability.py`: `claude-sonnet-5`, `gpt-4o`, and `gemini-3.6-flash`.
   For an OpenAI-spec endpoint, a second dropdown lists the models from
   `OPENAI_SPEC_MODELS`, and the selected model is the one that is called.
4. When the question has Splunk intent, lets the LLM call tools exposed by a
   **Splunk MCP server** to query your Splunk instance, with two safety nets
   against a stuck agent: a round cap, and a guard that stops if the model repeats an
   identical tool call (same tool, same arguments) at any point in that
   worker's turn.
5. Traces each turn (prompts, tool calls, responses) to **Splunk Agent Observability (Galileo)** for
   agent observability, structured as `supervisor → [classifier, worker →
   [llm, tool, ...], …]` agent spans — every turn in one browser conversation
   is grouped under a single Splunk Agent Observability (Galileo) session, so a full back-and-forth
   shows up as one session containing multiple traces.

```
 Browser (chat UI)
       │
       ▼
   FastAPI app ──► supervisor agent ──► classifier (keyword categories)
       │                 │
       │                 ▼
       │           worker agent(s) ──► LLM API (Anthropic / OpenAI / Gemini)
       │                 │                     │
       │                 │                     ▼ (tool calls)
       │                 └───────────► Splunk MCP server ──► your Splunk instance
       │                 │
       │                 ▼ (when more than one category matched)
       │           synthesis LLM call (no tools)
       │
       ▼
    Splunk Agent Observability (Galileo) (nested trace: supervisor → classifier + worker(s) → llm/tool spans)
```

Splunk Agent Observability (Galileo) only ships a native wrapper for OpenAI (`galileo.openai`, drop-in,
auto-logs every call). Anthropic and Gemini calls build their span by hand
via `GalileoLogger.add_llm_span(...)` for full control over what gets
logged — the generic `@log` decorator dumps every function argument
(including the system prompt) as a stray field instead of a clean message
list, and mangles response types it doesn't recognize. Splunk MCP tool
calls still use `@log(span_type="tool")`, which doesn't have that problem.

A working reference app ships in [`app/`](./app) (`uvicorn app.main:app
--reload`) so everyone has something running by the end of the session. If
you're comfortable, you're encouraged to build your own version from scratch
using the same three building blocks (LLM API, MCP client, Splunk Agent Observability (Galileo)).

## Data available via Splunk MCP

Each participant's Splunk instance ships with the same demo dataset, exposed
through these MCP tools (confirmed live against a workshop instance):

- `splunk_get_info`, `splunk_get_indexes`, `splunk_get_index_info` — instance
  and index metadata
- `splunk_run_query` — run an arbitrary SPL search and get results back
- `splunk_get_metadata`, `splunk_get_knowledge_objects`,
  `splunk_get_kv_store_collections` — field/source/sourcetype metadata and
  saved knowledge objects
- `splunk_get_user_list`, `splunk_get_user_info` — Splunk user/role info
- `saia_generate_spl`, `saia_explain_spl`, `saia_optimize_spl`,
  `saia_ask_splunk_question` — Splunk AI Assistant helpers for going from a
  plain-English question to SPL (or explaining/optimizing SPL you already
  have)

The two indexes this workshop is built around:

| Index | Events | What's in it |
|---|---|---|
| `oidemo` | ~109k | IT/datacenter operations telemetry — PDU power draw (`Amps`/`Volts`/`W`), CRAC cooling unit temperatures (Kepware sourcetype), plus Windows/Exchange Perfmon counters |
| `oidemo_notable` | ~2.8k | Splunk Enterprise Security **notable events** correlated to the above — brute-force login attempts, insecure/cleartext auth failures, expired-identity activity, audit-log-cleared events |

`oidemo` is raw infrastructure telemetry and `oidemo_notable` is the
correlated security-alert layer on top of it, so questions like "any
high-severity notables in the last 30 days, and what infra were they near?"
have real, connected data to answer from. Try asking your AI harness (once
MCP is wired up in [step 9](./build.md)) something like:

> "Run an SPL search against index=oidemo_notable for high severity events,
> then check oidemo for related PDU or cooling activity around the same
> time."

## Prerequisites

### On your workstation

What you need installed and runnable locally before you start:

- **Python 3.11+** (`python3 --version`)
- **git**
- **Node.js/npx** (`node --version`) — your AI harness connects to Splunk MCP
  through the `mcp-remote` proxy, run on demand via `npx`, no separate
  install needed
- **An AI coding harness** (e.g. Claude Code) — used to drive the bootstrap
  steps and, once MCP is wired up, to query Splunk MCP tools directly
- A terminal and a text editor

Nothing else needs to be running ahead of time — `build.md` walks you through
creating a Python virtual environment and installing the rest.

### Accounts & keys

- A [Splunk Agent Observability (Galileo)](https://app.galileo.ai/sign-up) account (free to sign up)
- Your own Anthropic, OpenAI, or Gemini API key (**not** a subscription tool
  like Claude Code/Claude.ai or Cursor/ChatGPT Plus — the app needs a key it
  can call directly). No key yet? [Google's Gemini API has a free tier](https://ai.google.dev/gemini-api/docs/pricing)
  with free input/output tokens on several models — the quickest way to get
  one. Use a plain **gmail.com** account for this, not a legacy/grandfathered
  Google Workspace account — those can land on a Cloud project that's denied
  access to free-tier calls.
- A Splunk Cloud login and Splunk MCP token — **provided by the workshop
  facilitator** at the start of the session

## Getting started

Follow [`build.md`](./build.md) — it's the step-by-step workflow for this
workshop, from cloning the repo through to a running app.

## Repo layout

| Path | Purpose |
|---|---|
| `build.md` | Step-by-step workshop workflow |
| `.env.example` | Template for your local `.env` (API keys, tokens) |
| `requirements.txt` | Python dependencies |
| `scripts/check_env.py` | Reports whether your `.env` has everything needed to participate |
| `scripts/setup_mcp.py` | Derives the Splunk MCP endpoint, verifies it, and wires up `.mcp.json` |
| `app/main.py` | FastAPI app — `/chat`, `/config` (`uvicorn app.main:app --reload` to run it) |
| `app/agent.py` | Per-provider LLM <-> Splunk MCP tool-calling loop |
| `app/mcp_client.py` | Splunk MCP connection (self-signed cert handled) |
| `app/observability.py` | Splunk Agent Observability (Galileo) tracing (native OpenAI wrapper / `@log` decorator, sessions) |
| `app/static/index.html` | The chat UI, with a provider switcher |
| `.github/workflows/gitleaks.yml` | CI check that scans commits for leaked secrets |
