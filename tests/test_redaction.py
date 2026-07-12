"""Audit redaction: tamper-evidence WITHOUT retention.

A hash chain cannot be redacted afterwards — that is what makes it worth
something. So content never enters it: the chain stores an HMAC commitment
to each prompt, document body and excerpt. What happened stays provable;
what was said stays unreadable; and destroying the salt makes the link
permanently underivable, which is the only honest way to serve an erasure
request against a log you may not rewrite.
"""

import json

import pytest

from llm_os import redact
from llm_os.audit import AuditLog

SECRET = "My access code is ZORPTHAX-9971-QUELVIN and my client is Vandelay."
NDA_BODY = "The liability cap is USD 250,000 under Delaware law."


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path, content_mode=redact.REDACTED)


def _raw(log) -> str:
    return log.path.read_text()


def test_the_prompt_never_reaches_the_disk(log):
    log.append("tool_execution", {"prompt": SECRET, "tool": "calculator", "status": "success"})
    assert "ZORPTHAX" not in _raw(log)
    assert "Vandelay" not in _raw(log)


def test_document_bodies_and_excerpts_never_reach_the_disk(log):
    log.append(
        "tool_execution",
        {
            "tool": "write_markdown",
            "params": {"filename": "nda-notes", "content": NDA_BODY},
            "result": {"matches": [{"citation": "nda.md", "excerpt": NDA_BODY}]},
        },
    )
    raw = _raw(log)
    assert "250,000" not in raw
    assert "Delaware" not in raw


def test_structure_survives_because_that_is_what_an_auditor_reads(log):
    log.append(
        "tool_executed_after_approval",
        {"prompt": SECRET, "tool": "write_markdown", "status": "success",
         "decided_by": "dilip", "approval_id": "AP-123456", "duration_ms": 4.2},
    )
    record = json.loads(_raw(log).splitlines()[0])
    assert record["tool"] == "write_markdown"
    assert record["status"] == "success"
    assert record["decided_by"] == "dilip"          # who approved it
    assert record["approval_id"] == "AP-123456"
    assert record["duration_ms"] == 4.2
    assert record["prompt"]["redacted"] is True     # …but not what was said
    assert record["prompt"]["chars"] == len(SECRET)


def test_the_chain_still_verifies(log):
    for i in range(5):
        log.append("tool_execution", {"prompt": f"{SECRET} {i}", "tool": "calculator"})
    assert log.verify_chain() is True


def test_a_prompt_can_still_be_PROVEN_to_have_produced_a_record(log):
    log.append("chat", {"prompt": SECRET, "tools_used": []})
    record = json.loads(_raw(log).splitlines()[0])

    assert redact.matches(record, SECRET, log.salt) == ["prompt"]
    assert redact.matches(record, "some other prompt", log.salt) == []


def test_destroying_the_salt_makes_the_link_underivable_forever(log, tmp_path):
    """Crypto-erasure: the record stays, the chain stays valid, and nobody —
    including us — can ever link it back to the text again."""
    log.append("chat", {"prompt": SECRET, "tools_used": []})
    record = json.loads(_raw(log).splitlines()[0])
    old_salt = log.salt

    (tmp_path / ".salt").unlink()                      # the erasure switch
    new_log = AuditLog(tmp_path, content_mode=redact.REDACTED)

    assert new_log.salt != old_salt                    # a fresh salt
    assert redact.matches(record, SECRET, new_log.salt) == []   # no longer provable
    assert new_log.verify_chain() is True              # but history is intact


def test_plaintext_mode_is_available_for_full_forensics(tmp_path):
    log = AuditLog(tmp_path, content_mode=redact.PLAINTEXT)
    log.append("chat", {"prompt": SECRET})
    assert "ZORPTHAX" in _raw(log)
    assert log.salt is None
    assert log.verify_chain() is True


def test_commitments_are_salted_so_two_installs_do_not_match(tmp_path):
    """Without a salt, an attacker with the log could confirm a guessed
    prompt across every deployment at once. The salt makes each install's
    commitments meaningless anywhere else."""
    a = AuditLog(tmp_path / "a", content_mode=redact.REDACTED)
    b = AuditLog(tmp_path / "b", content_mode=redact.REDACTED)
    assert redact.commit(SECRET, a.salt) != redact.commit(SECRET, b.salt)


def test_empty_values_are_not_pointlessly_committed(log):
    log.append("chat", {"prompt": "", "note": None, "tool": "calculator"})
    record = json.loads(_raw(log).splitlines()[0])
    assert record["prompt"] == ""      # nothing to hide
    assert record["note"] is None
