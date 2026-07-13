"""v0.2: conversation persistence, document Q&A, and tool approval gates."""

import pytest

from llm_os.approvals import ApprovalStore
from llm_os.audit import AuditLog
from llm_os.conversations import ConversationStore
from llm_os.documents import _chunk
from llm_os.kernel import Kernel, _parse_textual_tool_call
from llm_os.tools import default_registry
from tests.test_kernel import FakeClient, text_response, tool_call_response

# ── #3 conversations ────────────────────────────────────────────────────────

def test_conversation_survives_and_replays(tmp_path):
    store = ConversationStore(tmp_path)
    convo = store.create()

    store.append_turn(convo["id"], "What is 2+2?", "It is 4.",
                      trace=[{"tool": "calculator", "status": "success", "audit_id": "a1"}])
    store.append_turn(convo["id"], "And times 10?", "40.")

    # A *new* store instance = a restarted server: the chat is still there.
    reloaded = ConversationStore(tmp_path).get(convo["id"])
    assert len(reloaded["turns"]) == 2
    assert reloaded["title"] == "What is 2+2?"          # titled from first prompt
    assert reloaded["turns"][0]["tools"][0]["audit_id"] == "a1"

    history = ConversationStore(tmp_path).history_for(convo["id"], turns=6)
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "What is 2+2?"


def test_conversation_list_and_delete(tmp_path):
    store = ConversationStore(tmp_path)
    a = store.create("first")
    store.append_turn(a["id"], "first", "ok")
    b = store.create("second")
    store.append_turn(b["id"], "second", "ok")

    listed = store.list()
    assert {c["id"] for c in listed} == {a["id"], b["id"]}
    assert store.delete(a["id"]) is True
    assert store.get(a["id"]) is None
    assert store.delete("nope") is False


# ── #5 approval gates ───────────────────────────────────────────────────────

@pytest.fixture
def gated(tmp_path):
    registry = default_registry()
    registry.require_approval("write_markdown")
    approvals = ApprovalStore(tmp_path / "approvals.json")
    kernel = Kernel(
        registry=registry,
        client=FakeClient([
            tool_call_response("write_markdown",
                               {"filename": "x", "title": "T", "content": "c"}),
            text_response("It needs your approval."),
        ]),
        model="fake",
        audit=AuditLog(tmp_path),
        approvals=approvals,
    )
    return kernel, approvals


def test_gated_tool_does_not_run_without_approval(gated, tmp_path, monkeypatch):
    from llm_os import config

    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path / "scratch")
    kernel, approvals = gated

    result = kernel.handle("Write a note")
    step = result["trace"][0]
    assert step["status"] == "awaiting_approval"
    assert step["approval_id"].startswith("AP-")
    assert not (tmp_path / "scratch" / "x.md").exists(), "the tool must NOT have run"
    assert len(approvals.pending()) == 1


def test_execution_is_refused_until_approved(gated, tmp_path, monkeypatch):
    from llm_os import config

    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path / "scratch")
    kernel, approvals = gated
    approval_id = kernel.handle("Write a note")["trace"][0]["approval_id"]

    refused = kernel.execute_approved(approval_id)   # still PENDING
    assert refused["executed"] is False
    assert "REFUSED" in refused["error"]

    approvals.decide(approval_id, "approve", who="dilip")
    done = kernel.execute_approved(approval_id)
    assert done["executed"] is True
    assert (tmp_path / "scratch" / "x.md").exists()

    events = [r["event"] for r in kernel.audit.tail(20)]
    assert "approval_requested" in events
    assert "tool_executed_after_approval" in events


def test_rejected_tool_never_runs(gated, tmp_path, monkeypatch):
    from llm_os import config

    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path / "scratch")
    kernel, approvals = gated
    approval_id = kernel.handle("Write a note")["trace"][0]["approval_id"]

    approvals.decide(approval_id, "reject", who="dilip")
    result = kernel.execute_approved(approval_id)
    assert result["executed"] is False
    assert not (tmp_path / "scratch" / "x.md").exists()


def test_ungated_tools_still_run_immediately(tmp_path):
    kernel = Kernel(
        registry=default_registry(),            # nothing gated
        client=FakeClient([
            tool_call_response("calculator", {"expression": "2+2"}),
            text_response("4."),
        ]),
        model="fake",
        audit=AuditLog(tmp_path),
        approvals=ApprovalStore(tmp_path / "approvals.json"),
    )
    step = kernel.handle("2+2?")["trace"][0]
    assert step["status"] == "success"
    assert step["result"]["result"] == 4.0


# ── #4 documents ────────────────────────────────────────────────────────────

def test_chunking_respects_paragraphs_and_size():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(12))
    chunks = _chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 for c in chunks)
    assert "Paragraph 0." in chunks[0]


# ── the textual tool-call recovery these features exposed ───────────────────

@pytest.mark.parametrize("content,expected", [
    ('{"name": "calculator", "arguments": {"expression": "2+2"}}', "calculator"),
    ('```json\n{"name": "calculator", "arguments": {}}\n```', "calculator"),
    ('<tool_call>{"name": "calculator", "arguments": {}}</tool_call>', "calculator"),
    # qwen narrates, then emits the call:
    ('I will look that up. {"name": "search_documents", "arguments": {"query": "x"}}',
     "search_documents"),
    # …or puts the name outside the JSON:
    ('search_documents {"query": "liability cap"}', "search_documents"),
])
def test_textual_tool_calls_are_recovered(content, expected):
    call = _parse_textual_tool_call(content)
    assert call and call["function"]["name"] == expected


def test_plain_json_answer_is_not_mistaken_for_a_tool_call():
    call = _parse_textual_tool_call('Here is JSON: {"age": 30, "city": "Pune"}')
    # It may parse as a candidate, but the kernel only runs REGISTERED tools —
    # and this shape has no registered name.
    assert call is None or call["function"]["name"] not in default_registry()._tools
