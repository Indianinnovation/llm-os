"""Built-in tools. Registering a new tool = one module with a TOOL object."""

from ..registry import ToolRegistry
from . import calculator, markdown_writer


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(calculator.TOOL)
    registry.register(markdown_writer.TOOL)
    return registry
