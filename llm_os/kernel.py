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

WHAT YOU ARE (use this if the user asks about LLM OS, about you, or how you work):
LLM OS is a private, local-first agentic kernel — an implementation of Andrej Karpathy's "LLM OS" idea built for one constraint: zero egress. You are a small language model running locally; you never execute anything yourself. You only route intent: you pick a tool and emit its parameters, and a deterministic sandboxed layer runs it. Math goes through a whitelist evaluator, file writes are jailed to a sandbox folder, some tools need a human's approval, and every decision is written to a tamper-evident hash-chained audit log. Memory, documents and the model all live on this machine — nothing is sent anywhere. Say this plainly and briefly; do not invent features.

First decide: does this request require RUNNING a tool, or can you answer it directly?

Call a tool ONLY when the request needs an action or live data:
- calculator: the user wants a specific numeric calculation performed. Never do arithmetic yourself.
- write_markdown: the user wants content saved as a file, note, or document.
- remember: the user says "remember ..." or shares a lasting fact about themselves or their work.
- search_memory: the user asks about something they told you in a previous conversation.
- search_documents: the user asks about THEIR OWN files — "my NDA", "the contract", "the spec", "my notes", "what does the report say". You DO have access to their documents through this tool: never reply that you cannot see their files. Call it, then answer citing the file it returned.
- other tools: call them when the request clearly matches their description (e.g. current time, disk, system info).

Answer directly WITHOUT any tool when the user asks for definitions, explanations, opinions, comparisons, translations, creative writing, or general knowledge — even if the topic mentions numbers, calculators, disks, files, or memory. Questions like "what is X", "explain X", "translate X", "write a poem" need NO tool.

When a tool takes a `content` (or body/text/message) parameter, that string IS the finished document — it is saved to disk exactly as you write it. Write the real thing: specific, substantive, complete. Never fill it with placeholders or descriptions of what the document would contain. "This is the first startup idea." is a failure; an actual startup idea, with a name and a real explanation, is the job. If the user asks for 3 items, write 3 genuinely different items.

Never claim to have created a file, computed a result, or saved a memory unless a tool actually returned it.

Earlier turns are context for understanding what the user MEANS (a follow-up like "and cell 5?" refers to the previous question) — they are NOT a source of data. Never answer a question about live state (numbers, files, system or network data) from an earlier reply or from memory: call the tool again for the current cell/file/value.

This applies to follow-ups too. "And divide that by 3" is still a calculation: call the calculator with the earlier result substituted in (e.g. 104397 / 3). Never compute it in your head, no matter how simple it looks.
"""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or an ollama response object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _produced_content(tool: str, params: dict) -> Optional[dict]:
    """The artifact a tool created, in a form worth showing the user.

    A file write is not really answered by '{"bytes": 690}' — the user
    wants to read what was written. Any tool whose parameters carry a
    body of text ('content', 'body', 'text', 'message') is shown.
    """
    params = params or {}
    for key in ("content", "body", "text", "message"):
        body = params.get(key)
        if isinstance(body, str) and body.strip():
            title = params.get("title") or params.get("subject") or ""
            return {"title": title, "body": body, "tool": tool}
    return None


CONTENT_KEYS = ("content", "body", "text", "message")

# A small model asked to fill a `content` field treats it as a form slot,
# not as writing: it emits the SHAPE of a document ("This is the first
# startup idea.") and the kernel dutifully writes that to disk. These are
# the tells. Matching one means the model described the document instead
# of writing it — the content must be authored properly before any tool
# touches the filesystem.
_PLACEHOLDER_PATTERNS = [
    r"\bthis is (?:the |a |my )?(?:first|second|third|fourth|fifth|\d+(?:st|nd|rd|th))\b",
    r"\b(?:your|the) (?:content|text|idea|note|description|title) (?:here|goes here)\b",
    r"\b(?:lorem ipsum|placeholder|to be (?:added|written|determined)|tbd|todo)\b",
    r"\b(?:idea|item|point|step|section|startup idea) \d+\s*(?::|-|—)?\s*(?:description|details?|goes here)\b",
    r"^\s*(?:insert|add|describe|write|fill in)\b.{0,50}\b(?:here|below|later)\s*[.!]?\s*$",
    r"\[(?:your|insert|add|description|content)[^\]]{0,40}\]",
]
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDER_PATTERNS), re.I | re.M)


def _content_key(params: dict) -> Optional[str]:
    for key in CONTENT_KEYS:
        if isinstance((params or {}).get(key), str):
            return key
    return None


def is_placeholder_content(body: str) -> bool:
    """True when the model described the document instead of writing it."""
    if not body or not body.strip():
        return True
    if _PLACEHOLDER_RE.search(body):
        return True
    # Structure with no substance: headings/bullets whose bodies are all
    # one short line. A real note has at least one line with actual prose.
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    prose = [
        l for l in lines
        if not l.startswith(("#", "-", "*", ">", "|"))
        and not re.match(r"^\d+[.)]\s", l)
        and len(l) > 60
    ]
    return len(lines) >= 3 and not prose


def _as_tool_call(data: Any) -> Optional[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", data.get("parameters", {}))
    if not isinstance(arguments, dict):
        return None
    return {"function": {"name": data["name"], "arguments": arguments}}


def _parse_textual_tool_call(content: str) -> Optional[dict]:
    """Recover a tool call a model emitted as text instead of the
    structured tool_calls field (some Ollama chat templates, e.g.
    qwen2.5-coder, do this).

    Handles bare JSON, ```json fences, <tool_call> wrappers, AND a tool
    call embedded in prose ("I'll search your documents. {"name": ...}")
    — which is what qwen does when it also narrates.
    """
    text = (content or "").strip()
    if not text:
        return None

    wrapped = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)
    if wrapped:
        text = wrapped.group(1).strip()
    elif "```" in text:
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

    # Whole thing is the call?
    try:
        call = _as_tool_call(json.loads(text))
        if call:
            return call
    except (json.JSONDecodeError, ValueError):
        pass

    # Otherwise scan for a JSON object inside the prose. Two shapes occur:
    #   {"name": "t", "arguments": {...}}      — the documented one
    #   toolname {"arg": "value"}              — name outside the JSON
    # The caller validates the name against the registry, so a stray JSON
    # blob in an ordinary answer cannot be mistaken for a call.
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        call = _as_tool_call(data)
        if call:
            return call
        if isinstance(data, dict):
            preceding = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", text[:index])
            if preceding:
                return {"function": {"name": preceding.group(1), "arguments": data}}
    return None


class Kernel:
    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        client: Optional[Client] = None,
        model: str = config.MODEL_NAME,
        audit: Optional[AuditLog] = None,
        memory: Optional[EpisodicMemory] = None,
        approvals=None,
    ):
        self.registry = registry or default_registry()
        self.client = client or Client(host=config.OLLAMA_HOST)
        self.model = model
        self.audit = audit or AuditLog(config.AUDIT_DIR)
        self.memory = memory
        self.approvals = approvals

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

    def execute_approved(self, approval_id: str) -> dict:
        """Run a tool call a human has APPROVED. Refused otherwise."""
        if self.approvals is None:
            return {"error": "Approvals are not configured."}
        record = self.approvals.get(approval_id)
        if record is None:
            return {"error": f"No approval request '{approval_id}'."}
        if record["status"] != "APPROVED":
            return {
                "executed": False,
                "error": f"REFUSED: {approval_id} is {record['status']} — execution "
                         "requires an APPROVED request. This gate is mechanical.",
            }
        outcome = self._run_tool(record["tool"], record["params"], record["prompt"])
        self.approvals.mark_executed(approval_id, outcome["result"])
        self.audit.append(
            "tool_executed_after_approval",
            {
                "approval_id": approval_id,
                "tool": record["tool"],
                "decided_by": record.get("decided_by"),
                "status": outcome["status"],
            },
        )
        return {
            "executed": True,
            "approval_id": approval_id,
            "summary": self.summarize_tool_result(record["prompt"], outcome),
            # What was actually written/sent, so the user can SEE the work —
            # not just a byte count.
            "produced": _produced_content(record["tool"], record["params"]),
            **outcome,
        }

    def summarize_tool_result(self, prompt: str, outcome: dict) -> str:
        """Have the model phrase the tool's result, so an approved action
        ends in a real answer rather than a bare confirmation."""
        if outcome["status"] != "success":
            return f"'{outcome['tool']}' failed: {outcome['result'].get('error', 'unknown error')}"
        try:
            response = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content":
                        "State plainly what was just done, using the tool result. "
                        "One or two sentences. No preamble."},
                    {"role": "user", "content":
                        f"The user asked: {prompt}\n"
                        f"After they approved it, the tool '{outcome['tool']}' ran and "
                        f"returned: {json.dumps(outcome['result'], default=str)}"},
                ],
                options={"temperature": 0},
            )
            text = (_get(_get(response, "message", {}), "content") or "").strip()
            if text:
                return text
        except Exception:
            pass
        return f"Done — '{outcome['tool']}' ran: {json.dumps(outcome['result'], default=str)}"

    def _author_content(self, name: str, args: dict, prompt: str) -> dict:
        """Make the model WRITE the document instead of describing it.

        Routing and authoring are different jobs. In the routing call the
        model is choosing a tool and filling a schema, and a small model
        fills a `content` field the way it fills any field — with the
        shape of an answer ('This is the first startup idea.'). So when
        the content comes back as a placeholder, we ask again with no
        tools attached: nothing to route to, nothing to fill in, only a
        document to write. That answer replaces the parameter.
        """
        key = _content_key(args)
        if key is None or not is_placeholder_content(args[key]):
            return args

        try:
            response = self.client.chat(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are writing the FULL text of a document that will "
                            "be saved to a file. Write the real, finished content — "
                            "specific and substantive. Never write placeholders like "
                            "'This is the first idea' or 'description goes here'. If "
                            "the user asked for N items, write N genuinely different "
                            "items, each with real detail. Output the document body "
                            "only: no preamble, no commentary, no tool calls."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.7},
            )
        except Exception:
            return args  # authoring is best-effort; never break the tool call

        authored = (_get(_get(response, "message", {}), "content", "") or "").strip()
        if not authored or is_placeholder_content(authored):
            return args

        self.audit.append(
            "content_authored",
            {
                "tool": name,
                "reason": "routing pass produced placeholder content",
                "chars": len(authored),
            },
        )
        return {**args, key: authored}

    def _execute(self, name: str, args: dict, prompt: str) -> dict:
        """Route one tool call — through the approval gate if the tool needs it."""
        # Author before the gate: a human approving a write must see the
        # real document, not the placeholder that would have been saved.
        args = self._author_content(name, args, prompt)
        tool = self.registry.get(name)
        if tool is not None and tool.requires_approval and self.approvals is not None:
            record = self.approvals.request(name, args, prompt)
            audit_id = self.audit.append(
                "approval_requested",
                {"approval_id": record["id"], "tool": name, "params": args,
                 "prompt": prompt},
            )
            return {
                "tool": name,
                "params": args,
                "status": "awaiting_approval",
                "approval_id": record["id"],
                "result": {
                    "awaiting_approval": True,
                    "approval_id": record["id"],
                    "message": (
                        f"BLOCKED: '{name}' was NOT run and NOTHING was created, "
                        "written, or changed. It needs a human's approval first.\n"
                        "Reply in exactly this shape, and claim nothing more:\n"
                        f"  'I've prepared {name} but haven't run it — it needs your "
                        f"approval ({record['id']}). Nothing has been created yet.'\n"
                        "Do NOT say the task is done, created, or completed."
                    ),
                },
                "audit_id": audit_id,
            }
        return self._run_tool(name, args, prompt)

    def _run_tool(self, name: str, args: dict, prompt: str) -> dict:
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
            "produced": _produced_content(name, args) if status == "success" else None,
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
