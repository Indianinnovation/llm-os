"""Reference MCP server: fully offline system information tools.

Demonstrates the plug-in story — the LLM OS kernel discovers this
server from mcp_servers.json and its tools become routable exactly like
built-ins. Everything here reads local state only; nothing leaves the
machine.

Run standalone:  python examples/system_info_server.py
"""

import platform
import shutil
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("system-info")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_local_time() -> dict:
    """Get the current local date and time on this machine."""
    return {
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": time.strftime("%Z"),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_disk_usage() -> dict:
    """Get total, used and free disk space of the main volume in GB."""
    usage = shutil.disk_usage("/")
    return {
        "total_gb": round(usage.total / 2**30, 1),
        "free_gb": round(usage.free / 2**30, 1),
        "used_percent": round(usage.used / usage.total * 100, 1),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_system_info() -> dict:
    """Get the operating system, architecture and Python version."""
    return {
        "os": platform.system(),
        "os_version": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


if __name__ == "__main__":
    mcp.run()
