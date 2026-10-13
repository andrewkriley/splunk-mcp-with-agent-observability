#!/usr/bin/env python3
"""Galileo session names prepend the email local-part. No live Galileo."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


if __name__ == "__main__":
    unittest.main()
