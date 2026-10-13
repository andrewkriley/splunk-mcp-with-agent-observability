"""Galileo traces panel: prefix filter and GALILEO_TRACES_PANEL flag."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.galileo_traces import (
    filter_sessions,
    session_belongs,
    session_prefix,
    traces_panel_enabled,
)
from app.main import app


class PrefixFilterTests(unittest.TestCase):
    def test_prefix_is_local_part_plus_workshop_chat(self):
        self.assertEqual(session_prefix("Ada@Example.com"), "ada-workshop-chat-")
        self.assertEqual(session_prefix("ada"), "ada-workshop-chat-")
        self.assertEqual(session_prefix(""), "")

    def test_ada_does_not_match_adam(self):
        self.assertTrue(session_belongs("ada-workshop-chat-abc", "ada"))
        self.assertFalse(session_belongs("adam-workshop-chat-abc", "ada"))
        self.assertFalse(session_belongs("workshop-chat-abc", "ada"))
        self.assertFalse(session_belongs("ada-workshop-chat-abc", ""))

    def test_filter_drops_other_users_and_keeps_newest_shape(self):
        records = [
            {"id": "1", "name": "bob-workshop-chat-1", "created_at": "2"},
            {"id": "2", "name": "ada-workshop-chat-old", "created_at": "1"},
            {"id": "3", "name": "ada-workshop-chat-new", "created_at": "3"},
            {"id": "4", "name": "adam-workshop-chat-x", "created_at": "9"},
        ]
        out = filter_sessions(records, "ada@example.com")
        self.assertEqual([row["id"] for row in out], ["3", "2"])
        self.assertTrue(all(row["name"].startswith("ada-workshop-chat-") for row in out))


class FlagTests(unittest.TestCase):
    def test_zero_disables_even_when_a_key_is_set(self):
        env = {"GALILEO_TRACES_PANEL": "0", "GALILEO_API_KEY": "sk-test-not-real"}
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(traces_panel_enabled())

    def test_default_on_when_key_present(self):
        env = {"GALILEO_API_KEY": "sk-test-not-real"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GALILEO_TRACES_PANEL", None)
            self.assertTrue(traces_panel_enabled())

    def test_on_without_key_stays_off(self):
        with patch.dict(os.environ, {"GALILEO_TRACES_PANEL": "1"}, clear=False):
            os.environ.pop("GALILEO_API_KEY", None)
            self.assertFalse(traces_panel_enabled())


class TracesApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_config_reports_the_flag(self):
        with (
            patch("app.main._configured_providers", return_value=["openai"]),
            patch("app.main.traces_panel_enabled", return_value=False),
        ):
            payload = self.client.get("/config").json()
        self.assertIn("galileo_traces_panel", payload)
        self.assertFalse(payload["galileo_traces_panel"])

    def test_disabled_panel_returns_404(self):
        with patch("app.main.traces_panel_enabled", return_value=False):
            listed = self.client.get("/galileo/sessions", headers={"X-Mcp-Lab-User": "ada"})
            detail = self.client.get("/galileo/sessions/abc", headers={"X-Mcp-Lab-User": "ada"})
        self.assertEqual(listed.status_code, 404)
        self.assertEqual(detail.status_code, 404)

    def test_list_uses_header_prefix_not_a_query_param(self):
        records = [
            {"id": "mine", "name": "ada-workshop-chat-1", "created_at": "2", "traces": []},
            {"id": "other", "name": "bob-workshop-chat-1", "created_at": "3", "traces": []},
        ]
        with (
            patch("app.main.traces_panel_enabled", return_value=True),
            patch("app.main.fetch_sessions", return_value=records),
        ):
            response = self.client.get(
                "/galileo/sessions?user=bob",
                headers={"X-Mcp-Lab-User": "Ada@Example.com"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"], "ada")
        self.assertEqual(body["prefix"], "ada-workshop-chat-")
        ids = [row["id"] for row in body["sessions"]]
        self.assertEqual(ids, ["mine"])
        self.assertNotIn("bob", ids)
        self.assertNotIn("@", response.text)
        self.assertNotIn("GALILEO_API_KEY", response.text)

    def test_me_returns_local_part_only(self):
        with patch("app.main.traces_panel_enabled", return_value=True):
            response = self.client.get("/me", headers={"X-Mcp-Lab-User": "Ada@Example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": "ada"})
        self.assertNotIn("@", response.text)

    def test_session_detail_404s_for_another_user(self):
        record = {"id": "x", "name": "bob-workshop-chat-1", "created_at": "1", "traces": []}
        with (
            patch("app.main.traces_panel_enabled", return_value=True),
            patch("app.main.fetch_session", return_value=record),
        ):
            response = self.client.get(
                "/galileo/sessions/x",
                headers={"X-Mcp-Lab-User": "ada"},
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
