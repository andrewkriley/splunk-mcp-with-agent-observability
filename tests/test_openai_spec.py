"""OpenAI-spec provider: key, endpoint, and the model list shown to the UI."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.observability import ANTHROPIC_MODEL, OPENAI_MODEL, call_openai, describe_llm, openai_spec_config

SPEC_ENV = {
    "OPENAI_SPEC_API_KEY": "sk-spec-test",
    "OPENAI_SPEC_BASE_URL": "https://llm.example.com/v1",
    "OPENAI_SPEC_MODELS": "llama-3.1-8b, mistral-small",
    "LLM_PROVIDER": "openai-spec",
}


class OpenAISpecConfigTests(unittest.TestCase):
    def setUp(self):
        self.previous = {name: os.environ.get(name) for name in SPEC_ENV}

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_models_are_parsed_from_the_list(self):
        os.environ.update(SPEC_ENV)
        spec = openai_spec_config()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["models"], ["llama-3.1-8b", "mistral-small"])
        self.assertEqual(spec["base_url"], "https://llm.example.com/v1")
        self.assertNotIn("sk-spec-test", spec["base_url"])

    def test_missing_endpoint_hides_the_provider(self):
        os.environ.update(SPEC_ENV)
        os.environ["OPENAI_SPEC_BASE_URL"] = ""
        self.assertIsNone(openai_spec_config())

    def test_url_with_credentials_is_rejected(self):
        os.environ.update(SPEC_ENV)
        os.environ["OPENAI_SPEC_BASE_URL"] = "https://user:secret@llm.example.com/v1"
        with self.assertRaises(ValueError):
            openai_spec_config()


class OpenAISpecApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.previous = {name: os.environ.get(name) for name in (*SPEC_ENV, "SPLUNK_AO_API_KEY", "SPLUNK_AO_CONSOLE_URL")}
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_CONSOLE_URL"] = "https://app.galileo.ai"

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_config_lists_the_provider_and_models_for_the_ui(self):
        os.environ.update(SPEC_ENV)
        response = self.client.get("/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("openai-spec", body["providers"])
        self.assertEqual(body["default_provider"], "openai-spec")
        self.assertEqual(body["openai_spec_models"], ["llama-3.1-8b", "mistral-small"])
        self.assertNotIn("sk-spec-test", response.text)

        html = self.client.get("/").text
        self.assertIn('id="model"', html)
        self.assertIn("openai_spec_models", html)
        self.assertIn("OpenAI spec", html)

    def test_unknown_model_is_rejected(self):
        os.environ.update(SPEC_ENV)
        response = self.client.post(
            "/chat",
            json={
                "message": "hi",
                "conversation_id": "c-spec",
                "provider": "openai-spec",
                "model": "not-a-configured-model",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not one of the configured OpenAI-spec models", response.json()["detail"])

    def test_selected_model_is_sent_with_the_turn(self):
        os.environ.update(SPEC_ENV)

        async def capture(*_args, **kwargs):
            capture.seen = kwargs
            return "ok"

        with patch("app.main.run_traced_turn", capture):
            response = self.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "conversation_id": "c-spec",
                    "provider": "openai-spec",
                    "model": "mistral-small",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(capture.seen["provider"], "openai-spec")
        self.assertEqual(capture.seen["model"], "mistral-small")


class LlmLogTests(unittest.TestCase):
    def setUp(self):
        self.previous = {name: os.environ.get(name) for name in SPEC_ENV}

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_selection_names_the_spec_model_and_host(self):
        os.environ.update(SPEC_ENV)
        provider, model, endpoint = describe_llm("openai-spec", "mistral-small")
        self.assertEqual(provider, "openai-spec")
        self.assertEqual(model, "mistral-small")
        self.assertEqual(endpoint, "llm.example.com")

    def test_builtin_providers_keep_their_hardcoded_models(self):
        self.assertEqual(describe_llm("openai"), ("openai", OPENAI_MODEL, "api.openai.com"))
        self.assertEqual(describe_llm("anthropic"), ("anthropic", ANTHROPIC_MODEL, "api.anthropic.com"))

    def test_call_log_names_the_model_and_omits_the_key(self):
        class FakeMessage:
            content = "final answer"
            tool_calls = None
            model_extra = {"reasoning_content": "thought about sk-spec-test"}

        class FakeChoice:
            message = FakeMessage()
            finish_reason = "stop"

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **_kwargs):
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = FakeChat()

        with patch("openai.OpenAI", FakeOpenAI), self.assertLogs("app.llm", level="INFO") as captured:
            call_openai(
                [{"role": "user", "content": "use key sk-spec-test"}],
                [],
                "system prompt",
                model="shared-gpt-oss-120b",
                api_key="sk-spec-test",
                base_url="https://inference.sharonai.cloud/api/v1",
                name="openai-spec",
            )
        logged = "\n".join(captured.output)
        self.assertIn("LLM call provider=openai-spec", logged)
        self.assertIn("model=shared-gpt-oss-120b", logged)
        self.assertIn("endpoint=inference.sharonai.cloud", logged)
        self.assertIn("LLM input provider=openai-spec", logged)
        self.assertIn("system: system prompt", logged)
        self.assertIn("user: use key [redacted]", logged)
        self.assertIn("LLM output provider=openai-spec finish_reason=stop", logged)
        self.assertIn("assistant: final answer", logged)
        self.assertIn("[reasoning: thought about [redacted]]", logged)
        self.assertNotIn("sk-spec-test", logged)
