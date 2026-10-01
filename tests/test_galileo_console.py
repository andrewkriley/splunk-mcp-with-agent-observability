"""GALILEO_CONSOLE_URL override for a non-default console."""

import os
import unittest

from app.observability import apply_galileo_console_url, galileo_console_url


class GalileoConsoleUrlTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("GALILEO_CONSOLE_URL")

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("GALILEO_CONSOLE_URL", None)
        else:
            os.environ["GALILEO_CONSOLE_URL"] = self.previous

    def test_blank_uses_the_sdk_default(self):
        os.environ.pop("GALILEO_CONSOLE_URL", None)
        self.assertEqual(galileo_console_url(), "https://app.galileo.ai")

    def test_override_is_published_for_the_sdk(self):
        os.environ["GALILEO_CONSOLE_URL"] = "https://console.multitenant.galileocloud.io/"
        self.assertEqual(apply_galileo_console_url(), "https://console.multitenant.galileocloud.io")
        self.assertEqual(os.environ["GALILEO_CONSOLE_URL"], "https://console.multitenant.galileocloud.io")

    def test_scheme_is_added_when_missing(self):
        os.environ["GALILEO_CONSOLE_URL"] = "console.multitenant.galileocloud.io"
        self.assertEqual(galileo_console_url(), "https://console.multitenant.galileocloud.io")

    def test_url_with_credentials_is_rejected(self):
        os.environ["GALILEO_CONSOLE_URL"] = "https://user:secret@console.example.com"
        with self.assertRaises(ValueError):
            galileo_console_url()
