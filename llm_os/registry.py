"""Tool registry — the seam where MCP slots in later.

A Tool is a name, a description, a Pydantic parameter model, and a
handler. Parameters coming from the model are validated before the
handler ever runs; the registry emits Ollama-native tool specs derived
from the same Pydantic schema, so there is a single source of truth.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError


class ToolError(Exception):
    """Raised when a tool cannot execute; message is safe to show the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: Type[BaseModel]
    handler: Callable[..., Any]

    def spec(self) -> dict:
        schema = self.parameters.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def run(self, raw_params: Dict[str, Any]) -> Any:
        try:
            validated = self.parameters(**(raw_params or {}))
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
            {"name": t.name, "description": t.description}
            for t in self._tools.values()
        ]
