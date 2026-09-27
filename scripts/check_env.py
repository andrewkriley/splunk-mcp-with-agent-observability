"""Check whether .env has everything needed to participate in the workshop.

Run this after filling in .env to get a readiness report — at least one LLM
key matching LLM_PROVIDER, a Splunk Agent Observability (Galileo) API key, and a Splunk instance URL +
MCP token — instead of discovering a missing value later mid-session.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

LLM_KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def check(label: str, ok: bool, detail: str = "") -> bool:
    symbol = "PASS" if ok else "FAIL"
    line = f"  [{symbol}] {label}"
    if detail:
        line += f" ({detail})"
    print(line)
    return ok


def main():
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        print(f"No .env file found at {env_path}")
        print("Run: cp .env.example .env, then fill it in.")
        sys.exit(1)

    load_dotenv(env_path)
    all_ok = True

    print("LLM provider")
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    provider_valid = provider in LLM_KEY_ENV_VARS
    all_ok &= check(
        "LLM_PROVIDER is set to a supported value (anthropic | openai | gemini)",
        provider_valid,
        f"got '{provider or '(empty)'}'",
    )

    key_present = {p: bool(os.environ.get(v, "").strip()) for p, v in LLM_KEY_ENV_VARS.items()}
    any_key_present = any(key_present.values())
    filled_in = ", ".join(p for p, present in key_present.items() if present)
    all_ok &= check("At least one LLM API key is filled in", any_key_present, filled_in or "none set")

    if provider_valid:
        all_ok &= check(
            f"{LLM_KEY_ENV_VARS[provider]} is filled in (matches LLM_PROVIDER={provider})",
            key_present[provider],
        )

    print("\nSplunk Agent Observability (Galileo)")
    all_ok &= check("GALILEO_API_KEY is filled in", bool(os.environ.get("GALILEO_API_KEY", "").strip()))

    print("\nSplunk MCP")
    all_ok &= check("SPLUNK_INSTANCE_URL is filled in", bool(os.environ.get("SPLUNK_INSTANCE_URL", "").strip()))
    all_ok &= check("SPLUNK_MCP_TOKEN is filled in", bool(os.environ.get("SPLUNK_MCP_TOKEN", "").strip()))

    print()
    if all_ok:
        print("Your .env is ready for the workshop.")
    else:
        print("Some required values are missing or mismatched — see FAIL lines above.")
        print("Check build.md for where each value comes from.")
        sys.exit(1)


if __name__ == "__main__":
    main()
