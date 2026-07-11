"""The inverse adapter: expose LLM OS built-in tools as an MCP server,
so any MCP host (Claude Desktop, another agent) can use them.

Claude Desktop config entry:

    "llm-os": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "llm_os.mcp_server"],
      "cwd": "/path/to/llm-os"
    }
"""

from mcp.server.fastmcp import FastMCP

from .tools import calculator, markdown_writer

mcp = FastMCP("llm-os")


@mcp.tool()
def calculate(expression: str) -> dict:
    """Evaluate an arithmetic/math expression exactly and offline,
    e.g. 'sqrt(3**2 + 4**2)'. Sandboxed: only pure math can execute."""
    return calculator.calculate(expression)


@mcp.tool()
def write_markdown(filename: str, title: str, content: str) -> dict:
    """Create a Markdown document in the LLM OS sandbox directory.
    Filename is the base name without extension."""
    return markdown_writer.write_markdown(filename, title, content)


if __name__ == "__main__":
    mcp.run()
