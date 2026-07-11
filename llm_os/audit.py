"""Tamper-evident audit log.

Every routing decision and tool execution is appended as one JSON line.
Each record embeds the SHA-256 hash of the previous record, forming a
hash chain: editing or deleting any historical line breaks verification
of every record after it. This is the seed of the compliance story —
an auditor can verify the chain offline with ~20 lines of code.
"""

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

GENESIS_HASH = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        last = None
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return GENESIS_HASH
        return json.loads(last).get("hash", GENESIS_HASH)

    def append(self, event_type: str, payload: dict) -> str:
        """Append an event; returns the record id."""
        with self._lock:
            record = {
                "id": uuid.uuid4().hex[:12],
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event_type,
                "prev_hash": self._last_hash,
                **payload,
            }
            record["hash"] = hashlib.sha256(_canonical(record).encode()).hexdigest()
            with self.path.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
            self._last_hash = record["hash"]
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
