"""Deterministic offline calculator backed by the AST-whitelist evaluator."""

from pydantic import BaseModel, Field

from ..registry import Tool, ToolError
from ..safe_eval import UnsafeExpressionError, safe_eval


class CalculatorParams(BaseModel):
    expression: str = Field(
        ...,
        description=(
            "A pure arithmetic expression using numbers, + - * / // % **, "
            "parentheses, constants pi/e, and functions like sqrt, sin, cos, "
            "log, exp, abs, round. Example: 'sqrt(3**2 + 4**2)'"
        ),
    )


def calculate(expression: str) -> dict:
    try:
        result = safe_eval(expression)
    except UnsafeExpressionError as exc:
        raise ToolError(str(exc)) from exc
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ToolError(f"Math error: {exc}") from exc
    return {"expression": expression, "result": result}


TOOL = Tool(
    name="calculator",
    description=(
        "Evaluate an arithmetic or math expression exactly, offline. "
        "Use this for any calculation instead of doing math yourself."
    ),
    parameters=CalculatorParams,
    handler=calculate,
)
