"""The routing kernel.

The LLM never executes anything. It only selects a tool and emits
structured parameters via Ollama's native tool-calling API; the kernel
validates those parameters (Pydantic), executes the deterministic tool,
records the hash-chained audit trail, then asks the model to phrase the
final answer from the tool result.
"""

import json
import re
import time
from typing import Any, Optional

from ollama import Client

from . import config
from .audit import AuditLog
from .memory import EpisodicMemory
from .registry import ToolError, ToolRegistry
from .tools import default_registry

SYSTEM_PROMPT = """You are the routing kernel of LLM OS, a private assistant that runs entirely on this machine.

First decide: does this request require RUNNING a tool, or can you answer it directly?

Call a tool ONLY when the request needs an action or live data:
- calculator: the user wants a specific numeric calculation performed. Never do arithmetic yourself.
- write_markdown: the user wants content saved as a file, note, or document.
- remember: the user says "remember ..." or shares a lasting fact about themselves or their work.
- search_memory: the user asks about something they told you in a previous conversation.
- other tools: call them when the request clearly matches their description (e.g. current time, disk, system info).

Answer directly WITHOUT any tool when the user asks for definitions, explanations, opinions, comparisons, translations, creative writing, or general knowledge — even if the topic mentions numbers, calculators, disks, files, or memory. Questions like "what is X", "explain X", "translate X", "write a poem" need NO tool.

Never claim to have created a file, computed a result, or saved a memory unless a tool actually returned it.

Earlier turns are context for understanding what the user MEANS (a follow-up like "and cell 5?" refers to the previous question) — they are NOT a source of data. Never answer a question about live state (numbers, files, system or network data) from an earlier reply or from memory: call the tool again for the current cell/file/value.
"""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or an ollama response object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_textual_tool_call(content: str) -> Optional[dict]:
    """Recover a tool call a model emitted as text instead of the
    structured tool_calls field (some Ollama chat templates, e.g.
    qwen2.5-coder, do this). Accepts bare JSON, ```json fences, and
    <tool_call> wrappers shaped like {"name": ..., "arguments": ...}."""
    text = (content or "").strip()
    wrapped = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)
    if wrapped:
        text = wrapped.group(1)
    elif text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", data.get("parameters", {}))
    if not isinstance(arguments, dict):
        return None
    return {"function": {"name": data["name"], "arguments": arguments}}


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

    def _archive_exchange(self, prompt: str, reply: str, trace: Optional[list] = None) -> None:
        """Archive what the USER said — never the assistant's own reply.

        Storing model output as memory creates a feedback loop: a wrong
        answer gets recalled later as if it were fact, and the model
        repeats it instead of calling the tool. Memory holds the user's
        statements and requests; live data always comes from tools.
        """
        if self.memory is None or not prompt.strip():
            return
        tools_used = sorted({t["tool"] for t in (trace or [])})
        note = f"User said: {prompt.strip()}"
        if tools_used:
            note += f"\n(answered using: {', '.join(tools_used)})"
        try:
            self.memory.archive(note)
        except Exception as exc:
            self.audit.append("memory_error", {"stage": "archive", "error": str(exc)})

    def _build_messages(self, prompt: str, history: Optional[list] = None) -> tuple:
        """System prompt + paged-in memories + recent turns + this prompt.

        `history` is the client's recent conversation ([{role, content}, …]);
        the last MAX_HISTORY_TURNS exchanges are included so follow-ups like
        "and cell 5?" or "summarize that" work. Only text is replayed — never
        past tool payloads, which would bloat context for a small model.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        memories = self._page_in_memories(prompt)
        if memories:
            recalled = "\n".join(f"- ({m['ts']}) {m['text']}" for m in memories)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Notes from PAST conversations. They record what was said "
                        "before and MAY BE OUT OF DATE — including any numbers or "
                        "system state in them. Use them only to understand the "
                        "user's context and preferences. NEVER answer a question "
                        "about current state (values, files, alarms, network data) "
                        "from these notes: call the tool and use its result.\n"
                        + recalled
                    ),
                }
            )

        for turn in (history or [])[-(config.MAX_HISTORY_TURNS * 2):]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:4000]})

        messages.append({"role": "user", "content": prompt})
        return messages, memories

    def handle(self, prompt: str, history: Optional[list] = None) -> dict:
        """Route one user prompt; returns reply text plus the full trace."""
        started = time.time()
        messages, memories = self._build_messages(prompt, history)
        trace = []

        for _ in range(config.MAX_TOOL_CALLS):
            # Routing must be reproducible: greedy decoding, no sampling.
            response = self.client.chat(
                model=self.model,
                messages=messages,
                tools=self.registry.specs(),
                options={"temperature": 0},
            )
            message = _get(response, "message", {})
            tool_calls = _get(message, "tool_calls") or []

            if not tool_calls:
                # Correction loop: only promote textual JSON to a tool call
                # if it names a registered tool; anything else is a reply.
                recovered = _parse_textual_tool_call(_get(message, "content"))
                if recovered and self.registry.get(recovered["function"]["name"]):
                    tool_calls = [recovered]
                    message = {"content": "", "tool_calls": tool_calls}

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
                self._archive_exchange(prompt, reply, trace)
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

    def stream(self, prompt: str, history: Optional[list] = None):
        """Same routing as handle(), but yields events as they happen:

            {"type": "memories", ...}   memories paged in (once, if any)
            {"type": "tool", ...}       a tool was executed (name, status, audit id)
            {"type": "token", "text"}   a chunk of the final answer
            {"type": "done", ...}       reply text + trace + duration

        Local models are slow; watching the answer appear is the difference
        between "thinking…" and a usable product.
        """
        started = time.time()
        messages, memories = self._build_messages(prompt, history)
        trace = []
        if memories:
            yield {"type": "memories", "memories": memories}

        for _ in range(config.MAX_TOOL_CALLS):
            # Non-streamed pass first: we must know whether the model wants a
            # tool before we can stream anything to the user.
            response = self.client.chat(
                model=self.model,
                messages=messages,
                tools=self.registry.specs(),
                options={"temperature": 0},
            )
            message = _get(response, "message", {})
            tool_calls = _get(message, "tool_calls") or []
            if not tool_calls:
                recovered = _parse_textual_tool_call(_get(message, "content"))
                if recovered and self.registry.get(recovered["function"]["name"]):
                    tool_calls = [recovered]
                    message = {"content": "", "tool_calls": tool_calls}

            if not tool_calls:
                reply = (_get(message, "content") or "").strip()
                if not trace:
                    # No tools involved: re-run streamed so the user sees tokens.
                    reply = ""
                    for chunk in self.client.chat(
                        model=self.model, messages=messages,
                        options={"temperature": 0}, stream=True,
                    ):
                        piece = _get(_get(chunk, "message", {}), "content") or ""
                        if piece:
                            reply += piece
                            yield {"type": "token", "text": piece}
                else:
                    for piece in reply.split(" "):
                        yield {"type": "token", "text": piece + " "}
                self.audit.append(
                    "chat" if not trace else "chat_after_tools",
                    {
                        "prompt": prompt,
                        "tools_used": [t["tool"] for t in trace],
                        "memories_recalled": len(memories),
                    },
                )
                self._archive_exchange(prompt, reply, trace)
                yield {"type": "done", **self._result(reply.strip(), trace, memories, started)}
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": _get(message, "content") or "",
                    "tool_calls": [
                        {"function": {
                            "name": _get(_get(tc, "function", {}), "name"),
                            "arguments": _get(_get(tc, "function", {}), "arguments"),
                        }}
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
                yield {"type": "tool_start", "tool": name, "params": args}
                outcome = self._execute(name, args, prompt)
                trace.append(outcome)
                yield {"type": "tool", **outcome}
                messages.append(
                    {"role": "tool", "content": json.dumps(outcome["result"], default=str)}
                )

        yield {
            "type": "done",
            **self._result("I hit the tool-call limit for a single request.",
                           trace, memories, started),
        }

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
