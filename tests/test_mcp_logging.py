"""Splunk MCP request logs name the tool and hide the token."""

import asyncio
import os
import unittest

from app.mcp_client import call_tool, list_splunk_tools, log_mcp_request


class McpLogTests(unittest.TestCase):
    def setUp(self):
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

    def test_session_log_names_the_endpoint_and_omits_the_token(self):
        with self.assertLogs("app.mcp", level="INFO") as captured:
            log_mcp_request("session")
        line = captured.output[0]
        self.assertIn("MCP session endpoint=splunk.example.com:8089/services/mcp", line)
        self.assertNotIn("workshop-mcp-token", line)

    def test_tool_call_logs_the_request_and_redacts_the_token(self):
        class FakeContent:
            text = "ok"

        class FakeResult:
            content = [FakeContent()]

        class FakeSession:
            async def call_tool(self, name, arguments):
                FakeSession.seen = (name, arguments)
                return FakeResult()

        async def run():
            return await call_tool(FakeSession(), "splunk_run_query", {"search": "index=main workshop-mcp-token"})

        with self.assertLogs("app.mcp", level="INFO") as captured:
            result = asyncio.run(run())
        self.assertEqual(result, "ok")
        self.assertEqual(FakeSession.seen[0], "splunk_run_query")
        line = captured.output[0]
        self.assertIn("MCP call", line)
        self.assertIn("tool=splunk_run_query", line)
        self.assertIn("[redacted]", line)
        self.assertNotIn("workshop-mcp-token", line)

    def test_list_tools_logs_the_request(self):
        class FakeTool:
            def __init__(self, name):
                self.name = name
                self.description = ""
                self.input_schema = {}

        class FakeList:
            tools = [FakeTool("splunk_run_query")]

        class FakeSession:
            async def list_tools(self):
                return FakeList()

        with self.assertLogs("app.mcp", level="INFO") as captured:
            tools = asyncio.run(list_splunk_tools(FakeSession()))
        self.assertEqual(tools[0]["name"], "splunk_run_query")
        self.assertIn("MCP list_tools endpoint=splunk.example.com:8089/services/mcp", captured.output[0])
