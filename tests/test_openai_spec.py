"""OpenAI-spec provider: key, endpoint, and the model list shown to the UI."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.observability import openai_spec_config

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
        self.previous = {name: os.environ.get(name) for name in SPEC_ENV}

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
