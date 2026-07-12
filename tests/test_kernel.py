"""Kernel routing tests with a fake LLM client — no Ollama required."""

import pytest

from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.tools import default_registry


class FakeClient:
    """Scripted ollama.Client: returns queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, model, messages, tools=None, options=None):
        self.calls.append(
            {"messages": list(messages), "tools": tools, "options": options}
        )
        return self.responses.pop(0)


def tool_call_response(name, arguments):
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


def text_response(text):
    return {"message": {"content": text}}


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path)


def make_kernel(responses, audit):
    return Kernel(
        registry=default_registry(),
        client=FakeClient(responses),
        model="fake-model",
        audit=audit,
    )


def test_routes_math_to_calculator(audit):
    kernel = make_kernel(
        [
            tool_call_response("calculator", {"expression": "4539 * 23"}),
            text_response("The answer is 104397."),
        ],
        audit,
    )
    result = kernel.handle("What is 4539 multiplied by 23?")
    assert result["reply"] == "The answer is 104397."
    assert result["trace"][0]["tool"] == "calculator"
    assert result["trace"][0]["status"] == "success"
    assert result["trace"][0]["result"]["result"] == 104397.0


def test_plain_chat_needs_no_tool(audit):
    kernel = make_kernel([text_response("An LLM OS routes intents to tools.")], audit)
    result = kernel.handle("What is an LLM OS?")
    assert result["trace"] == []
    assert "routes intents" in result["reply"]


def test_unknown_tool_is_reported_not_fatal(audit):
    kernel = make_kernel(
        [
            tool_call_response("format_disk", {}),
            text_response("I cannot do that."),
        ],
        audit,
    )
    result = kernel.handle("Format my disk")
    assert result["trace"][0]["status"] == "unknown_tool"


def test_bad_params_surface_as_tool_error(audit):
    kernel = make_kernel(
        [
            tool_call_response("calculator", {"wrong_field": "1+1"}),
            text_response("Something went wrong."),
        ],
        audit,
    )
    result = kernel.handle("math please")
    assert result["trace"][0]["status"] == "tool_error"
    assert "error" in result["trace"][0]["result"]


def test_recovers_textual_tool_call(audit):
    """Models whose templates emit tool calls as raw JSON text (e.g.
    qwen2.5-coder) must still route correctly."""
    kernel = make_kernel(
        [
            text_response('{"name": "calculator", "arguments": {"expression": "2+2"}}'),
            text_response("The answer is 4."),
        ],
        audit,
    )
    result = kernel.handle("What is 2+2?")
    assert result["trace"][0]["tool"] == "calculator"
    assert result["trace"][0]["result"]["result"] == 4.0
    assert result["reply"] == "The answer is 4."


def test_json_reply_that_is_not_a_tool_call_stays_a_reply(audit):
    kernel = make_kernel(
        [text_response('{"name": "Alice", "age": 30}')],
        audit,
    )
    result = kernel.handle("Give me a sample JSON user object")
    assert result["trace"] == []
    assert "Alice" in result["reply"]


def test_audit_chain_records_and_verifies(audit):
    kernel = make_kernel(
        [
            tool_call_response("calculator", {"expression": "2+2"}),
            text_response("4."),
        ],
        audit,
    )
    kernel.handle("2+2?")
    records = audit.tail(10)
    assert any(r["event"] == "tool_execution" for r in records)
    assert audit.verify_chain() is True


def test_tampered_audit_log_fails_verification(audit, tmp_path):
    kernel = make_kernel(
        [
            tool_call_response("calculator", {"expression": "2+2"}),
            text_response("4."),
        ],
        audit,
    )
    kernel.handle("2+2?")
    # An attacker edits history. Content is redacted, so there is no prose to
    # rewrite — they go for what IS still readable: the outcome of the call.
    content = audit.path.read_text().replace('"status": "success"', '"status": "denied"')
    assert '"denied"' in content, "the tamper must actually change the log"
    audit.path.write_text(content)
    assert audit.verify_chain() is False
