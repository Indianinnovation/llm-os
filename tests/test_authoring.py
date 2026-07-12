"""Authoring pass: a small model fills a `content` field like a form slot
('This is the first startup idea.') and the kernel would write that to
disk. Placeholder content must trigger a tool-free authoring call whose
result replaces the parameter — before the file is written, and before a
human is asked to approve it."""

import pytest

from llm_os.approvals import ApprovalStore
from llm_os.audit import AuditLog
from llm_os.kernel import Kernel, is_placeholder_content
from llm_os.tools import default_registry
from llm_os import config
from tests.test_kernel import FakeClient, text_response, tool_call_response

REAL_NOTE = """## 1. Ledger — reconciliation for freight brokers

Freight brokers reconcile carrier invoices against rate confirmations by
hand, in spreadsheets. Ledger ingests both, flags the 3–5% that disagree,
and explains why. Wedge: brokers already pay clerks to do exactly this.

## 2. Rounds — handoff notes for night-shift nurses

Shift handoff is verbal and lossy. Rounds turns the last 12 hours of chart
events into a structured handoff a nurse can correct in 90 seconds.

## 3. Prairie — soil-test interpretation for row-crop agronomists

Labs return a PDF of numbers; agronomists translate it into a fertilizer
plan from memory. Prairie does the translation and shows its reasoning
against the local extension guidelines.
"""

PLACEHOLDER_NOTE = """# Startup Ideas

## Startup Idea 1
This is the first startup idea.

## Startup Idea 2
This is the second startup idea.

## Startup Idea 3
This is the third startup idea.
"""


@pytest.mark.parametrize(
    "body",
    [
        PLACEHOLDER_NOTE,
        "This is the first idea.",
        "Idea 1: description goes here",
        "Your content here",
        "Lorem ipsum dolor sit amet",
        "[insert description]",
        "TODO",
        "",
        "# A\n- one\n- two\n- three",  # structure, no substance
    ],
)
def test_detects_placeholders(body):
    assert is_placeholder_content(body)


@pytest.mark.parametrize("body", [REAL_NOTE, "The liability cap is USD 250,000, except for breaches of Section 4 (Confidentiality), which are uncapped under Delaware law."])
def test_accepts_real_content(body):
    assert not is_placeholder_content(body)


def _kernel(tmp_path, responses, **kw):
    return Kernel(
        registry=default_registry(),
        client=FakeClient(responses),
        model="fake",
        audit=AuditLog(tmp_path),
        **kw,
    )


def test_placeholder_content_is_reauthored_before_the_file_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    kernel = _kernel(
        tmp_path,
        [
            # 1. routing pass: model fills the form with placeholders
            tool_call_response("write_markdown", {"filename": "ideas", "title": "Startup Ideas", "content": PLACEHOLDER_NOTE}),
            # 2. authoring pass (no tools): model actually writes it
            text_response(REAL_NOTE),
            # 3. final summary turn
            text_response("Saved ideas.md."),
        ],
    )
    outcome = kernel.handle("Write a markdown note called ideas with 3 startup ideas")

    call = outcome["trace"][0]
    assert call["tool"] == "write_markdown"
    assert "This is the first startup idea" not in call["params"]["content"]
    assert "freight brokers" in call["params"]["content"].lower()

    written = (tmp_path / "ideas.md").read_text()
    assert "This is the first startup idea" not in written
    assert "Ledger" in written


def test_good_content_is_not_reauthored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    kernel = _kernel(
        tmp_path,
        [
            tool_call_response("write_markdown", {"filename": "ideas", "title": "Startup Ideas", "content": REAL_NOTE}),
            text_response("Saved ideas.md."),
        ],
    )
    kernel.handle("Write a markdown note called ideas with 3 startup ideas")
    # Both scripted responses consumed, none left over: exactly two model
    # calls (route + summarize) — the authoring detour never happened.
    assert kernel.client.responses == []
    assert "Ledger" in (tmp_path / "ideas.md").read_text()


def test_human_approves_the_authored_document_not_the_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    approvals = ApprovalStore(tmp_path / "approvals.json")
    registry = default_registry()
    registry.require_approval("write_markdown")
    kernel = Kernel(
        registry=registry,
        client=FakeClient(
            [
                tool_call_response("write_markdown", {"filename": "ideas", "title": "Startup Ideas", "content": PLACEHOLDER_NOTE}),
                text_response(REAL_NOTE),
                text_response("Prepared ideas.md — it needs your approval."),
            ]
        ),
        model="fake",
        audit=AuditLog(tmp_path),
        approvals=approvals,
    )
    outcome = kernel.handle("Write a markdown note called ideas with 3 startup ideas")

    approval_id = outcome["trace"][0]["approval_id"]
    pending = approvals.get(approval_id)
    # What the human is asked to approve is the REAL document.
    assert "This is the first startup idea" not in pending["params"]["content"]
    assert "Ledger" in pending["params"]["content"]


def test_authoring_is_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    kernel = _kernel(
        tmp_path,
        [
            tool_call_response("write_markdown", {"filename": "ideas", "title": "Startup Ideas", "content": PLACEHOLDER_NOTE}),
            text_response(REAL_NOTE),
            text_response("Saved."),
        ],
    )
    kernel.handle("Write a markdown note called ideas with 3 startup ideas")
    events = [r["event"] for r in kernel.audit.tail(50)]
    assert "content_authored" in events


def test_write_markdown_is_gated_by_default():
    # A guarantee you must remember to switch on is not a guarantee.
    import importlib

    from llm_os import config as fresh
    importlib.reload(fresh)
    assert "write_markdown" in fresh.APPROVAL_TOOLS


def test_gate_can_be_opened_deliberately(monkeypatch):
    import importlib

    monkeypatch.setenv("LLM_OS_APPROVAL_TOOLS", "")
    from llm_os import config as fresh
    importlib.reload(fresh)
    assert fresh.APPROVAL_TOOLS == []
    monkeypatch.undo()
    importlib.reload(fresh)


# ── the model must not be able to narrate a blocked action ──────────────────

DISOBEDIENT = (
    "# ideas\n\n## Startup Idea 1: AI-Powered Project Management Tool\n"
    "Develop an AI-powered project management tool for teams.\n"
)


def _gated_kernel(tmp_path, monkeypatch, final_reply):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    registry = default_registry()
    registry.require_approval("write_markdown")
    return Kernel(
        registry=registry,
        client=FakeClient(
            [
                tool_call_response("write_markdown", {"filename": "ideas", "title": "ideas", "content": REAL_NOTE}),
                text_response(final_reply),
            ]
        ),
        model="fake",
        audit=AuditLog(tmp_path),
        approvals=ApprovalStore(tmp_path / "approvals.json"),
    )


def test_model_cannot_print_the_document_when_the_write_is_blocked(tmp_path, monkeypatch):
    # The model ignores the BLOCKED instruction and prints the finished note.
    # The user would read that and believe it was saved. It was not.
    kernel = _gated_kernel(tmp_path, monkeypatch, DISOBEDIENT)
    outcome = kernel.handle("Write a markdown note called ideas with 3 startup ideas")

    assert "AI-Powered Project Management Tool" not in outcome["reply"]
    assert "Waiting for your approval" in outcome["reply"]
    assert "nothing has been created" in outcome["reply"].lower()
    assert not (tmp_path / "ideas.md").exists()


def test_blocked_reply_names_the_approval(tmp_path, monkeypatch):
    kernel = _gated_kernel(tmp_path, monkeypatch, "Done! I saved it for you.")
    outcome = kernel.handle("Write a markdown note called ideas")
    approval_id = outcome["trace"][0]["approval_id"]
    # A false 'Done!' never reaches the user; the real state does.
    assert "Done!" not in outcome["reply"]
    assert approval_id in outcome["reply"]
    assert "write_markdown" in outcome["reply"]


def test_streamed_tokens_never_leak_the_blocked_document(tmp_path, monkeypatch):
    kernel = _gated_kernel(tmp_path, monkeypatch, DISOBEDIENT)
    streamed = "".join(
        ev["text"]
        for ev in kernel.stream("Write a markdown note called ideas")
        if ev["type"] == "token"
    )
    # What the user watches appear on screen, token by token.
    assert "AI-Powered Project Management Tool" not in streamed
    assert "Waiting for your approval" in streamed


def test_ungated_success_is_still_narrated_by_the_model(tmp_path, monkeypatch):
    # The override must apply ONLY when something is pending.
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    kernel = _kernel(
        tmp_path,
        [
            tool_call_response("write_markdown", {"filename": "ideas", "title": "ideas", "content": REAL_NOTE}),
            text_response("Saved ideas.md with three ideas."),
        ],
    )
    outcome = kernel.handle("Write a markdown note called ideas")
    assert outcome["reply"] == "Saved ideas.md with three ideas."
