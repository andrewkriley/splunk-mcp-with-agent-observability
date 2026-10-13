"""Galileo traces panel: prefix filter and GALILEO_TRACES_PANEL flag."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.galileo_traces import (
    fetch_sessions,
    filter_sessions,
    resolve_project_id,
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


class ProjectLookupTests(unittest.TestCase):
    def setUp(self):
        import app.galileo_traces as traces

        traces._PROJECT_IDS.clear()
        traces._STREAM_IDS.clear()

    def test_uuid_env_is_used_as_is(self):
        pid = "123e4567-e89b-12d3-a456-426614174000"
        with patch.dict(os.environ, {"GALILEO_PROJECT_ID": pid, "GALILEO_PROJECT": "named"}, clear=False):
            self.assertEqual(resolve_project_id(http=lambda *a, **k: (500, None)), pid)

    def test_project_name_is_looked_up_via_paginated_list(self):
        pid = "123e4567-e89b-12d3-a456-426614174000"
        calls: list[str] = []

        def http(method, url, **_kwargs):
            calls.append(f"{method} {url}")
            return 200, {"projects": [{"id": pid, "name": "andreril-splunk-mcp-lab"}]}

        env = {
            "GALILEO_PROJECT": "andreril-splunk-mcp-lab",
            "GALILEO_API_URL": "https://api.example.test",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GALILEO_PROJECT_ID", None)
            self.assertEqual(resolve_project_id(http=http), pid)
        self.assertTrue(any("/v2/projects/paginated" in url for url in calls))
        self.assertTrue(all("andreril-splunk-mcp-lab/" not in url for url in calls))

    def test_session_search_uses_project_uuid_not_the_name(self):
        pid = "123e4567-e89b-12d3-a456-426614174000"
        calls: list[str] = []

        def http(method, url, **kwargs):
            calls.append(f"{method} {url}")
            if url.endswith("/projects/paginated"):
                return 200, {"projects": [{"id": pid, "name": "andreril-splunk-mcp-lab"}]}
            if url.endswith("/log_streams"):
                return 200, [{"id": "stream-1", "name": "shared"}]
            if url.endswith("/sessions/search"):
                self.assertIn(pid, url)
                self.assertNotIn("andreril-splunk-mcp-lab", url)
                return 200, {
                    "records": [
                        {"id": "s1", "name": "ada-workshop-chat-1", "created_at": "9"},
                        {"id": "s2", "name": "bob-workshop-chat-1", "created_at": "8"},
                    ]
                }
            return 404, None

        env = {
            "GALILEO_PROJECT": "andreril-splunk-mcp-lab",
            "GALILEO_LOG_STREAM": "shared",
            "GALILEO_API_URL": "https://api.example.test",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GALILEO_PROJECT_ID", None)
            os.environ.pop("GALILEO_LOG_STREAM_ID", None)
            rows = fetch_sessions("ada", http=http)
        self.assertEqual([row["id"] for row in rows], ["s1"])
        self.assertTrue(any(url.endswith("/sessions/search") for url in calls))
        self.assertTrue(any(pid in url for url in calls))
        self.assertFalse(any("andreril-splunk-mcp-lab/" in url for url in calls))


if __name__ == "__main__":
    unittest.main()
