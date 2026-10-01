"""Chat API validation, timeout, and error text."""

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.previous_ao_key = os.environ.get("SPLUNK_AO_API_KEY")
        self.previous_ao_console = os.environ.get("SPLUNK_AO_CONSOLE_URL")
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_CONSOLE_URL"] = "https://app.galileo.ai"

    def tearDown(self):
        if self.previous_ao_key is None:
            os.environ.pop("SPLUNK_AO_API_KEY", None)
        else:
            os.environ["SPLUNK_AO_API_KEY"] = self.previous_ao_key
        if self.previous_ao_console is None:
            os.environ.pop("SPLUNK_AO_CONSOLE_URL", None)
        else:
            os.environ["SPLUNK_AO_CONSOLE_URL"] = self.previous_ao_console

    def test_blank_message_names_the_problem(self):
        response = self.client.post("/chat", json={"message": "   ", "conversation_id": "c1"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("message is empty", response.text)

    def test_overlong_message_is_rejected(self):
        response = self.client.post(
            "/chat",
            json={"message": "x" * 4001, "conversation_id": "c1"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("4000", response.text)

    def test_unknown_provider_returns_its_reason(self):
        response = self.client.post(
            "/chat",
            json={"message": "hi", "conversation_id": "c1", "provider": "not-a-provider"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no API key", response.json()["detail"])

    def test_index_locks_search_and_reads_error_detail(self):
        html = self.client.get("/").text
        self.assertIn('id="submit" type="submit" disabled', html)
        self.assertIn("submitButton.disabled = true", html)
        self.assertIn("errorDetail", html)
        self.assertIn('maxlength="4000"', html)
        self.assertIn("/chat/stream", html)
        self.assertIn('addMessage("pending", "Noodling...")', html)
        self.assertIn('pending.textContent = "Searching Splunk..."', html)
        self.assertIn('id="destination"', html)
        self.assertIn("Observability Cloud", html)
        self.assertIn("destination: destinationSelect.value || null", html)

    def test_timeout_returns_a_readable_504(self):
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(0.3)

        with (
            patch("app.main.run_traced_turn", slow),
            patch("app.main.CHAT_TIMEOUT_SECONDS", 0.05),
        ):
            response = self.client.post("/chat", json={"message": "hi", "conversation_id": "c-timeout"})
        self.assertEqual(response.status_code, 504)
        self.assertIn("timed out", response.json()["detail"])

    def test_failure_returns_the_message_with_secrets_redacted(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("upstream failed for workshop-token-value")

        previous = os.environ.get("SPLUNK_MCP_TOKEN")
        os.environ["SPLUNK_MCP_TOKEN"] = "workshop-token-value"
        try:
            with patch("app.main.run_traced_turn", boom):
                response = self.client.post("/chat", json={"message": "hi", "conversation_id": "c-fail"})
        finally:
            if previous is None:
                os.environ.pop("SPLUNK_MCP_TOKEN", None)
            else:
                os.environ["SPLUNK_MCP_TOKEN"] = previous

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("upstream failed", detail)
        self.assertNotIn("workshop-token-value", detail)
        self.assertIn("[redacted]", detail)

    def test_stream_reports_a_tool_call_separately_from_the_model(self):
        from app.observability import announce

        async def scripted(*_args, **_kwargs):
            await announce("model")
            await announce("tool", "splunk_run_query")
            await announce("model")
            return "three notables"

        with patch("app.main.run_traced_turn", scripted):
            with self.client.stream(
                "POST",
                "/chat/stream",
                json={"message": "show notables", "conversation_id": "c-stream"},
            ) as response:
                self.assertEqual(response.status_code, 200)
                body = "".join(response.iter_text())

        self.assertLess(body.index('"phase": "model"'), body.index('"phase": "tool"'))
        self.assertIn("splunk_run_query", body)
        self.assertIn("three notables", body)
        self.assertLess(body.index('"phase": "tool"'), body.rindex('"phase": "model"'))
