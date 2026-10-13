"""Suggested workshop prompts on the Buttercup Games page."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


class PromptGuideTests(unittest.TestCase):
    def test_guide_sits_below_the_chat_form(self):
        form = HTML.index('id="chat-form"')
        guide = HTML.index('id="prompt-guide"')
        footer = HTML.rindex("<footer")
        self.assertLess(form, guide)
        self.assertLess(guide, footer)
        self.assertIn("Try these searches", HTML)
        self.assertIn("grounded in this workshop's Splunk indexes", HTML)

    def test_prompts_cover_live_mcp_data_including_the_misspelling(self):
        required = (
            "any high severity notables in the last 30 days",
            "what noteables do we have",
            "brute force",
            "SQL injection",
            "malware",
            "PDU power",
            "CRAC",
            "Compare security notables with PDU power draw",
            "Cisco ASA",
            "indexes",
        )
        for phrase in required:
            self.assertIn(phrase, HTML, phrase)
        self.assertIn('data-prompt="what noteables do we have"', HTML)
        self.assertNotEqual(
            HTML.lower().count("notables"),
            HTML.lower().count("noteables"),
            "the guide must keep both spellings",
        )

    def test_clicking_a_prompt_fills_the_search_box(self):
        self.assertIn('id="prompt-guide"', HTML)
        self.assertTrue(re.search(r'button[^>]*class="[^"]*prompt[^"]*"', HTML))
        self.assertIn("data-prompt", HTML)
        self.assertIn('document.getElementById("prompt-guide")', HTML)
        self.assertIn("input.value = button.dataset.prompt", HTML)
        self.assertIn("input.focus()", HTML)
        self.assertNotIn("form.requestSubmit()", HTML)


class LabGuideTests(unittest.TestCase):
    def test_lab_guide_sits_to_the_right_of_chat(self):
        chat = HTML.index('id="chat-column"')
        form = HTML.index('id="chat-form"')
        prompts = HTML.index('id="prompt-guide"')
        guide = HTML.index('id="lab-guide"')
        footer = HTML.rindex("<footer")
        self.assertLess(chat, form)
        self.assertLess(form, prompts)
        self.assertLess(prompts, guide)
        self.assertLess(guide, footer)
        self.assertIn("<aside", HTML[guide - 80 : guide + 20])
        self.assertIn("grid-template-columns: minmax(0, 1fr) 22rem", HTML)
        self.assertIn('name="viewport"', HTML)

    def test_lab_guide_covers_access_search_and_galileo(self):
        start = HTML.index('id="lab-guide"')
        end = HTML.rindex("<footer")
        guide = HTML[start:end]
        self.assertIn("Lab guide", guide)
        self.assertIn("1. Access", guide)
        self.assertIn("2. Run a search", guide)
        self.assertIn("3. Galileo traces and evaluations", guide)
        self.assertIn("mcp-lab.apps.andrewriley.info", guide)
        self.assertIn("Open lab", guide)
        self.assertIn("email", guide.lower())
        self.assertIn("Search", guide)
        self.assertIn("Try these searches", guide)
        self.assertIn("noteables", guide)
        self.assertIn("console.multitenant.galileocloud.io", guide)
        self.assertIn("andreril-splunk-mcp-lab", guide)
        self.assertIn("shared", guide)
        self.assertIn("workshop-chat-", guide)
        self.assertIn("supervisor", guide)
        self.assertIn("groundedness", guide)
        self.assertIn("tool_selection_quality", guide)
        self.assertNotIn("GALILEO_API_KEY", guide)
        self.assertNotIn("SPLUNK_MCP_TOKEN", guide)


class TracesPanelTests(unittest.TestCase):
    def test_traces_tab_is_present_and_collapsed(self):
        self.assertIn('id="traces-panel"', HTML)
        self.assertIn('id="tab-traces"', HTML)
        self.assertIn("My traces", HTML)
        self.assertIn("/galileo/sessions", HTML)
        self.assertIn("galileo_traces_panel", HTML)
        self.assertIn("GALILEO_TRACES_PANEL", HTML)
        self.assertIn("loadTraces", HTML)
        start = HTML.index('id="traces-panel"')
        chunk = HTML[start : start + 400]
        self.assertIn("hidden", chunk)
        self.assertNotIn("GALILEO_API_KEY", HTML)
        self.assertNotIn("user=", HTML.lower().split("/galileo/sessions")[1][:80])


if __name__ == "__main__":
    unittest.main()
