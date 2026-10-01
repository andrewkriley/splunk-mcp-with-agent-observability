"""Check whether .env has everything needed to participate in the workshop.

Run this after filling in .env to get a readiness report — at least one LLM
key matching LLM_PROVIDER, a Galileo or Observability Cloud destination, and a Splunk instance URL +
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
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.observability import ao_destination_errors, ao_destination_target, configured_ao_destinations, openai_spec_config

    print("LLM provider")
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    supported = set(LLM_KEY_ENV_VARS) | {"openai-spec"}
    provider_valid = provider in supported
    all_ok &= check(
        "LLM_PROVIDER is set to a supported value (anthropic | openai | gemini | openai-spec)",
        provider_valid,
        f"got '{provider or '(empty)'}'",
    )

    key_present = {p: bool(os.environ.get(v, "").strip()) for p, v in LLM_KEY_ENV_VARS.items()}
    try:
        spec = openai_spec_config()
    except ValueError as exc:
        spec = None
        all_ok &= check("OPENAI_SPEC_BASE_URL is a valid endpoint", False, str(exc))
    if spec:
        key_present["openai-spec"] = True
    any_key_present = any(key_present.values())
    filled_in = ", ".join(p for p, present in key_present.items() if present)
    all_ok &= check("At least one LLM API key is filled in", any_key_present, filled_in or "none set")

    if provider_valid and provider == "openai-spec":
        all_ok &= check(
            "OPENAI_SPEC_API_KEY, OPENAI_SPEC_BASE_URL, and OPENAI_SPEC_MODELS are set",
            spec is not None,
            ", ".join(spec["models"]) if spec else "key, endpoint URL, and model list are all required",
        )
    elif provider_valid:
        all_ok &= check(
            f"{LLM_KEY_ENV_VARS[provider]} is filled in (matches LLM_PROVIDER={provider})",
            key_present[provider],
        )

    print("\nAgent Observability destination")
    destinations = configured_ao_destinations()
    labels = {"standalone": "Galileo", "o11y": "Observability Cloud"}
    all_ok &= check(
        "At least one destination is configured (Galileo and/or Observability Cloud)",
        bool(destinations),
        ", ".join(labels[name] for name in destinations) or "none set",
    )
    for name in destinations:
        all_ok &= check(labels[name], True, ao_destination_target(name))
    for error in ao_destination_errors():
        all_ok &= check(error, False)

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
