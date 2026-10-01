"""Traces export to Observability Cloud, and Galileo settings stay hidden."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.observability import _ao_session_id, _ao_sessions, activate_o11y, o11y_configured, o11y_errors, o11y_target

AO_ENV = (
    "SPLUNK_AO_API_KEY",
    "SPLUNK_AO_CONSOLE_URL",
    "SPLUNK_AO_API_URL",
    "SPLUNK_AO_REALM",
    "SPLUNK_AO_O11Y_TOKEN",
    "SPLUNK_AO_O11Y_API_TOKEN",
    "SPLUNK_AO_DESTINATION",
    "SPLUNK_AO_PROJECT",
    "SPLUNK_AO_AGENT_STREAM",
    "GALILEO_API_KEY",
    "GALILEO_CONSOLE_URL",
    "GALILEO_PROJECT",
    "GALILEO_LOG_STREAM",
)


class O11yDestinationTests(unittest.TestCase):
    def setUp(self):
        self.previous = {name: os.environ.get(name) for name in AO_ENV}
        for name in AO_ENV:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_realm_and_token_are_required(self):
        self.assertFalse(o11y_configured())
        self.assertEqual(o11y_errors(), ["Observability Cloud needs SPLUNK_AO_REALM and SPLUNK_AO_O11Y_TOKEN"])
        os.environ["SPLUNK_AO_REALM"] = "not a realm"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        self.assertFalse(o11y_configured())
        self.assertIn("SPLUNK_AO_REALM", o11y_errors()[0])

    def test_target_uses_the_realm(self):
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        self.assertTrue(o11y_configured())
        self.assertEqual(o11y_target(), "ingest.au0.observability.splunkcloud.com")

    def test_turn_hides_galileo_settings(self):
        os.environ["GALILEO_API_KEY"] = "test-galileo-key"
        os.environ["GALILEO_CONSOLE_URL"] = ""
        os.environ["GALILEO_PROJECT"] = "galileo-project"
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_O11Y_API_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_PROJECT"] = "o11y-project"
        os.environ["SPLUNK_AO_AGENT_STREAM"] = "traces"

        with activate_o11y():
            self.assertEqual(os.environ["SPLUNK_AO_REALM"], "au0")
            self.assertEqual(os.environ["SPLUNK_AO_O11Y_TOKEN"], "test-o11y-token")
            self.assertNotIn("SPLUNK_AO_O11Y_API_TOKEN", os.environ)
            self.assertNotIn("SPLUNK_AO_API_KEY", os.environ)
            self.assertNotIn("GALILEO_API_KEY", os.environ)
            self.assertNotIn("GALILEO_CONSOLE_URL", os.environ)
            self.assertNotIn("GALILEO_PROJECT", os.environ)
            self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "o11y-project")
            self.assertEqual(os.environ["SPLUNK_AO_AGENT_STREAM"], "traces")

        self.assertEqual(os.environ["GALILEO_API_KEY"], "test-galileo-key")
        self.assertEqual(os.environ["GALILEO_PROJECT"], "galileo-project")
        self.assertEqual(os.environ["SPLUNK_AO_API_KEY"], "test-galileo-key")

    def test_session_external_id_is_the_conversation_id(self):
        _ao_sessions.clear()
        with patch("app.observability.start_session", return_value="api-session-id") as started:
            first = _ao_session_id("conv-1")
            second = _ao_session_id("conv-1")
        self.assertEqual(first, "conv-1")
        self.assertEqual(second, "conv-1")
        started.assert_called_once_with(name="workshop-chat-conv-1", external_id="conv-1")
        _ao_sessions.clear()

    def test_config_reports_observability_cloud_and_the_page_has_no_switch(self):
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        client = TestClient(app)
        body = client.get("/config").json()
        self.assertTrue(body["o11y_configured"])
        self.assertEqual(body["o11y_target"], "ingest.au0.observability.splunkcloud.com")
        self.assertNotIn("destinations", body)
        self.assertNotIn("test-o11y-token", client.get("/config").text)

        html = client.get("/").text
        self.assertNotIn('id="destination"', html)
        self.assertNotIn("Galileo", html)
        self.assertIn("Splunk Observability Cloud", html)

        async def capture(*_args, **kwargs):
            capture.seen = kwargs
            return "ok"

        with patch("app.main.run_traced_turn", capture):
            response = client.post("/chat", json={"message": "hi", "conversation_id": "c-ao"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("destination", capture.seen)
