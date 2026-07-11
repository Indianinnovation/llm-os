"""The routing kernel.

The LLM never executes anything. It only selects a tool and emits
structured parameters via Ollama's native tool-calling API; the kernel
validates those parameters (Pydantic), executes the deterministic tool,
records the hash-chained audit trail, then asks the model to phrase the
final answer from the tool result.
"""

import json
import time
from typing import Any, Optional

from ollama import Client

from . import config
from .audit import AuditLog
from .memory import EpisodicMemory
from .registry import ToolError, ToolRegistry
from .tools import default_registry

SYSTEM_PROMPT = """You are the routing kernel of LLM OS, a private assistant that runs entirely on this machine.

Rules:
- For any calculation, ALWAYS call the calculator tool. Never do arithmetic yourself.
- When the user asks to write, save, or generate a document/note/report as a file, ALWAYS call the write_markdown tool.
- If another available tool matches the user's request (for example system or time information), call that tool instead of guessing.
- When the user asks you to remember something, or tells you a lasting fact about themselves or their work, call the remember tool with that fact.
- For general questions with no matching tool, answer directly and concisely.
- Never claim to have created a file or computed a result unless a tool actually returned it.
"""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or an ollama response object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class Kernel:
    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        client: Optional[Client] = None,
        model: str = config.MODEL_NAME,
        audit: Optional[AuditLog] = None,
        memory: Optional[EpisodicMemory] = None,
    ):
        self.registry = registry or default_registry()
        self.client = client or Client(host=config.OLLAMA_HOST)
        self.model = model
        self.audit = audit or AuditLog(config.AUDIT_DIR)
        self.memory = memory

    def _page_in_memories(self, prompt: str) -> list:
        """MemGPT-style paging: pull relevant long-term memories into the
        context window for this request."""
        if self.memory is None:
            return []
        try:
            return self.memory.recall(prompt)
        except Exception as exc:  # memory failures must never block routing
            self.audit.append("memory_error", {"stage": "recall", "error": str(exc)})
            return []

    def _archive_exchange(self, prompt: str, reply: str) -> None:
        if self.memory is None or not reply:
            return
        try:
            self.memory.archive(f"User said: {prompt}\nAssistant replied: {reply}")
        except Exception as exc:
            self.audit.append("memory_error", {"stage": "archive", "error": str(exc)})

    def handle(self, prompt: str) -> dict:
        """Route one user prompt; returns reply text plus the full trace."""
        started = time.time()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        memories = self._page_in_memories(prompt)
        if memories:
            recalled = "\n".join(f"- ({m['ts']}) {m['text']}" for m in memories)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Possibly relevant memories from previous sessions "
                        "(ignore any that do not apply):\n" + recalled
                    ),
                }
            )
        messages.append({"role": "user", "content": prompt})
        trace = []

        for _ in range(config.MAX_TOOL_CALLS):
            response = self.client.chat(
                model=self.model, messages=messages, tools=self.registry.specs()
            )
            message = _get(response, "message", {})
            tool_calls = _get(message, "tool_calls") or []

            if not tool_calls:
                reply = (_get(message, "content") or "").strip()
                self.audit.append(
                    "chat" if not trace else "chat_after_tools",
                    {
                        "prompt": prompt,
                        "tools_used": [t["tool"] for t in trace],
                        "memories_recalled": len(memories),
                    },
                )
                self._archive_exchange(prompt, reply)
                return self._result(reply, trace, memories, started)

            messages.append(
                {
                    "role": "assistant",
                    "content": _get(message, "content") or "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": _get(_get(tc, "function", {}), "name"),
                                "arguments": _get(_get(tc, "function", {}), "arguments"),
                            }
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                function = _get(call, "function", {})
                name = _get(function, "name")
                args = _get(function, "arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                outcome = self._execute(name, args, prompt)
                trace.append(outcome)
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(outcome["result"], default=str),
                    }
                )

        # Tool-call budget exhausted; return whatever we have.
        return self._result(
            "I hit the tool-call limit for a single request.", trace, memories, started
        )

    def _execute(self, name: str, args: dict, prompt: str) -> dict:
        tool = self.registry.get(name)
        started = time.time()
        if tool is None:
            result: Any = {"error": f"Tool '{name}' is not registered."}
            status = "unknown_tool"
        else:
            try:
                result = tool.run(args)
                status = "success"
            except ToolError as exc:
                result = {"error": str(exc)}
                status = "tool_error"
            except Exception as exc:  # tool bugs must not kill the kernel
                result = {"error": f"Unexpected tool failure: {exc}"}
                status = "tool_crash"
        audit_id = self.audit.append(
            "tool_execution",
            {
                "prompt": prompt,
                "tool": name,
                "params": args,
                "status": status,
                "result": result,
                "duration_ms": round((time.time() - started) * 1000, 1),
            },
        )
        return {
            "tool": name,
            "params": args,
            "status": status,
            "result": result,
            "audit_id": audit_id,
        }

    def _result(
        self, reply: str, trace: list, memories: list, started: float
    ) -> dict:
        return {
            "reply": reply,
            "trace": trace,
            "memories": memories,
            "model": self.model,
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
