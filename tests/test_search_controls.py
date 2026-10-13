"""Tool-loop budget, truncation, and workshop comfort notes."""

import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.agent import (
    BASE_SYSTEM_PROMPT,
    CATEGORY_TOOL_NAMES,
    _openai_loop,
    _scoped_tools,
    run_agent_turn,
)
from app.mcp_client import MAX_TOOL_RESULT_CHARS, call_tool
from app.search_controls import (
    CONTROL_NOTES,
    fingerprint,
    note_control,
    reset_controls,
    used_controls,
    with_comfort_notes,
)


def _openai_text(text: str):
    message = MagicMock()
    message.content = text
    message.tool_calls = None
    message.model_dump.return_value = {"role": "assistant", "content": text}
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _openai_tools(calls: list[tuple[str, str, dict]]):
    tool_calls = []
    dumped = []
    for call_id, name, arguments in calls:
        fn = MagicMock()
        fn.name = name
        fn.arguments = json.dumps(arguments)
        tc = MagicMock()
        tc.id = call_id
        tc.function = fn
        tool_calls.append(tc)
        dumped.append(
            {"id": call_id, "function": {"name": name, "arguments": fn.arguments}}
        )
    message = MagicMock()
    message.content = None
    message.tool_calls = tool_calls
    message.model_dump.return_value = {"role": "assistant", "tool_calls": dumped}
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


class ComfortNoteTests(unittest.TestCase):
    def setUp(self):
        reset_controls()

    def test_plain_text_has_no_note(self):
        self.assertEqual(with_comfort_notes("3 notables"), "3 notables")

    def test_each_control_has_a_workshop_note(self):
        for key, sentence in CONTROL_NOTES.items():
            reset_controls()
            note_control(key)
            wrapped = with_comfort_notes("here are the findings")
            self.assertIn("here are the findings", wrapped)
            self.assertIn("Note:", wrapped)
            self.assertIn(sentence, wrapped)


class FingerprintTests(unittest.TestCase):
    def test_whitespace_different_spl_is_the_same_search(self):
        a = fingerprint("splunk_run_query", {"search": "index=oidemo_notable\n| head 20"})
        b = fingerprint("splunk_run_query", {"search": "index=oidemo_notable | head 20"})
        self.assertEqual(a, b)

    def test_different_spl_stays_distinct(self):
        a = fingerprint("splunk_run_query", {"search": "index=oidemo | stats count"})
        b = fingerprint("splunk_run_query", {"search": "index=oidemo_notable | stats count"})
        self.assertNotEqual(a, b)


class ToolScopeTests(unittest.TestCase):
    def test_general_uses_the_same_three_search_tools_as_security(self):
        tools = [
            {"name": "splunk_run_query"},
            {"name": "splunk_get_user_list"},
            {"name": "saia_generate_spl"},
            {"name": "splunk_get_indexes"},
            {"name": "splunk_get_metadata"},
            {"name": "splunk_get_index_info"},
            {"name": "splunk_get_kv_store_collections"},
        ]
        names = [tool["name"] for tool in _scoped_tools(tools, "general")]
        self.assertEqual(
            names,
            ["splunk_run_query", "splunk_get_metadata", "splunk_get_index_info"],
        )
        self.assertEqual(
            CATEGORY_TOOL_NAMES["general"],
            CATEGORY_TOOL_NAMES["security"],
        )

    def test_system_prompt_teaches_a_short_search_budget(self):
        self.assertIn("splunk_run_query", BASE_SYSTEM_PROMPT)
        self.assertIn("head 20", BASE_SYSTEM_PROMPT)
        self.assertIn("two or three", BASE_SYSTEM_PROMPT)


class TruncationTests(unittest.TestCase):
    def setUp(self):
        reset_controls()
        self.previous = {
            "SPLUNK_INSTANCE_URL": os.environ.get("SPLUNK_INSTANCE_URL"),
            "SPLUNK_MCP_TOKEN": os.environ.get("SPLUNK_MCP_TOKEN"),
        }
        os.environ["SPLUNK_INSTANCE_URL"] = "https://splunk.example.com"
        os.environ["SPLUNK_MCP_TOKEN"] = "workshop-mcp-token"

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_large_tool_result_is_shortened_and_noted(self):
        class FakeContent:
            text = "x" * (MAX_TOOL_RESULT_CHARS + 80)

        class FakeResult:
            content = [FakeContent()]

        class FakeSession:
            async def call_tool(self, name, arguments):
                return FakeResult()

        result = asyncio.run(call_tool(FakeSession(), "splunk_run_query", {"search": "index=main"}))
        self.assertLessEqual(len(result), MAX_TOOL_RESULT_CHARS + 40)
        self.assertIn("truncated", result)
        self.assertIn("truncated", used_controls())


class OpenAILoopControlTests(unittest.TestCase):
    def setUp(self):
        reset_controls()

    def test_last_round_drops_tools_and_adds_a_comfort_note(self):
        seen_tools = []
        responses = [
            _openai_tools([("c1", "splunk_run_query", {"search": "index=oidemo | head 20"})]),
            _openai_text("PDU draw is 12A."),
        ]

        def fake_openai(messages, tools, system_prompt, **_kwargs):
            seen_tools.append(tools)
            return responses.pop(0)

        async def fake_splunk(name, arguments):
            return "amps=12"

        with (
            patch("app.agent.MAX_TURNS", 2),
            patch("app.observability.call_openai", fake_openai),
            patch("app.observability.call_splunk_tool", fake_splunk),
            patch("app.agent.galileo_context") as galileo,
        ):
            galileo.get_logger_instance.return_value = MagicMock()
            reply = asyncio.run(
                run_agent_turn(
                    "what's the PDU power draw in the datacenter",
                    [{"name": "splunk_run_query", "description": "", "input_schema": {}}],
                    provider="openai",
                )
            )

        self.assertEqual(len(seen_tools), 2)
        self.assertTrue(seen_tools[0])
        self.assertEqual(seen_tools[1], [])
        self.assertIn("PDU draw is 12A.", reply)
        self.assertIn("Note:", reply)
        self.assertIn(CONTROL_NOTES["forced_final"], reply)
        self.assertNotIn("Reached the tool-call limit", reply)

    def test_query_cap_stops_further_searches_and_notes(self):
        seen_tools = []
        responses = [
            _openai_tools([("c1", "splunk_run_query", {"search": "index=a | head 1"})]),
            _openai_tools([("c2", "splunk_run_query", {"search": "index=b | head 1"})]),
            _openai_text("Two indexes returned rows."),
        ]

        def fake_openai(messages, tools, system_prompt, **_kwargs):
            seen_tools.append(tools)
            return responses.pop(0)

        async def fake_splunk(name, arguments):
            return "ok"

        with (
            patch("app.agent.MAX_TURNS", 8),
            patch("app.search_controls.MAX_SPLUNK_QUERIES", 2),
            patch("app.observability.call_openai", fake_openai),
            patch("app.observability.call_splunk_tool", fake_splunk),
        ):
            reply = asyncio.run(
                _openai_loop(
                    "show events",
                    [{"name": "splunk_run_query", "description": "", "input_schema": {}}],
                    "sys",
                    [],
                    client_settings={},
                )
            )

        text, status = reply
        self.assertEqual(status, 0)
        self.assertEqual(len(seen_tools), 3)
        self.assertEqual(seen_tools[2], [])
        self.assertIn("Two indexes returned rows.", text)
        self.assertIn("query_cap", used_controls())
        self.assertIn(CONTROL_NOTES["query_cap"], with_comfort_notes(text))

    def test_repeated_search_answers_from_what_it_has(self):
        responses = [
            _openai_tools([("c1", "splunk_run_query", {"search": "index=oidemo_notable | head 20"})]),
            _openai_tools([("c2", "splunk_run_query", {"search": "index=oidemo_notable   |  head 20"})]),
            _openai_text("There are 4 notable events."),
        ]

        def fake_openai(messages, tools, system_prompt, **_kwargs):
            return responses.pop(0)

        calls = []

        async def fake_splunk(name, arguments):
            calls.append(arguments)
            return "4 events"

        with (
            patch("app.observability.call_openai", fake_openai),
            patch("app.observability.call_splunk_tool", fake_splunk),
            patch("app.agent.galileo_context") as galileo,
        ):
            galileo.get_logger_instance.return_value = MagicMock()
            reply = asyncio.run(
                run_agent_turn(
                    "any high severity notables in the last 30 days",
                    [{"name": "splunk_run_query", "description": "", "input_schema": {}}],
                    provider="openai",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("There are 4 notable events.", reply)
        self.assertIn(CONTROL_NOTES["repeat"], reply)
        self.assertNotIn("Stopped after detecting a repeated identical call", reply)


if __name__ == "__main__":
    unittest.main()
