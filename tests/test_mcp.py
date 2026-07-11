"""MCP host tests: spawn the real example server over stdio (offline)."""

import json
import sys
from pathlib import Path

import pytest

from llm_os.mcp_client import MCP_AVAILABLE, MCPManager
from llm_os.registry import Tool, ToolRegistry
from llm_os.tools import default_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp SDK not installed")


@pytest.fixture(scope="module")
def manager(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("mcp") / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "system-info": {
                        "command": sys.executable,
                        "args": [str(PROJECT_ROOT / "examples" / "system_info_server.py")],
                    }
                }
            }
        )
    )
    mgr = MCPManager(config_path)
    mgr.start()
    yield mgr
    mgr.shutdown()


def test_discovers_server_tools(manager):
    names = {t["name"] for t in manager.discovered}
    assert {"get_local_time", "get_disk_usage", "get_system_info"} <= names
    assert all(t["server"] == "system-info" for t in manager.discovered)


def test_call_tool_returns_structured_result(manager):
    result = manager.call_tool("system-info", "get_disk_usage", {})
    assert result["total_gb"] > 0
    assert 0 <= result["used_percent"] <= 100


def test_mcp_tools_merge_into_registry_and_run(manager):
    registry = default_registry()
    added = manager.register_tools(registry)
    assert added == len(manager.discovered)

    tool = registry.get("get_local_time")
    assert tool is not None
    assert tool.source == "mcp:system-info"
    outcome = tool.run({})
    assert "local_time" in outcome

    # Built-ins are still intact alongside MCP tools.
    assert registry.get("calculator").source == "builtin"
    assert len(registry.specs()) == 2 + added


def test_missing_config_degrades_gracefully(tmp_path):
    mgr = MCPManager(tmp_path / "nonexistent.json")
    assert mgr.start() == []
    mgr.shutdown()  # must be a no-op, not an error


def test_schema_tool_without_pydantic_model():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="echo",
            handler=lambda **kw: kw,
            json_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            source="mcp:test",
        )
    )
    spec = registry.specs()[0]
    assert spec["function"]["parameters"]["properties"]["x"]["type"] == "string"
    assert registry.get("echo").run({"x": "hi"}) == {"x": "hi"}
