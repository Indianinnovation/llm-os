"""Smoke test for the inverse adapter (`python -m llm_os.mcp_server`), which
exposes LLM OS built-ins to any MCP host. It was the only source module with
no test, yet it's a promoted integration path — a break in the tool wiring
would ship silently to exactly the developer surface being advertised.
"""

import asyncio

import pytest

from llm_os.mcp_client import MCP_AVAILABLE

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp SDK not installed")


def test_server_registers_both_builtin_tools():
    from llm_os import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"calculate", "write_markdown"} <= names


def test_advertised_tools_expose_input_schemas():
    from llm_os import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    # A host needs the schema to call the tool; a broken wrapper drops it.
    assert "expression" in str(tools["calculate"].inputSchema)
    assert "filename" in str(tools["write_markdown"].inputSchema)
