"""Minimal FastAPI chat app: browser <-> LLM agent <-> Splunk MCP, traced to Splunk Agent Observability (Galileo).

Run from the repo root with: uvicorn app.main:app --reload
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.observability import run_traced_turn  # noqa: E402 — import after load_dotenv sets env vars

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
)
MAX_MESSAGE_CHARS = 4000
CHAT_TIMEOUT_SECONDS = 180


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    conversation_id: str = Field(min_length=1, max_length=128)
    provider: str | None = None

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


def _configured_providers() -> list[str]:
    return [p for p, env_var in PROVIDER_KEY_ENV_VARS.items() if os.environ.get(env_var, "").strip()]


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
    return ConfigResponse(providers=providers, default_provider=default_provider)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    if request.provider and request.provider not in _configured_providers():
        raise HTTPException(400, f"'{request.provider}' has no API key configured in .env")

    try:
        reply = await asyncio.wait_for(
            run_traced_turn(request.message, request.conversation_id, provider=request.provider),
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
