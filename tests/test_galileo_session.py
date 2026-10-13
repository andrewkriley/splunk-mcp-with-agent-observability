#!/usr/bin/env python3
"""Galileo session names and one session per browser conversation. No live Galileo."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability import _galileo_sessions, run_traced_turn
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

    def tearDown(self) -> None:
        _galileo_sessions.clear()

    def _run_turns(self, start_session, galileo, turns: list[tuple[str, str]]) -> MagicMock:
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo.return_value = context
        galileo.get_logger_instance.return_value = logger

        async def fake_turn(*_args, **_kwargs):
            return "ok"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability.start_session", start_session),
            patch("app.observability.galileo_context", galileo),
            patch("app.agent._needs_splunk", return_value=False),
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


if __name__ == "__main__":
    unittest.main()
