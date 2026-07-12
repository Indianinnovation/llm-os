"""MCP host: connects the kernel to external MCP servers.

Reads a Claude-Desktop-compatible config file (`mcpServers` mapping),
spawns each server over stdio, and exposes their tools to the kernel
through the same registry as built-ins — validated by the server,
audited by the kernel.

Concurrency model: the MCP SDK is asyncio-based and its stdio transport
must be entered and exited in the same task. The manager therefore runs
one background event loop in a daemon thread, with one long-lived task
per server holding the connection open until shutdown; the kernel calls
in synchronously via run_coroutine_threadsafe.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from functools import partial
from pathlib import Path
from typing import List, Optional

from . import mcptrust
from .registry import Tool, ToolError, ToolRegistry

logger = logging.getLogger("llm_os.mcp")

CONNECT_TIMEOUT_S = 30
# Long enough for MCP tools that run local-model pipelines internally
# (e.g. a full telecom RCA is ~3 sequential 7B calls).
CALL_TIMEOUT_S = int(os.environ.get("MCP_CALL_TIMEOUT_S", "180"))

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    MCP_AVAILABLE = False


def needs_approval(tool) -> bool:
    """Should this MCP tool wait for a human?

    MCP's own defaults are the safe ones — `readOnlyHint` defaults to
    false and `destructiveHint` to true — so a tool that declares nothing
    is assumed to change the world irreversibly. Silence is never consent.
    A server opens the gate only by saying so, in one of two ways:

      readOnlyHint=true     it changes nothing            → open
      destructiveHint=false it changes something, safely  → open
                            (a proposal, a report, a sim)
      anything else                                       → gated

    That middle case is the one that matters in practice: proposing a
    network change is not the same act as executing it, and only the
    second one should stop and ask.
    """
    annotations = getattr(tool, "annotations", None)
    if getattr(annotations, "readOnlyHint", None) is True:
        return False
    if getattr(annotations, "destructiveHint", None) is False:
        return False
    return True


class MCPManager:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.discovered: List[dict] = []
        # {server: {"status", "detail"}} — supply-chain verification
        # outcome for every configured server, spawned or refused.
        self.trust_report: dict = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sessions = {}
        self._server_tasks = []
        self._shutdown_event: Optional[asyncio.Event] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> List[dict]:
        """Connect to every configured server; returns discovered tools.
        Missing SDK or config file degrades to 'no MCP tools', never a crash."""
        if not MCP_AVAILABLE:
            logger.warning("mcp package not installed; MCP tools disabled.")
            return []
        if not self.config_path.exists():
            logger.info("No MCP config at %s; running with built-ins only.", self.config_path)
            return []
        try:
            servers = json.loads(self.config_path.read_text()).get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Cannot read MCP config %s: %s", self.config_path, exc)
            return []
        if not servers:
            return []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-loop", daemon=True
        )
        self._thread.start()
        self._shutdown_event = asyncio.Event()

        for name, spec in servers.items():
            # Supply-chain gate: a server whose files drifted from their
            # pinned hashes is refused — never spawned, not just ignored.
            status, detail = mcptrust.verify_server(name, spec)
            self.trust_report[name] = {"status": status, "detail": detail}
            if status == mcptrust.FAIL:
                logger.error("MCP server '%s' REFUSED: %s", name, detail)
                continue
            future = asyncio.run_coroutine_threadsafe(
                self._connect_server(name, spec), self._loop
            )
            try:
                self.discovered.extend(future.result(CONNECT_TIMEOUT_S))
            except Exception as exc:
                logger.error("MCP server '%s' failed to start: %s", name, exc)
        return self.discovered

    async def _connect_server(self, name: str, spec: dict) -> List[dict]:
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        task = asyncio.get_running_loop().create_task(
            self._server_task(name, spec, ready)
        )
        self._server_tasks.append(task)
        return await asyncio.wait_for(ready, CONNECT_TIMEOUT_S)

    async def _server_task(self, name: str, spec: dict, ready: asyncio.Future):
        """Owns the server connection for its entire lifetime (see module doc)."""
        command = spec.get("command", "")
        # Convenience: bare "python" means the interpreter running the kernel.
        if command == "python":
            command = sys.executable
        params = StdioServerParameters(
            command=command, args=spec.get("args", []), env=spec.get("env")
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self._sessions[name] = session
                    tools = [
                        {
                            "server": name,
                            "name": tool.name,
                            "description": tool.description or "",
                            "schema": tool.inputSchema,
                            "gated": needs_approval(tool),
                        }
                        for tool in listing.tools
                    ]
                    logger.info(
                        "MCP server '%s' connected with tools: %s",
                        name,
                        [t["name"] for t in tools],
                    )
                    ready.set_result(tools)
                    await self._shutdown_event.wait()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
        finally:
            self._sessions.pop(name, None)

    def shutdown(self) -> None:
        if self._loop is None:
            return

        async def _stop():
            self._shutdown_event.set()
            if self._server_tasks:
                await asyncio.gather(*self._server_tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(10)
        except Exception as exc:
            logger.warning("MCP shutdown incomplete: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # -- tool invocation ---------------------------------------------------

    def call_tool(self, server: str, tool_name: str, args: dict):
        session = self._sessions.get(server)
        if session is None:
            raise ToolError(f"MCP server '{server}' is not connected.")

        async def _call():
            return await asyncio.wait_for(
                session.call_tool(tool_name, args or {}), CALL_TIMEOUT_S
            )

        try:
            result = asyncio.run_coroutine_threadsafe(_call(), self._loop).result(
                CALL_TIMEOUT_S + 5
            )
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"MCP call to {server}/{tool_name} failed: {exc}") from exc

        texts = [
            item.text
            for item in (result.content or [])
            if getattr(item, "type", "") == "text"
        ]
        text = "\n".join(texts).strip()
        if getattr(result, "isError", False):
            raise ToolError(text or f"MCP tool '{tool_name}' reported an error.")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"output": text}

    # -- registry integration ----------------------------------------------

    def register_tools(self, registry: ToolRegistry) -> int:
        """Merge discovered MCP tools into the kernel registry."""
        count = 0
        for entry in self.discovered:
            if registry.get(entry["name"]) is not None:
                logger.warning(
                    "Skipping MCP tool '%s' from '%s': name already registered.",
                    entry["name"],
                    entry["server"],
                )
                continue
            handler = partial(self._call_with_kwargs, entry["server"], entry["name"])
            # A server's self-description can only ever OPEN this gate. It
            # can never close one the operator set by name in APPROVAL_TOOLS
            # (api.py applies those after registration) — a hint is a
            # convenience, not an authority.
            gated = entry.get("gated", True)
            registry.register(
                Tool(
                    name=entry["name"],
                    description=entry["description"],
                    handler=handler,
                    json_schema=entry["schema"],
                    source=f"mcp:{entry['server']}",
                    requires_approval=gated,
                )
            )
            if gated:
                logger.info(
                    "MCP tool '%s' is human-gated (server did not declare it read-only).",
                    entry["name"],
                )
            count += 1
        return count

    def _call_with_kwargs(self, server: str, tool_name: str, **kwargs):
        return self.call_tool(server, tool_name, kwargs)
