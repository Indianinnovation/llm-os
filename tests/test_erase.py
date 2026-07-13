"""Crypto-erasure must cover every store, not just the audit chain.

Deleting audit/.salt makes audit content unrecoverable — but the same
prompts and document bodies also live in memory_store/, conversations/,
approvals.json, and the document index. `erase_all` clears those AND rotates
the salt, so "delete my data" is honoured everywhere at once.
"""

import json

from llm_os import redact
from llm_os.audit import AuditLog
from scripts.erase import erase_all

SECRET = "ZORP-9971-QUELVIN"


def _make_stores(base):
    (base / "memory_store").mkdir()
    (base / "memory_store" / "chroma.sqlite3").write_text(f"embedding of {SECRET}")
    (base / "document_index").mkdir()
    (base / "document_index" / "idx.bin").write_text(SECRET)
    (base / "documents").mkdir()
    (base / "documents" / "nda.md").write_text(f"liability {SECRET}")
    (base / "conversations").mkdir()
    (base / "conversations" / "c1.json").write_text(json.dumps({"prompt": SECRET}))
    (base / "approvals.json").write_text(json.dumps({"AP-1": {"content": SECRET}}))
    audit = AuditLog(base / "audit", content_mode=redact.REDACTED)
    audit.append("chat", {"prompt": SECRET})
    return audit


def _grep(base) -> int:
    hits = 0
    for path in base.rglob("*"):
        if path.is_file():
            try:
                if SECRET in path.read_text(errors="ignore"):
                    hits += 1
            except OSError:
                pass
    return hits


def test_secret_is_present_before_erase(tmp_path):
    _make_stores(tmp_path)
    assert _grep(tmp_path) >= 5   # memory, index, doc, conversation, approvals


def test_erase_all_removes_every_cleartext_store(tmp_path):
    audit = _make_stores(tmp_path)
    old_salt = audit.salt

    removed = erase_all(
        memory_dir=tmp_path / "memory_store",
        document_index_dir=tmp_path / "document_index",
        documents_dir=tmp_path / "documents",
        conversations_dir=tmp_path / "conversations",
        approvals_file=tmp_path / "approvals.json",
        audit_dir=tmp_path / "audit",
    )

    # No readable copy of the secret survives in any plaintext store.
    for store in ("memory_store", "document_index", "documents", "conversations"):
        remaining = list((tmp_path / store).rglob("*"))
        assert all(SECRET not in p.read_text(errors="ignore")
                   for p in remaining if p.is_file())
    assert not (tmp_path / "approvals.json").exists()

    # The salt was rotated: the audit record's commitment is no longer
    # derivable from the new salt — its content is cryptographically gone.
    new_salt = redact.load_or_create_salt(tmp_path / "audit")
    assert new_salt != old_salt
    assert removed["salt_rotated"] is True


def test_erase_is_idempotent(tmp_path):
    _make_stores(tmp_path)
    erase_all(
        memory_dir=tmp_path / "memory_store",
        document_index_dir=tmp_path / "document_index",
        documents_dir=tmp_path / "documents",
        conversations_dir=tmp_path / "conversations",
        approvals_file=tmp_path / "approvals.json",
        audit_dir=tmp_path / "audit",
    )
    # Running again on already-clean stores must not raise.
    erase_all(
        memory_dir=tmp_path / "memory_store",
        document_index_dir=tmp_path / "document_index",
        documents_dir=tmp_path / "documents",
        conversations_dir=tmp_path / "conversations",
        approvals_file=tmp_path / "approvals.json",
        audit_dir=tmp_path / "audit",
    )
