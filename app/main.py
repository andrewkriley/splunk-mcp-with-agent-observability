"""Minimal FastAPI chat app: browser <-> LLM agent <-> Splunk MCP, traced to Observability Cloud.

Run from the repo root with: uvicorn app.main:app --reload
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.observability import (  # noqa: E402 — import after load_dotenv sets env vars
    bind_activity,
    o11y_configured,
    o11y_target,
    openai_spec_config,
    reset_activity,
    run_traced_turn,
)

app = FastAPI(title="splunk-mcp-with-agent-observability")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

PROVIDER_KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
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
MAX_MESSAGE_CHARS = 4000
CHAT_TIMEOUT_SECONDS = 180


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    conversation_id: str = Field(min_length=1, max_length=128)
    provider: str | None = None
    model: str | None = Field(default=None, max_length=128)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message is empty")
        return stripped


def _public_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name, "").strip()
        if secret:
            message = message.replace(secret, "[redacted]")
    if not message:
        message = "The request failed."
    if len(message) > 300:
        message = message[:300] + "…"
    return message


class ChatResponse(BaseModel):
    reply: str


class ConfigResponse(BaseModel):
    providers: list[str]
    default_provider: str
    openai_spec_models: list[str] = []
    o11y_configured: bool = False
    o11y_target: str = ""


def _configured_providers() -> list[str]:
    providers = [p for p, env_var in PROVIDER_KEY_ENV_VARS.items() if os.environ.get(env_var, "").strip()]
    try:
        if openai_spec_config() is not None:
            providers.append("openai-spec")
    except ValueError:
        pass
    return providers


def _openai_spec_models() -> list[str]:
    try:
        spec = openai_spec_config()
    except ValueError:
        return []
    return list(spec["models"]) if spec else []


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/config", response_model=ConfigResponse)
async def config():
    providers = _configured_providers()
    if not providers:
        raise HTTPException(500, "No LLM provider API key is configured in .env")

    env_default = os.environ.get("LLM_PROVIDER", "").strip()
    default_provider = env_default if env_default in providers else providers[0]
    return ConfigResponse(
        providers=providers,
        default_provider=default_provider,
        openai_spec_models=_openai_spec_models(),
        o11y_configured=o11y_configured(),
        o11y_target=o11y_target(),
    )


def _turn_arguments(request: ChatRequest) -> tuple[str | None, str | None]:
    if request.provider and request.provider not in _configured_providers():
        raise HTTPException(400, f"'{request.provider}' has no API key configured in .env")

    model = request.model
    if request.provider == "openai-spec":
        allowed = _openai_spec_models()
        if model and model not in allowed:
            raise HTTPException(400, f"'{model}' is not one of the configured OpenAI-spec models")
        model = model or (allowed[0] if allowed else None)

    return request.provider, model


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    provider, model = _turn_arguments(request)

    try:
        reply = await asyncio.wait_for(
            run_traced_turn(
                request.message,
                request.conversation_id,
                provider=provider,
                model=model,
            ),
            timeout=CHAT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            504,
            "The request timed out before the agent finished. Try a narrower question.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, _public_error(exc)) from None
    return ChatResponse(reply=reply)


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Stream status events, then the reply. The page shows noodling vs a Splunk tool call."""
    provider, model = _turn_arguments(request)

    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        token = bind_activity(queue)
        try:
            reply = await asyncio.wait_for(
                run_traced_turn(
                    request.message,
                    request.conversation_id,
                    provider=provider,
                    model=model,
                ),
                timeout=CHAT_TIMEOUT_SECONDS,
            )
            await queue.put({"phase": "done", "reply": reply})
        except TimeoutError:
            await queue.put(
                {
                    "phase": "error",
                    "detail": "The request timed out before the agent finished. Try a narrower question.",
                }
            )
        except Exception as exc:
            await queue.put({"phase": "error", "detail": _public_error(exc)})
        finally:
            reset_activity(token)

    task = asyncio.create_task(run())

    async def events():
        try:
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
                if item["phase"] in {"done", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream")
