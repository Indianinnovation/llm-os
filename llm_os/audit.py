"""Tamper-evident audit log.

Every routing decision and tool execution is appended as one JSON line.
Each record embeds the SHA-256 hash of the previous record, forming a
hash chain: editing or deleting any historical line breaks verification
of every record after it. This is the seed of the compliance story —
an auditor can verify the chain offline with ~20 lines of code.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import portalock

GENESIS_HASH = "0" * 64
_TAIL_BYTES = 8192  # enough to hold the last record


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _last_hash_of(handle) -> str:
    """Read the final record's hash straight from the file.

    The chain's prev_hash MUST come from what is actually on disk, not
    from memory: two processes sharing an audit directory (a restart that
    overlaps, a second kernel, a cron job) would otherwise each append
    against their own stale idea of the tip and fork the chain — which
    verification then reports as tampering, forever.
    """
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return GENESIS_HASH
    handle.seek(max(0, size - _TAIL_BYTES))
    lines = [line for line in handle.read().splitlines() if line.strip()]
    if not lines:
        return GENESIS_HASH
    try:
        return json.loads(lines[-1]).get("hash", GENESIS_HASH)
    except json.JSONDecodeError:
        return GENESIS_HASH


class AuditLog:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()  # threads within this process

    def append(self, event_type: str, payload: dict) -> str:
        """Append an event under an exclusive file lock; returns the id.

        The lock plus the read-tip-from-disk step make appends safe across
        processes, so the chain stays linear no matter who is writing.
        """
        with self._lock:
            with self.path.open("r+") as f:
                portalock.lock(f)
                try:
                    record = {
                        "id": uuid.uuid4().hex[:12],
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "event": event_type,
                        "prev_hash": _last_hash_of(f),
                        **payload,
                    }
                    record["hash"] = hashlib.sha256(
                        _canonical(record).encode()
                    ).hexdigest()
                    f.seek(0, os.SEEK_END)
                    f.write(json.dumps(record, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    portalock.unlock(f)
            return record["id"]

    def tail(self, n: int = 20) -> list:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            lines = [line for line in f if line.strip()]
        return [json.loads(line) for line in lines[-n:]]

    def verify_chain(self) -> bool:
        """Recompute every hash; False means the log was tampered with."""
        if not self.path.exists():
            return True
        prev = GENESIS_HASH
        with self.path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                claimed = record.pop("hash", None)
                if record.get("prev_hash") != prev:
                    return False
                if hashlib.sha256(_canonical(record).encode()).hexdigest() != claimed:
                    return False
                prev = claimed
        return True
