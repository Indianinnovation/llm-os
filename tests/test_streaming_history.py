"""Streaming, multi-turn context, and the memory feedback-loop fix."""

import pytest

from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.tools import default_registry
from tests.test_kernel import FakeClient, text_response, tool_call_response
from tests.test_memory import fake_embedder

from llm_os.memory import EpisodicMemory


class StreamingFakeClient(FakeClient):
    """Adds stream=True support: yields the queued text in chunks."""

    def chat(self, model, messages, tools=None, options=None, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        response = self.responses.pop(0)
        if not stream:
            return response
        text = response["message"]["content"]
        return iter(
            [{"message": {"content": piece + " "}} for piece in text.split(" ")]
        )


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path)


def test_stream_emits_tokens_for_plain_chat(audit):
    kernel = Kernel(
        registry=default_registry(),
        client=StreamingFakeClient(
            [text_response("An LLM OS routes intent."), text_response("An LLM OS routes intent.")]
        ),
        model="fake",
        audit=audit,
    )
    events = list(kernel.stream("What is an LLM OS?"))
    kinds = [e["type"] for e in events]
    assert "token" in kinds and kinds[-1] == "done"
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert "routes intent" in streamed
    assert events[-1]["reply"].strip() == "An LLM OS routes intent."


def test_stream_emits_tool_events_before_answer(audit):
    kernel = Kernel(
        registry=default_registry(),
        client=StreamingFakeClient(
            [
                tool_call_response("calculator", {"expression": "2+2"}),
                text_response("The answer is 4."),
            ]
        ),
        model="fake",
        audit=audit,
    )
    events = list(kernel.stream("What is 2+2?"))
    kinds = [e["type"] for e in events]
    assert kinds.index("tool_start") < kinds.index("tool") < kinds.index("done")

    tool_event = next(e for e in events if e["type"] == "tool")
    assert tool_event["tool"] == "calculator"
    assert tool_event["result"]["result"] == 4.0
    assert tool_event["audit_id"]

    done = events[-1]
    assert done["reply"] == "The answer is 4."
    assert done["trace"][0]["tool"] == "calculator"


def test_history_is_replayed_to_the_model(audit):
    client = StreamingFakeClient([text_response("Cell 5 is fine.")])
    kernel = Kernel(
        registry=default_registry(), client=client, model="fake", audit=audit
    )
    kernel.handle(
        "And cell 5?",
        history=[
            {"role": "user", "content": "Alarms on cell 7?"},
            {"role": "assistant", "content": "Cell 7 has a link failure."},
        ],
    )
    sent = client.calls[0]["messages"]
    contents = [m["content"] for m in sent]
    assert "Alarms on cell 7?" in contents
    assert "Cell 7 has a link failure." in contents
    assert contents[-1] == "And cell 5?"


def test_history_is_bounded(audit, monkeypatch):
    from llm_os import config

    monkeypatch.setattr(config, "MAX_HISTORY_TURNS", 1)
    client = StreamingFakeClient([text_response("ok")])
    kernel = Kernel(
        registry=default_registry(), client=client, model="fake", audit=audit
    )
    long_history = [
        {"role": "user", "content": f"q{i}"} if i % 2 == 0
        else {"role": "assistant", "content": f"a{i}"}
        for i in range(10)
    ]
    kernel.handle("now", history=long_history)
    contents = [m["content"] for m in client.calls[0]["messages"]]
    assert "q0" not in contents          # old turns dropped
    assert "a9" in contents              # most recent kept
    assert contents[-1] == "now"


def test_system_prompt_forbids_doing_followup_math_in_head():
    """A follow-up like 'and divide that by 3' must still go through the
    calculator. The model got this wrong (34,806 instead of 34,799) until
    the prompt said so explicitly — arithmetic is never done in-head."""
    from llm_os.kernel import SYSTEM_PROMPT

    assert "follow-ups too" in SYSTEM_PROMPT
    assert "call the calculator with the earlier result" in SYSTEM_PROMPT
    assert "Never compute it in your head" in SYSTEM_PROMPT


def test_memory_never_archives_the_assistants_own_reply(tmp_path, audit):
    """The feedback loop: storing model output as memory means a wrong
    answer is recalled later as fact. Memory records the USER only."""
    memory = EpisodicMemory(tmp_path / "mem", embedder=fake_embedder)
    kernel = Kernel(
        registry=default_registry(),
        client=StreamingFakeClient(
            [
                tool_call_response("calculator", {"expression": "2+2"}),
                text_response("The answer is 4."),
            ]
        ),
        model="fake",
        audit=audit,
        memory=memory,
    )
    kernel.handle("What is 2+2?")

    stored = memory.list_records()
    assert len(stored) == 1
    text = stored[0]["text"]
    assert "User said: What is 2+2?" in text
    assert "answered using: calculator" in text
    assert "The answer is 4" not in text  # the reply itself is never stored
