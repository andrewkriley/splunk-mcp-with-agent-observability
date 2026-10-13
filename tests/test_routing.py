"""Routing, history, and per-request MCP session behavior."""

import asyncio
import os
import unittest

from unittest.mock import AsyncMock, MagicMock, patch

from app.agent import (
    EXCLUDED_TOOLS,
    INFRA_KEYWORDS,
    INTENT_ROUTER_SYSTEM,
    SECURITY_KEYWORDS,
    _matches_any,
    _needs_splunk,
    _parse_yes_no,
    _scoped_tools,
    run_agent_turn,
)
from app.observability import (
    MAX_HISTORY_TURNS,
    _mcp_session,
    prior_turns,
    remember_turn,
    run_traced_turn,
    set_mcp_session,
)


class KeywordTests(unittest.TestCase):
    def test_power_does_not_match_powershell(self):
        self.assertFalse(_matches_any("how do I use powershell", INFRA_KEYWORDS))

    def test_power_matches_a_power_question(self):
        self.assertTrue(_matches_any("what is the PDU power draw", INFRA_KEYWORDS))

    def test_hack_does_not_match_inside_another_word(self):
        self.assertFalse(_matches_any("the shack by the lake", SECURITY_KEYWORDS))

    def test_severity_and_notables_match_security(self):
        self.assertTrue(_matches_any("any high severity notables in the last 30 days", SECURITY_KEYWORDS))

    def test_phrase_matches_on_word_boundaries(self):
        self.assertTrue(_matches_any("show brute force attempts", SECURITY_KEYWORDS))
        self.assertFalse(_matches_any("bruteforce", SECURITY_KEYWORDS))


class ToolScopeTests(unittest.TestCase):
    def test_general_drops_user_list_and_saia_tools(self):
        tools = [
            {"name": "splunk_run_query"},
            {"name": "splunk_get_user_list"},
            {"name": "saia_generate_spl"},
            {"name": "splunk_get_indexes"},
        ]
        names = [tool["name"] for tool in _scoped_tools(tools, "general")]
        self.assertEqual(names, ["splunk_run_query", "splunk_get_indexes"])
        self.assertIn("saia_generate_spl", EXCLUDED_TOOLS)

    def test_security_stays_on_its_allowlist(self):
        tools = [
            {"name": "splunk_run_query"},
            {"name": "splunk_get_user_list"},
            {"name": "splunk_get_indexes"},
            {"name": "splunk_get_metadata"},
        ]
        names = [tool["name"] for tool in _scoped_tools(tools, "security")]
        self.assertEqual(names, ["splunk_run_query", "splunk_get_metadata"])


class IntentGateTests(unittest.TestCase):
    def setUp(self):
        self.previous_mode = os.environ.get("SPLUNK_INTENT_MODE")
        os.environ["SPLUNK_INTENT_MODE"] = "keywords"

    def tearDown(self):
        if self.previous_mode is None:
            os.environ.pop("SPLUNK_INTENT_MODE", None)
        else:
            os.environ["SPLUNK_INTENT_MODE"] = self.previous_mode

    def test_plain_question_skips_splunk(self):
        self.assertFalse(_needs_splunk("what model provider are we using"))

    def test_notables_question_uses_splunk(self):
        self.assertTrue(_needs_splunk("any high severity notables in the last 30 days"))

    def test_follow_up_keeps_splunk_after_a_matching_turn(self):
        history = [
            {"role": "user", "content": "show notables"},
            {"role": "assistant", "content": "there were 3"},
        ]
        self.assertTrue(_needs_splunk("what about the last 7 days?", history))

    def test_powershell_does_not_count_as_power(self):
        self.assertFalse(_needs_splunk("how do I use powershell"))

    def test_added_trigger_words_open_splunk(self):
        for question in ("check infra", "review security", "any threats", "show events"):
            self.assertTrue(_needs_splunk(question), question)

    def test_noteables_misses_the_static_list(self):
        self.assertFalse(_needs_splunk("what noteables do we have"))
        self.assertFalse(_matches_any("what noteables do we have", SECURITY_KEYWORDS))
        self.assertTrue(_needs_splunk("any high severity notables in the last 30 days"))

    def test_yes_no_parser_reads_a_leading_answer(self):
        self.assertTrue(_parse_yes_no("yes"))
        self.assertFalse(_parse_yes_no("No, that is a greeting."))
        self.assertIsNone(_parse_yes_no("not sure"))

    def test_router_prompt_teaches_the_noteables_misspelling(self):
        self.assertIn("noteables", INTENT_ROUTER_SYSTEM)
        self.assertIn("notables", INTENT_ROUTER_SYSTEM)
        self.assertIn("yes or no", INTENT_ROUTER_SYSTEM)

    def test_model_mode_asks_the_llm_for_noteables_and_notables(self):
        os.environ["SPLUNK_INTENT_MODE"] = "model"
        seen = []

        def fake_router(_provider, _model, question):
            seen.append(question)
            return "yes"

        with patch("app.agent._router_text", fake_router):
            self.assertTrue(
                _needs_splunk(
                    "what noteables do we have",
                    provider="openai-spec",
                    model="bedrock-claude",
                )
            )
            self.assertTrue(
                _needs_splunk(
                    "show notables",
                    provider="openai-spec",
                    model="bedrock-claude",
                )
            )
        self.assertEqual(len(seen), 2)
        self.assertIn("noteables", seen[0])
        self.assertIn("notables", seen[1])

    def test_both_asks_the_model_only_when_the_list_misses(self):
        os.environ["SPLUNK_INTENT_MODE"] = "both"
        with patch("app.agent._model_needs_splunk", return_value=True) as router:
            self.assertTrue(_needs_splunk("what noteables do we have", provider="openai-spec"))
            self.assertTrue(_needs_splunk("show notables"))
        router.assert_called_once()

    def test_model_mode_can_decline_a_keyword_hit(self):
        os.environ["SPLUNK_INTENT_MODE"] = "model"
        with patch("app.agent._model_needs_splunk", return_value=False) as router:
            self.assertFalse(_needs_splunk("show notables"))
        router.assert_called_once()

    def test_router_error_falls_back_to_the_keyword_list(self):
        os.environ["SPLUNK_INTENT_MODE"] = "both"
        with patch("app.agent._model_needs_splunk", side_effect=RuntimeError("router down")):
            self.assertFalse(_needs_splunk("what noteables do we have"))
        os.environ["SPLUNK_INTENT_MODE"] = "model"
        with patch("app.agent._model_needs_splunk", side_effect=RuntimeError("router down")):
            self.assertTrue(_needs_splunk("show notables"))

    def test_chat_category_is_offered_no_tools(self):
        tools = [{"name": "splunk_run_query", "description": "", "input_schema": {}}]
        self.assertEqual(_scoped_tools(tools, "chat"), [])

    def test_plain_question_does_not_open_mcp(self):
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo = MagicMock(return_value=context)
        galileo.get_logger_instance.return_value = logger

        async def fake_turn(*args, **kwargs):
            fake_turn.seen = (args, kwargs)
            return "direct answer"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability._galileo_session_id", return_value="sess"),
            patch("app.observability.galileo_context", galileo),
            patch("app.observability.mcp_client.splunk_mcp_session") as session,
            patch("app.agent.run_agent_turn", fake_turn),
        ):
            reply = asyncio.run(run_traced_turn("what model are we using", "gate-plain"))

        self.assertEqual(reply, "direct answer")
        session.assert_not_called()
        args, kwargs = fake_turn.seen
        self.assertEqual(args[1], [])
        self.assertFalse(kwargs["use_splunk"])

    def test_splunk_question_opens_mcp(self):
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo = MagicMock(return_value=context)
        galileo.get_logger_instance.return_value = logger
        session = AsyncMock()
        session.__aenter__.return_value = "live-session"
        tools = [{"name": "splunk_run_query", "description": "", "input_schema": {}}]

        async def fake_turn(*args, **kwargs):
            fake_turn.seen = (args, kwargs)
            return "3 notables"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability._galileo_session_id", return_value="sess"),
            patch("app.observability.galileo_context", galileo),
            patch("app.observability.mcp_client.splunk_mcp_session", return_value=session),
            patch("app.observability.mcp_client.list_splunk_tools", AsyncMock(return_value=tools)),
            patch("app.agent.run_agent_turn", fake_turn),
        ):
            reply = asyncio.run(run_traced_turn("show notables", "gate-splunk"))

        self.assertEqual(reply, "3 notables")
        session.__aenter__.assert_awaited()
        args, kwargs = fake_turn.seen
        self.assertEqual(args[1], tools)
        self.assertNotIn("use_splunk", kwargs)

    def test_noteables_model_mode_opens_mcp(self):
        os.environ["SPLUNK_INTENT_MODE"] = "model"
        logger = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False
        galileo = MagicMock(return_value=context)
        galileo.get_logger_instance.return_value = logger
        session = AsyncMock()
        session.__aenter__.return_value = "live-session"
        tools = [{"name": "splunk_run_query", "description": "", "input_schema": {}}]

        async def fake_turn(*args, **kwargs):
            fake_turn.seen = (args, kwargs)
            return "4 notable events"

        with (
            patch("app.observability.apply_galileo_console_url"),
            patch("app.observability._galileo_session_id", return_value="sess"),
            patch("app.observability.galileo_context", galileo),
            patch("app.observability.mcp_client.splunk_mcp_session", return_value=session),
            patch("app.observability.mcp_client.list_splunk_tools", AsyncMock(return_value=tools)),
            patch("app.agent._router_text", return_value="yes"),
            patch("app.agent.run_agent_turn", fake_turn),
        ):
            reply = asyncio.run(
                run_traced_turn(
                    "what noteables do we have",
                    "gate-noteables",
                    provider="openai-spec",
                    model="bedrock-claude",
                )
            )

        self.assertEqual(reply, "4 notable events")
        session.__aenter__.assert_awaited()
        args, kwargs = fake_turn.seen
        self.assertEqual(args[1], tools)
        self.assertNotIn("use_splunk", kwargs)

    def test_chat_turn_calls_the_model_without_splunk_tools(self):
        seen = {}

        def fake_openai(messages, tools, system_prompt, **_kwargs):
            seen["tools"] = tools
            seen["system"] = system_prompt
            message = MagicMock()
            message.content = "shared-gpt-oss-120b"
            message.tool_calls = None
            choice = MagicMock()
            choice.message = message
            response = MagicMock()
            response.choices = [choice]
            return response

        tools = [{"name": "splunk_run_query", "description": "", "input_schema": {}}]
        with (
            patch("app.agent.galileo_context") as galileo,
            patch("app.observability.call_openai", fake_openai),
        ):
            galileo.get_logger_instance.return_value = MagicMock()
            reply = asyncio.run(
                run_agent_turn("what model are we using", tools, provider="openai", use_splunk=False)
            )

        self.assertEqual(reply, "shared-gpt-oss-120b")
        self.assertEqual(seen["tools"], [])
        self.assertNotIn("Prefer splunk_run_query", seen["system"])


class HistoryTests(unittest.TestCase):
    def test_history_keeps_the_newest_turns(self):
        conversation_id = "test-history-cap"
        for index in range(MAX_HISTORY_TURNS + 3):
            remember_turn(conversation_id, f"q{index}", f"a{index}")
        turns = prior_turns(conversation_id)
        self.assertEqual(len(turns), MAX_HISTORY_TURNS * 2)
        self.assertEqual(turns[0], {"role": "user", "content": "q3"})
        self.assertEqual(turns[-1], {"role": "assistant", "content": f"a{MAX_HISTORY_TURNS + 2}"})


class SessionTests(unittest.TestCase):
    def test_mcp_session_stays_on_the_task_that_set_it(self):
        async def use(value):
            set_mcp_session(value)
            await asyncio.sleep(0)
            return _mcp_session.get()

        async def both():
            return await asyncio.gather(use("session-a"), use("session-b"))

        first, second = asyncio.run(both())
        self.assertEqual(first, "session-a")
        self.assertEqual(second, "session-b")


if __name__ == "__main__":
    unittest.main()
