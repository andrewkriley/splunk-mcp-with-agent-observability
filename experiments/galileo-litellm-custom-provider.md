# Experiment: pointing Splunk Agent Observability (Galileo)'s custom LLM provider at LiteLLM

**Status: untested, exploratory.** Not part of the core workshop — this is
about configuring the LLM Splunk Agent Observability (Galileo) itself uses to power its scoring/judge
metrics (e.g. `groundedness`, `tool_selection_quality`), not anything the
workshop's chat app calls. See the `## Metrics stuck on "queued"` entry in
`build.md`'s troubleshooting for why this came up: those metrics never
completed even with a healthy account-level OpenAI integration, and using a
LiteLLM proxy as a custom provider was one avenue considered for more control
over (or visibility into) what backs Splunk Agent Observability (Galileo)'s scoring calls.

## Background

Splunk Agent Observability (Galileo)'s console has a "Custom Provider" integration option with a config
shape like:

```json
{
  "authentication_type": "api_key",
  "api_key_header": "YOUR_API_KEY_HEADER",
  "api_key_value": "YOUR_API_KEY_VALUE",
  "model_properties": [
    {
      "name": "gpt-5.2",
      "alias": "GPT 5.2",
      "supported_parameters": [
        "max_tokens", "n", "reasoning_effort", "stop_sequences",
        "temperature", "tool_choice", "tools", "verbosity"
      ]
    },
    { "name": "gpt-5.4", "alias": "GPT 5.4", "based_on": "gpt-5.4" },
    { "name": "claude-opus-4-6", "alias": "Opus 4.6", "based_on": "Claude Opus 4.6" }
  ],
  "endpoint": "https://YOUR_PROVIDER_BASE_URL"
}
```

[LiteLLM](https://docs.litellm.ai/) runs a proxy that exposes an
OpenAI-compatible API (`/chat/completions`) in front of many backends
(OpenAI, Anthropic, Bedrock, etc.), which is exactly the shape a
"custom provider" integration expects. I could not find a Splunk Agent Observability (Galileo) doc page
confirming the exact field semantics below — this is reasoned from the
schema plus LiteLLM's documented behavior, not verified end-to-end against
a real Splunk Agent Observability (Galileo) account.

## Proposed setup

**1. Run a LiteLLM proxy somewhere Splunk Agent Observability (Galileo) Cloud can reach.** Splunk Agent Observability (Galileo) Cloud
(`app.galileo.ai`) is a SaaS product — `localhost` won't work. Use a small
VM, a container behind TLS, or an ngrok/Cloudflare tunnel for a quick test.

```yaml
# litellm config.yaml
model_list:
  - model_name: gpt-5.2
    litellm_params:
      model: openai/gpt-5.2
      api_key: os.environ/OPENAI_API_KEY
  - model_name: claude-opus-4-6
    litellm_params:
      model: anthropic/claude-opus-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
general_settings:
  master_key: sk-your-litellm-master-key
```

```
litellm --config config.yaml --port 4000
```

**2. Fill in Splunk Agent Observability (Galileo)'s custom-provider config:**

```json
{
  "authentication_type": "api_key",
  "api_key_header": "Authorization",
  "api_key_value": "Bearer sk-your-litellm-master-key",
  "model_properties": [
    {
      "name": "gpt-5.2",
      "alias": "GPT 5.2 (via LiteLLM)",
      "supported_parameters": ["max_tokens", "n", "reasoning_effort", "stop_sequences", "temperature", "tool_choice", "tools", "verbosity"]
    },
    {
      "name": "claude-opus-4-6",
      "alias": "Opus 4.6 (via LiteLLM)",
      "supported_parameters": ["max_tokens", "temperature", "tool_choice", "tools", "stop_sequences"]
    }
  ],
  "endpoint": "https://<your-public-litellm-host>"
}
```

## Mapping rules / open questions

- **`model_properties[].name` must exactly match `model_name` in LiteLLM's
  `model_list`** — it's the literal string Splunk Agent Observability (Galileo) sends as `"model": "..."`,
  and LiteLLM routes on that name to the real backend.
- **`endpoint` is assumed to be the proxy's base URL, no path suffix** —
  LiteLLM's proxy serves the standard OpenAI paths itself. *Unconfirmed*
  whether Splunk Agent Observability (Galileo) expects the bare base URL or a full path.
- **`api_key_header: "Authorization"`** matches LiteLLM's default (`Authorization:
  Bearer <key>`), where `<key>` is the proxy's `master_key` or a virtual key
  from `/key/generate`. *Unconfirmed* whether `api_key_value` should be the
  bare key or the full `Bearer <key>` string — Splunk Agent Observability (Galileo) may or may not add the
  `Bearer ` prefix itself.
- **`supported_parameters` must match the real backend per model**, not be
  copy-pasted across entries — e.g. `reasoning_effort`/`verbosity` are
  GPT-5-reasoning-specific and invalid for a Claude-backed entry.

## How to actually verify this

1. Run LiteLLM locally with `--debug` so every incoming request is logged.
2. Trigger a call through Splunk Agent Observability (Galileo) that would use this custom provider (e.g.
   set it as the scoring/judge model, or test it in Splunk Agent Observability (Galileo)'s Playground if
   available).
3. Read LiteLLM's request log to see the exact path, headers, and body
   Splunk Agent Observability (Galileo) sent, and confirm/correct the assumptions above.
4. If it 401s: try both the bare key and the `Bearer `-prefixed value in
   `api_key_value`.
5. If it 404s: try both the bare host and a host with `/chat/completions`
   appended in `endpoint`.

Nothing here has been run against a live Splunk Agent Observability (Galileo) account yet — treat this as
a starting point, not a confirmed working config.
