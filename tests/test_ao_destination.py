"""Galileo and Observability Cloud can both be configured, and a turn picks one."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.observability import (
    activate_ao_destination,
    ao_destination_target,
    configured_ao_destinations,
    default_ao_destination,
    resolve_ao_destination,
)

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


class AoDestinationTests(unittest.TestCase):
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

    def test_galileo_key_alias_uses_the_default_console(self):
        os.environ["GALILEO_API_KEY"] = "test-galileo-key"
        self.assertEqual(configured_ao_destinations(), ["standalone"])
        self.assertEqual(ao_destination_target("standalone"), "app.galileo.ai")
        self.assertEqual(default_ao_destination(), "standalone")

    def test_console_override_rejects_userinfo(self):
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_CONSOLE_URL"] = "https://user:secret@console.example.com"
        self.assertEqual(configured_ao_destinations(), [])

    def test_both_destinations_keep_their_own_names(self):
        os.environ["GALILEO_API_KEY"] = "test-galileo-key"
        os.environ["GALILEO_CONSOLE_URL"] = "https://console.example.com"
        os.environ["GALILEO_PROJECT"] = "galileo-project"
        os.environ["GALILEO_LOG_STREAM"] = "lab"
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_O11Y_API_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_PROJECT"] = "o11y-project"
        os.environ["SPLUNK_AO_AGENT_STREAM"] = "traces"
        self.assertEqual(configured_ao_destinations(), ["standalone", "o11y"])
        self.assertEqual(ao_destination_target("o11y"), "ingest.au0.observability.splunkcloud.com")

        with activate_ao_destination("o11y"):
            self.assertEqual(os.environ["SPLUNK_AO_REALM"], "au0")
            self.assertEqual(os.environ["SPLUNK_AO_O11Y_TOKEN"], "test-o11y-token")
            self.assertNotIn("SPLUNK_AO_O11Y_API_TOKEN", os.environ)
            self.assertNotIn("SPLUNK_AO_API_KEY", os.environ)
            self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "o11y-project")
            self.assertEqual(os.environ["SPLUNK_AO_AGENT_STREAM"], "traces")

        with activate_ao_destination("standalone"):
            self.assertEqual(os.environ["SPLUNK_AO_API_KEY"], "test-galileo-key")
            self.assertEqual(os.environ["SPLUNK_AO_CONSOLE_URL"], "https://console.example.com")
            self.assertNotIn("SPLUNK_AO_REALM", os.environ)
            self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "galileo-project")
            self.assertEqual(os.environ["SPLUNK_AO_AGENT_STREAM"], "lab")

        self.assertEqual(os.environ["GALILEO_API_KEY"], "test-galileo-key")
        self.assertNotIn("SPLUNK_AO_API_KEY", os.environ)
        self.assertEqual(os.environ["SPLUNK_AO_REALM"], "au0")
        self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "o11y-project")

    def test_blank_galileo_console_is_hidden_for_the_turn(self):
        os.environ["GALILEO_API_KEY"] = "test-galileo-key"
        os.environ["GALILEO_CONSOLE_URL"] = ""
        os.environ["GALILEO_PROJECT"] = "galileo-project"
        os.environ["GALILEO_LOG_STREAM"] = "lab"
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_PROJECT"] = "o11y-project"
        os.environ["SPLUNK_AO_AGENT_STREAM"] = "traces"

        with activate_ao_destination("o11y"):
            self.assertNotIn("GALILEO_CONSOLE_URL", os.environ)
            self.assertNotIn("GALILEO_API_KEY", os.environ)
            self.assertNotIn("GALILEO_PROJECT", os.environ)
            self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "o11y-project")

        with activate_ao_destination("standalone"):
            self.assertNotIn("GALILEO_CONSOLE_URL", os.environ)
            self.assertEqual(os.environ["SPLUNK_AO_CONSOLE_URL"], "https://app.galileo.ai")
            self.assertEqual(os.environ["SPLUNK_AO_PROJECT"], "galileo-project")

        self.assertEqual(os.environ["GALILEO_CONSOLE_URL"], "")
        self.assertEqual(os.environ["GALILEO_PROJECT"], "galileo-project")

    def test_default_follows_splunk_ao_destination(self):
        os.environ["GALILEO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_REALM"] = "us1"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_DESTINATION"] = "o11y"
        self.assertEqual(resolve_ao_destination(None), "o11y")
        self.assertEqual(resolve_ao_destination("standalone"), "standalone")
        with self.assertRaises(ValueError):
            resolve_ao_destination("elsewhere")

    def test_config_lists_both_and_chat_sends_the_choice(self):
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        os.environ["SPLUNK_AO_REALM"] = "au0"
        os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-o11y-token"
        os.environ["SPLUNK_AO_DESTINATION"] = "standalone"
        client = TestClient(app)
        body = client.get("/config").json()
        self.assertEqual(body["destinations"], ["standalone", "o11y"])
        self.assertEqual(body["default_destination"], "standalone")
        self.assertNotIn("test-o11y-token", client.get("/config").text)

        async def capture(*_args, **kwargs):
            capture.seen = kwargs
            return "ok"

        with patch("app.main.run_traced_turn", capture):
            response = client.post(
                "/chat",
                json={"message": "hi", "conversation_id": "c-ao", "destination": "o11y"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(capture.seen["destination"], "o11y")

    def test_unconfigured_choice_is_rejected(self):
        os.environ["SPLUNK_AO_API_KEY"] = "test-galileo-key"
        client = TestClient(app)
        response = client.post(
            "/chat",
            json={"message": "hi", "conversation_id": "c-ao", "destination": "o11y"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not configured", response.json()["detail"])
