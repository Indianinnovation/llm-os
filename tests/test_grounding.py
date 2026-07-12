"""Grounding gate: an answer built on retrieved material must cite it, and
an answer with nothing behind it must not be given at all.

The bug this exists for: asked to summarize 3GPP TS 38.331, retrieval
returned one 0.434-scoring chunk of boilerplate plus a chunk of a
*different* spec. The model wrote a confident summary that blended the
two, cited neither, and read as authoritative. In a spec or a contract
that is the most dangerous output this system can produce."""

import pytest

from llm_os import config
from llm_os.audit import AuditLog
from llm_os.kernel import (
    Kernel,
    filter_weak_matches,
    retrieval_matches,
    sources_block,
    ungrounded_reply,
)
from llm_os.registry import Tool, ToolRegistry
from tests.test_kernel import FakeClient, text_response, tool_call_response

STRONG = {"citation": "3GPP TS 38.331 § 5.3.3", "excerpt": "RRC connection establishment…", "relevance": 0.761}
WEAK = {"citation": "3GPP TS 38.331 § 38.331", "excerpt": "⚠️ paraphrased demo excerpts…", "relevance": 0.434}
OTHER_SPEC = {"citation": "3GPP TS 28.552 § 5.1", "excerpt": "DL PRB usage…", "relevance": 0.403}


def _search_tool(result):
    return Tool(
        name="search_specs",
        description="Search the 3GPP spec corpus.",
        handler=lambda **kw: result,
        json_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


def _kernel(tmp_path, tool, responses):
    registry = ToolRegistry()
    registry.register(tool)
    return Kernel(
        registry=registry,
        client=FakeClient(responses),
        model="fake",
        audit=AuditLog(tmp_path),
    )


def test_retrieval_shape_is_recognised():
    assert retrieval_matches({"matches": [STRONG]}) == [STRONG]
    assert retrieval_matches({"total_gb": 21.4}) is None       # get_folder_size
    assert retrieval_matches({"result": 104397}) is None        # calculator
    assert retrieval_matches("not a dict") is None


def test_weak_chunks_are_dropped_before_the_model_sees_them():
    filtered, strong = filter_weak_matches(
        {"query": "TS 38.331", "matches": [WEAK, OTHER_SPEC]}, [WEAK, OTHER_SPEC]
    )
    assert strong == []
    assert filtered["matches"] == []
    assert filtered["weak_matches_dropped"] == 2


def test_the_exact_ts38331_case_refuses_instead_of_inventing(tmp_path):
    """The transcript that motivated this: weak boilerplate + a chunk of a
    different spec. The kernel must refuse, not summarize."""
    kernel = _kernel(
        tmp_path,
        _search_tool({"query": "TS 38.331 summary", "matches": [WEAK, OTHER_SPEC]}),
        [
            tool_call_response("search_specs", {"query": "TS 38.331 summary"}),
            # The model does exactly what it did in production: freelances.
            text_response(
                "I was unable to find a direct summary. However, I can provide a "
                "general overview. TS 38.331 outlines the NR RRC protocol, including "
                "connection establishment, handover, and power control. Key points "
                "include downlink total PRB usage measurement…"
            ),
        ],
    )
    outcome = kernel.handle("could you summarize TS 38.331")

    reply = outcome["reply"]
    assert "could not find that in your indexed material" in reply
    assert "not going to answer from memory" in reply
    # None of the invented content survives.
    assert "power control" not in reply
    assert "PRB usage" not in reply
    assert "general overview" not in reply


def test_a_grounded_answer_is_cited_by_the_kernel(tmp_path):
    kernel = _kernel(
        tmp_path,
        _search_tool({"query": "RRC", "matches": [STRONG]}),
        [
            tool_call_response("search_specs", {"query": "RRC connection"}),
            # The model answers well but forgets to cite. The kernel doesn't.
            text_response("RRC connection establishment moves the UE to RRC_CONNECTED."),
        ],
    )
    outcome = kernel.handle("How does RRC connection establishment work?")
    assert "RRC_CONNECTED" in outcome["reply"]
    assert "**Sources**" in outcome["reply"]
    assert "3GPP TS 38.331 § 5.3.3" in outcome["reply"]


def test_weak_and_strong_together_cites_only_the_strong(tmp_path):
    kernel = _kernel(
        tmp_path,
        _search_tool({"query": "RRC", "matches": [STRONG, WEAK, OTHER_SPEC]}),
        [
            tool_call_response("search_specs", {"query": "RRC"}),
            text_response("The UE transitions to RRC_CONNECTED."),
        ],
    )
    outcome = kernel.handle("How does RRC connection establishment work?")
    assert "3GPP TS 38.331 § 5.3.3" in outcome["reply"]
    # The other spec's chunk never becomes a citation on this answer.
    assert "28.552" not in outcome["reply"]


def test_non_retrieval_tools_are_untouched(tmp_path):
    """A calculator answer must not grow a Sources block or get refused."""
    from llm_os.tools import default_registry

    kernel = Kernel(
        registry=default_registry(),
        client=FakeClient(
            [
                tool_call_response("calculator", {"expression": "4539 * 23"}),
                text_response("The result is 104,397."),
            ]
        ),
        model="fake",
        audit=AuditLog(tmp_path),
    )
    outcome = kernel.handle("What is 4539 multiplied by 23?")
    assert outcome["reply"] == "The result is 104,397."
    assert "Sources" not in outcome["reply"]


def test_streamed_tokens_never_carry_the_invented_summary(tmp_path):
    kernel = _kernel(
        tmp_path,
        _search_tool({"query": "TS 38.331", "matches": [WEAK]}),
        [
            tool_call_response("search_specs", {"query": "TS 38.331"}),
            text_response("TS 38.331 covers connection establishment and power control."),
        ],
    )
    streamed = "".join(
        ev["text"] for ev in kernel.stream("summarize TS 38.331") if ev["type"] == "token"
    )
    assert "power control" not in streamed
    assert "could not find that in your indexed material" in streamed


def test_relevance_floor_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIN_RELEVANCE", 0.4)  # accept the weak chunk
    _, strong = filter_weak_matches({"matches": [WEAK]}, [WEAK])
    assert len(strong) == 1
