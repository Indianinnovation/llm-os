
import pytest

from llm_os.safe_eval import UnsafeExpressionError, safe_eval


def test_basic_arithmetic():
    assert safe_eval("4539 * 23") == 104397.0
    assert safe_eval("2 + 3 * 4") == 14.0
    assert safe_eval("(2 + 3) * 4") == 20.0
    assert safe_eval("-7 + 2") == -5.0
    assert safe_eval("10 / 4") == 2.5
    assert safe_eval("10 // 4") == 2.0
    assert safe_eval("10 % 3") == 1.0


def test_functions_and_constants():
    assert safe_eval("sqrt(3**2 + 4**2)") == 5.0
    assert safe_eval("round(pi, 2)") == 3.14
    assert abs(safe_eval("sin(pi / 2)") - 1.0) < 1e-9
    assert abs(safe_eval("log(e)") - 1.0) < 1e-9
    assert safe_eval("factorial(5)") == 120.0


@pytest.mark.parametrize(
    "attack",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "[x for x in (1,)]",
        "lambda: 1",
        "'a' * 10",
        "getattr(1, 'real')",
        "x = 5",
        "abs.__call__(1)",
        "round(1, ndigits=0)",  # keywords disallowed
    ],
)
def test_rejects_unsafe_expressions(attack):
    with pytest.raises((UnsafeExpressionError, SyntaxError)):
        safe_eval(attack)


def test_resource_bombs_rejected():
    with pytest.raises(UnsafeExpressionError):
        safe_eval("9 ** 9 ** 9")
    with pytest.raises(UnsafeExpressionError):
        safe_eval("factorial(100000)")
    with pytest.raises(UnsafeExpressionError):
        safe_eval("1" + "+1" * 400)  # over length limit


def test_llm_math_notation_normalized():
    # LLMs routinely emit '^' for power and unicode math symbols.
    assert safe_eval("5^2 + 12^2") == 169.0
    assert safe_eval("sqrt(5^2 + 12^2)") == 13.0
    assert safe_eval("√(9)") == 3.0
    assert safe_eval("6 × 7") == 42.0
    assert safe_eval("84 ÷ 2") == 42.0
    assert safe_eval("7!") == 5040.0
    assert safe_eval("math.sqrt(16)") == 4.0
    with pytest.raises(UnsafeExpressionError):
        safe_eval("9 ^ 9 ^ 9 ^ 9")  # pow guards still apply to '^'


def test_math_errors_propagate_cleanly():
    with pytest.raises(ZeroDivisionError):
        safe_eval("1 / 0")
    with pytest.raises(ValueError):
        safe_eval("sqrt(-1)")
