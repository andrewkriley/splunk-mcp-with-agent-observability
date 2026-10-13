"""Cisco Live shared LiteLLM key for the chat app and Galileo judge."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "experiments" / "galileo-litellm-custom-provider.md").read_text(
    encoding="utf-8"
)


class SharedEdgeLitellmKeyTests(unittest.TestCase):
    def test_notes_dual_use_of_ltrxar_in_mcp_lab_key(self):
        self.assertIn("LTRXAR_2194", DOC)
        self.assertIn("MCP_LAB_LITELLM_KEY", DOC)
        self.assertIn("OPENAI_SPEC_API_KEY", DOC)
        self.assertIn("same", DOC.lower())
        self.assertIn("separate", DOC.lower())
        self.assertIn("judge", DOC.lower())
        self.assertIn("bedrock-claude", DOC)
        self.assertIn("litellm.apps.andrewriley.info", DOC)
        self.assertIn("/apps/splunk-mcp-lab", DOC)
        self.assertIn("/apps/aws-edge-litellm", DOC)
        self.assertIn("Never print", DOC)
        self.assertNotRegex(DOC, r"sk-[A-Za-z0-9]{16,}")


if __name__ == "__main__":
    unittest.main()
