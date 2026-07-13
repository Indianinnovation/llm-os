"""Tool registry.

A Tool is a name, a description, a parameter contract, and a handler.
Built-in tools declare a Pydantic model and get validated before the
handler runs. MCP tools arrive with a ready-made JSON Schema from their
server (which validates its own inputs), so they carry `json_schema`
instead. Either way the registry emits Ollama-native tool specs from a
single source of truth, and every tool is labelled with its origin.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


class ToolError(Exception):
    """Raised when a tool cannot execute; message is safe to show the model."""


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    parameters: Optional[Type[BaseModel]] = None
    json_schema: Optional[dict] = None
    source: str = "builtin"
    # A tool that changes the world outside the sandbox (writes, sends,
    # executes) can require a human to confirm before the kernel runs it.
    # The model can propose; only a person can authorize. The decision is
    # written to the audit chain.
    requires_approval: bool = False

    def spec(self) -> dict:
        if self.parameters is not None:
            schema = self.parameters.model_json_schema()
            schema.pop("title", None)
        else:
            schema = self.json_schema or dict(_EMPTY_SCHEMA)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def run(self, raw_params: Dict[str, Any]) -> Any:
        raw_params = raw_params or {}
        if self.parameters is None:
            return self.handler(**raw_params)
        try:
            validated = self.parameters(**raw_params)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
            raise ToolError(f"Invalid parameters for '{self.name}': {errors}") from exc
        return self.handler(**validated.model_dump())


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def specs(self) -> List[dict]:
        return [tool.spec() for tool in self._tools.values()]

    def describe(self) -> List[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "source": t.source,
                "requires_approval": t.requires_approval,
            }
            for t in self._tools.values()
        ]

    def require_approval(self, *names: str) -> None:
        """Mark tools as human-gated (config-driven; see LLM_OS_APPROVAL_TOOLS)."""
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                tool.requires_approval = True
