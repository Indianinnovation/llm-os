"""Human-in-the-loop approval gates for tools.

Some tools change the world: they write files, send messages, execute
changes. For those, the model may *propose* — but the kernel refuses to
run them until a human approves. The gate is mechanical (state, checked
before execution), not advisory (a line in a prompt), and every decision
is written to the tamper-evident audit chain.

    kernel  → tool needs approval → PENDING request, execution stops
    human   → POST /approvals/{id}  (approve | reject)
    kernel  → runs it (or doesn't) and records who decided
"""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


class ApprovalStore:
    """Pending/decided tool calls, persisted so a restart cannot lose them."""

    def __init__(self, path: Path, ttl_seconds: int = 3600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str))

    def request(self, tool: str, params: dict, prompt: str) -> dict:
        """Create a PENDING request. The tool has NOT run."""
        with self._lock:
            data = self._load()
            record = {
                "id": f"AP-{uuid.uuid4().hex[:6].upper()}",
                "tool": tool,
                "params": params,
                "prompt": prompt,
                "status": "PENDING",
                "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "expires_at": time.time() + self.ttl,
                "decided_by": None,
                "result": None,
            }
            data[record["id"]] = record
            self._save(data)
            return record

    def get(self, approval_id: str) -> Optional[dict]:
        return self._load().get(approval_id)

    def decide(self, approval_id: str, decision: str, who: str = "user") -> dict:
        with self._lock:
            data = self._load()
            record = data.get(approval_id)
            if record is None:
                return {"error": f"No approval request '{approval_id}'."}
            if record["status"] != "PENDING":
                return {"error": f"{approval_id} is already {record['status']}."}
            if time.time() > record["expires_at"]:
                record["status"] = "EXPIRED"
                self._save(data)
                return {"error": f"{approval_id} expired before a decision was made."}

            record["status"] = "APPROVED" if decision.lower().startswith("a") else "REJECTED"
            record["decided_by"] = who
            record["decided_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save(data)
            return record

    def mark_executed(self, approval_id: str, result: dict) -> None:
        with self._lock:
            data = self._load()
            if approval_id in data:
                data[approval_id]["status"] = "EXECUTED"
                data[approval_id]["result"] = result
                self._save(data)

    def pending(self) -> list:
        now = time.time()
        return [
            r for r in self._load().values()
            if r["status"] == "PENDING" and r["expires_at"] > now
        ]

    def recent(self, limit: int = 20) -> list:
        records = sorted(
            self._load().values(), key=lambda r: r["requested_at"], reverse=True
        )
        return records[:limit]
