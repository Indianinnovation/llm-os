"""Content redaction for the audit log — tamper-evidence without retention.

A hash chain and a right to erasure pull in opposite directions: the
property that makes the log trustworthy to an auditor (no record can be
altered or removed) is exactly what makes it impossible to delete a
sentence someone asked you to forget.

The way out is to never write the sentence down. Instead of the prompt,
the chain stores a *commitment* to it: HMAC-SHA256(salt, text). That is
enough to prove afterwards that a specific prompt produced a specific
record — you re-derive the commitment and compare — while the log itself
holds nothing readable. The auditor still gets an unbroken chain of what
happened, when, with which tool, and who approved it.

Two consequences worth understanding:

  * The salt lives in one file outside the chain (`audit/.salt`, 0600).
    Destroy it and every commitment becomes permanently unverifiable —
    crypto-erasure. That is a feature: it is how you honour "delete my
    data" for a log you are not allowed to rewrite.

  * A commitment is not encryption. Anyone holding the salt can test a
    guess ("was the prompt X?"). It proves the past; it does not hide a
    value someone can enumerate.

Set LLM_OS_AUDIT_CONTENT=plaintext to keep full forensic detail instead.
"""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

# Fields that carry what the user actually said, wrote, or read. Anything
# here is content, not structure: it gets committed, never stored.
CONTENT_KEYS = frozenset(
    {
        "prompt",       # what the user typed
        "query",        # what was searched for
        "content",      # a document body being written
        "body",
        "text",
        "message",
        "excerpt",      # a chunk of a retrieved document
        "expression",   # a calculation, which may embed figures
        "reply",
        "answer",
        "note",
        "detail",
    }
)

REDACTED = "redacted"
PLAINTEXT = "plaintext"


def _salt_path(audit_dir: Path) -> Path:
    return Path(audit_dir) / ".salt"


def load_or_create_salt(audit_dir: Path) -> bytes:
    """The commitment key. One per install, never in the chain, 0600.

    Deleting this file is the erasure switch: the chain still verifies,
    but nothing in it can ever be linked back to a plaintext again.
    """
    path = _salt_path(audit_dir)
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    path.write_bytes(salt)
    os.chmod(path, 0o600)
    return salt


def commit(value: Any, salt: bytes) -> str:
    """HMAC-SHA256 over the canonical form of a value."""
    canonical = (
        value if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    )
    return hmac.new(salt, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _redact_value(value: Any, salt: bytes) -> dict:
    canonical = value if isinstance(value, str) else json.dumps(value, default=str)
    return {
        "redacted": True,
        "commitment": commit(value, salt),
        "chars": len(canonical),
    }


def redact(payload: Any, salt: bytes) -> Any:
    """Replace every content-bearing field with a commitment to it.

    Structure survives — tool names, statuses, durations, ids, citations,
    approvals — because that is what an auditor reads. Only the words go.
    """
    if isinstance(payload, dict):
        return {
            key: (
                _redact_value(value, salt)
                if key in CONTENT_KEYS and value not in (None, "", [], {})
                else redact(value, salt)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item, salt) for item in payload]
    return payload


def matches(record: dict, text: str, salt: bytes) -> list:
    """Which fields of this record commit to `text`? (proof after the fact)"""
    found = []

    def walk(node, path=""):
        if isinstance(node, dict):
            if node.get("redacted") and node.get("commitment") == commit(text, salt):
                found.append(path or "(root)")
                return
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(record)
    return found
