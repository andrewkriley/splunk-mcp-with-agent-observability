#!/usr/bin/env python3
"""Galileo session names and one session per browser conversation. No live Galileo."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability import (
    _galileo_session_inputs,
    _galileo_sessions,
    _stamp_session_io,
    run_traced_turn,
    session_io_for_turn,
)
from app.workshop_user import galileo_session_name, workshop_user_from_value


class WorkshopUserTest(unittest.TestCase):
    def test_local_part_ignores_domain(self):
        self.assertEqual(workshop_user_from_value("Ada@Example.com"), "ada")
        self.assertEqual(workshop_user_from_value("ada"), "ada")
        self.assertEqual(workshop_user_from_value(""), "")

    def test_session_name_prepends_user(self):
        cid = "conv-123"
        self.assertEqual(
            galileo_session_name(cid, "Ada@Example.com"),
            "ada-workshop-chat-conv-123",
        )
        self.assertEqual(galileo_session_name(cid, ""), "workshop-chat-conv-123")


class ConversationSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        _galileo_sessions.clear()
        _galileo_session_inputs.clear()

    def tearDown(self) -> None:
        _galileo_sessions.clear()
        _galileo_session_inputs.clear()

    def _run_turns(
        self,
        start_session,
        galileo,
        turns: list[tuple[str, str]],
        *,
        stamp=None,
        needs_splunk=None,
    ) -> MagicMock:
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo.return_value = context
        galileo.get_logger_instance.return_value = logger
        if stamp is None:
            stamp = MagicMock()
        if needs_splunk is None:
            needs_splunk = MagicMock(return_value=False)

        async def fake_turn(*_args, **_kwargs):
            return "ok"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability.start_session", start_session),
            patch("app.observability.galileo_context", galileo),
            patch("app.observability._stamp_session_io", stamp),
            patch("app.agent._needs_splunk", needs_splunk),
            patch("app.agent.run_agent_turn", fake_turn),
        ):
            for message, conversation_id in turns:
                asyncio.run(run_traced_turn(message, conversation_id, user="ada@example.com"))
        return logger

    def test_session_is_created_inside_context_with_external_id(self):
        start_session = MagicMock(return_value="sid-1")
        galileo = MagicMock()
        logger = self._run_turns(start_session, galileo, [("hello", "page-1")])

        self.assertNotIn("session_id", galileo.call_args.kwargs)
        start_session.assert_called_once_with(
            name="ada-workshop-chat-page-1",
            external_id="page-1",
        )
        galileo.set_session.assert_called_with("sid-1")
        logger.start_trace.assert_called_once_with(input="hello")
        logger.conclude.assert_called_once_with(output="ok", conclude_all=True)
        galileo.flush.assert_called()

    def test_follow_up_reuses_session_new_conversation_starts_another(self):
        start_session = MagicMock(side_effect=["sid-1", "sid-2"])
        galileo = MagicMock()
        logger = self._run_turns(
            start_session,
            galileo,
            [
                ("first", "page-1"),
                ("second", "page-1"),
                ("refresh", "page-2"),
            ],
        )

        self.assertEqual(start_session.call_count, 2)
        start_session.assert_any_call(name="ada-workshop-chat-page-1", external_id="page-1")
        start_session.assert_any_call(name="ada-workshop-chat-page-2", external_id="page-2")
        self.assertEqual(
            [call.args[0] for call in galileo.set_session.call_args_list],
            ["sid-1", "sid-1", "sid-2"],
        )
        self.assertEqual(
            [call.kwargs["input"] for call in logger.start_trace.call_args_list],
            ["first", "second", "refresh"],
        )
        self.assertEqual(_galileo_sessions["page-1"], "sid-1")
        self.assertEqual(_galileo_sessions["page-2"], "sid-2")

    def test_first_turn_stamps_session_row_after_flush(self):
        start_session = MagicMock(return_value="sid-1")
        galileo = MagicMock()
        stamp = MagicMock()
        order: list[str] = []
        galileo.flush.side_effect = lambda *a, **k: order.append("flush")
        stamp.side_effect = lambda *a, **k: order.append("stamp")

        self._run_turns(start_session, galileo, [("hello", "page-1")], stamp=stamp)

        stamp.assert_called_once()
        args = stamp.call_args.args
        self.assertEqual(args[1:], ("page-1", "hello", "ok", "sid-1"))
        self.assertEqual(order, ["flush", "stamp"])

    def test_follow_up_stamps_latest_output_on_same_session(self):
        start_session = MagicMock(return_value="sid-1")
        galileo = MagicMock()
        stamp = MagicMock()
        self._run_turns(
            start_session,
            galileo,
            [("first", "page-1"), ("second", "page-1")],
            stamp=stamp,
        )
        self.assertEqual(
            [call.args[1:] for call in stamp.call_args_list],
            [
                ("page-1", "first", "ok", "sid-1"),
                ("page-1", "second", "ok", "sid-1"),
            ],
        )

    def test_intent_router_runs_after_start_trace(self):
        start_session = MagicMock(return_value="sid-1")
        galileo = MagicMock()
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo.return_value = context
        galileo.get_logger_instance.return_value = logger
        order: list[str] = []
        logger.start_trace.side_effect = lambda **kwargs: order.append("trace")

        def needs(*_args, **_kwargs):
            order.append("needs")
            return False

        async def fake_turn(*_args, **_kwargs):
            return "ok"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability.start_session", start_session),
            patch("app.observability.galileo_context", galileo),
            patch("app.observability._stamp_session_io"),
            patch("app.agent._needs_splunk", needs),
            patch("app.agent.run_agent_turn", fake_turn),
        ):
            asyncio.run(run_traced_turn("hello", "page-1", user="ada@example.com"))
        self.assertEqual(order, ["trace", "needs"])


class SessionIoStampTest(unittest.TestCase):
    def setUp(self) -> None:
        _galileo_session_inputs.clear()
        self.previous = {
            name: os.environ.get(name)
            for name in ("GALILEO_API_KEY", "GALILEO_PROJECT_ID", "GALILEO_CONSOLE_URL", "GALILEO_API_URL")
        }

    def tearDown(self) -> None:
        _galileo_session_inputs.clear()
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_session_row_keeps_first_question(self):
        self.assertEqual(session_io_for_turn("page-1", "first", "ok"), ("first", "ok"))
        self.assertEqual(session_io_for_turn("page-1", "second", "later"), ("first", "later"))
        self.assertEqual(session_io_for_turn("page-2", "refresh", "new"), ("refresh", "new"))

    def test_stamp_posts_complete_trace_onto_the_session(self):
        os.environ["GALILEO_API_KEY"] = "test-key-not-a-secret"
        os.environ["GALILEO_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"
        os.environ["GALILEO_CONSOLE_URL"] = "https://console.multitenant.galileocloud.io"
        logger = MagicMock()
        logger.project_id = "11111111-1111-1111-1111-111111111111"
        logger.log_stream_id = "22222222-2222-2222-2222-222222222222"
        posted: list[tuple[str, dict]] = []

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=0):
            posted.append((req.full_url, json.loads(req.data.decode())))
            self.assertEqual(req.get_header("Galileo-api-key"), "test-key-not-a-secret")
            return FakeResp()

        with patch("app.observability.urllib.request.urlopen", fake_urlopen):
            _stamp_session_io(logger, "page-1", "hello", "ok", "sid-1")
            _stamp_session_io(logger, "page-1", "second", "later", "sid-1")

        self.assertEqual(len(posted), 2)
        self.assertTrue(posted[0][0].endswith("/ingest/traces/11111111-1111-1111-1111-111111111111"))
        self.assertIn("api.multitenant.galileocloud.io", posted[0][0])
        first_trace = posted[0][1]["traces"][0]
        self.assertEqual(first_trace["input"], "hello")
        self.assertEqual(first_trace["output"], "ok")
        self.assertEqual(posted[0][1]["session_id"], "sid-1")
        self.assertEqual(posted[0][1]["session_external_id"], "page-1")
        self.assertTrue(posted[0][1]["is_complete"])
        self.assertEqual(posted[1][1]["traces"][0]["input"], "hello")
        self.assertEqual(posted[1][1]["traces"][0]["output"], "later")

    def test_stamp_is_a_noop_without_a_key(self):
        os.environ.pop("GALILEO_API_KEY", None)
        logger = MagicMock()
        logger.project_id = "11111111-1111-1111-1111-111111111111"
        with patch("app.observability.urllib.request.urlopen") as urlopen:
            _stamp_session_io(logger, "page-1", "hello", "ok", "sid-1")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
