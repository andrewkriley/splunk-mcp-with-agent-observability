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
