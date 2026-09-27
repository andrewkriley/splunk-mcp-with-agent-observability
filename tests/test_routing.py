"""Routing, history, and per-request MCP session behavior."""

import asyncio
import unittest

from app.agent import (
    EXCLUDED_TOOLS,
    INFRA_KEYWORDS,
    SECURITY_KEYWORDS,
    _matches_any,
    _scoped_tools,
)
from app.observability import (
    MAX_HISTORY_TURNS,
    _mcp_session,
    prior_turns,
    remember_turn,
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
