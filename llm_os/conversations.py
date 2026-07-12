"""Conversation persistence — "what did I ask yesterday, and what did it do?"

Chats survive a refresh, a restart, and a reboot. Stored locally as JSON
(same machine, same guarantees), each conversation keeps its turns and
the audit ids of every tool the agent ran — so a lawyer or a NOC engineer
can reopen a session and trace exactly what happened, months later.
"""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

TITLE_LENGTH = 60


class ConversationStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, conversation_id: str) -> Path:
        safe = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
        return self.dir / f"{safe}.json"

    def create(self, first_prompt: str = "") -> dict:
        record = {
            "id": f"conv-{uuid.uuid4().hex[:10]}",
            "title": (first_prompt.strip()[:TITLE_LENGTH] or "New conversation"),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "turns": [],
        }
        with self._lock:
            self._path(record["id"]).write_text(json.dumps(record, indent=2))
        return record

    def get(self, conversation_id: str) -> Optional[dict]:
        path = self._path(conversation_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def append_turn(
        self,
        conversation_id: str,
        prompt: str,
        reply: str,
        trace: List[dict] = None,
        memories: List[dict] = None,
    ) -> Optional[dict]:
        """Record one exchange, keeping the audit ids of what the agent ran."""
        with self._lock:
            record = self.get(conversation_id)
            if record is None:
                return None
            record["turns"].append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "prompt": prompt,
                "reply": reply,
                "tools": [
                    {"tool": t.get("tool"), "status": t.get("status"),
                     "audit_id": t.get("audit_id")}
                    for t in (trace or [])
                ],
                "memories_recalled": len(memories or []),
            })
            if record["title"] in ("", "New conversation") and prompt.strip():
                record["title"] = prompt.strip()[:TITLE_LENGTH]
            record["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._path(conversation_id).write_text(json.dumps(record, indent=2))
            return record

    def history_for(self, conversation_id: str, turns: int) -> List[dict]:
        """The last N exchanges, as chat messages the kernel can replay."""
        record = self.get(conversation_id)
        if record is None:
            return []
        messages = []
        for turn in record["turns"][-turns:]:
            messages.append({"role": "user", "content": turn["prompt"]})
            if turn.get("reply"):
                messages.append({"role": "assistant", "content": turn["reply"]})
        return messages

    def list(self, limit: int = 50) -> List[dict]:
        items = []
        for path in self.dir.glob("conv-*.json"):
            try:
                record = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            items.append({
                "id": record["id"],
                "title": record["title"],
                "updated": record["updated"],
                "turns": len(record["turns"]),
                "tools_used": sum(len(t.get("tools", [])) for t in record["turns"]),
            })
        items.sort(key=lambda r: r["updated"], reverse=True)
        return items[:limit]

    def delete(self, conversation_id: str) -> bool:
        path = self._path(conversation_id)
        if not path.exists():
            return False
        path.unlink()
        return True
