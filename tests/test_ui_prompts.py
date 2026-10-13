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


if __name__ == "__main__":
    unittest.main()
