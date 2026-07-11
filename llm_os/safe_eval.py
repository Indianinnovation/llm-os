"""AST-whitelist arithmetic evaluator.

Replaces `eval()` entirely: the expression is parsed into an AST and
only explicitly allowed node types are walked. There is no code object
execution path, so sandbox-escape tricks against eval/exec
(`().__class__...`, attribute access, subscripts, comprehensions,
lambdas, imports) are rejected at the syntax level.
"""

import ast
import math

MAX_EXPRESSION_LENGTH = 500
# Bounds that keep Pow from allocating astronomically large ints.
MAX_POW_EXPONENT = 512
MAX_POW_BASE = 1e15

_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": math.factorial,
}

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}

_UNARY_OPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


class UnsafeExpressionError(ValueError):
    """Raised when an expression contains disallowed syntax."""


def safe_eval(expression: str) -> float:
    """Evaluate a pure arithmetic expression. Raises UnsafeExpressionError
    for anything outside the whitelist, ValueError/ZeroDivisionError for
    math errors."""
    if not isinstance(expression, str):
        raise UnsafeExpressionError("Expression must be a string.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise UnsafeExpressionError(
            f"Expression exceeds {MAX_EXPRESSION_LENGTH} characters."
        )
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"Invalid syntax: {exc.msg}") from exc
    result = _eval_node(tree.body)
    return float(result)


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsafeExpressionError(
                f"Only numeric literals are allowed, got {node.value!r}."
            )
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise UnsafeExpressionError(f"Unknown identifier '{node.id}'.")

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise UnsafeExpressionError(
                f"Operator '{type(node.op).__name__}' is not allowed."
            )
        return op(_eval_node(node.operand))

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > MAX_POW_EXPONENT or abs(left) > MAX_POW_BASE:
                raise UnsafeExpressionError("Exponentiation operands too large.")
            return left ** right
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise UnsafeExpressionError(
                f"Operator '{type(node.op).__name__}' is not allowed."
            )
        return op(left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise UnsafeExpressionError("Only plain calls to whitelisted functions are allowed.")
        func = _FUNCTIONS.get(node.func.id)
        if func is None:
            raise UnsafeExpressionError(f"Function '{node.func.id}' is not allowed.")
        args = [_eval_node(arg) for arg in node.args]
        if node.func.id == "factorial" and (args and args[0] > 5000):
            raise UnsafeExpressionError("factorial argument too large.")
        return func(*args)

    raise UnsafeExpressionError(
        f"Syntax element '{type(node).__name__}' is not allowed."
    )
