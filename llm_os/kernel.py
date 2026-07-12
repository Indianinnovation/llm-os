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


def retrieval_matches(result: Any) -> Optional[list]:
    """The matches of a retrieval tool, or None if this isn't one.

    Any tool — built-in or third-party MCP — that answers with
    {"matches": [{"citation": …, "relevance": …}]} is making an
    evidentiary claim, and the kernel holds it to one.
    """
    if not isinstance(result, dict):
        return None
    matches = result.get("matches")
    if not isinstance(matches, list):
        return None
    if matches and not isinstance(matches[0], dict):
        return None
    if matches and "citation" not in matches[0]:
        return None
    return matches


def filter_weak_matches(result: dict, matches: list) -> tuple:
    """Drop chunks too weak to be evidence, BEFORE the model sees them.

    This is not tidying. Asked to summarize TS 38.331, retrieval returned
    a 0.434 chunk of boilerplate plus a chunk of a *different* spec — and
    the model wrote a confident summary that mixed the two and cited
    neither. A weak chunk is not a hint; it is an invitation to invent.
    """
    strong = [m for m in matches if m.get("relevance", 0) >= config.MIN_RELEVANCE]
    filtered = {**result, "matches": strong}
    if len(strong) < len(matches):
        filtered["weak_matches_dropped"] = len(matches) - len(strong)
    return filtered, strong


# Does the question ask about the USER'S OWN material, or about the world?
# "Summarize TS 38.331" / "what does my NDA say" is a claim about their
# corpus: if retrieval comes up empty, answering from memory is a lie with
# a citation's clothes on, and the kernel refuses. "What is 5G?" is a
# question about the world: the corpus was never the point, and refusing is
# just a broken assistant. The two failures look identical to the model —
# it called the same tool — so the kernel tells them apart, not the model.
_CORPUS_SCOPED_RE = re.compile(
    r"""
      \b(?:my|our|the)\s+
        (?:nda|contract|agreement|document|documents|file|files|spec|specs|
           specification|policy|policies|report|note|notes|paper|manual)\b
    | \bTS\s?\d{2}[.\s]?\d{3}\b          # 3GPP-style identifier: TS 38.331
    | \b\w[\w-]*\.(?:md|pdf|txt|docx?)\b # a filename
    | \bin\s+(?:my|our|the)\s+(?:corpus|documents|files|specs)\b
    | \b(?:section|clause|annex|chapter)\s+\d
    """,
    re.I | re.X,
)


def is_corpus_scoped(prompt: str) -> bool:
    return bool(_CORPUS_SCOPED_RE.search(prompt or ""))


def ungrounded_reply(trace: list, prompt: str = "") -> Optional[str]:
    """The reply when retrieval found nothing — but ONLY when the question
    was about the user's own material.

    The model must not be the one to decide whether it knows. Left alone it
    says 'I was unable to find a direct summary… however, I can provide a
    general overview' and then invents one, which in a spec or a contract is
    the most dangerous thing this system can do. But that danger comes from
    *false attribution*, not from general knowledge as such — so this refusal
    applies only when the user was asking about their corpus.
    """
    empty = [t for t in (trace or []) if t.get("retrieval") and not t.get("citations")]
    if not empty or any(t.get("citations") for t in (trace or [])):
        return None
    if not is_corpus_scoped(prompt):
        return None  # a question about the world — answer it (see _answer_unaided)
    tools = ", ".join(sorted({f"`{t['tool']}`" for t in empty}))
    return (
        "**I could not find that in your indexed material, so I am not going "
        "to answer from memory.**\n\n"
        f"Searched with {tools} — nothing scored above the relevance floor "
        f"({config.MIN_RELEVANCE}). Anything I said next would be my own "
        "recollection dressed up as a citation.\n\n"
        "Add the source to your corpus and re-index, or ask me something the "
        "corpus actually covers."
    )


def needs_unaided_answer(trace: list, prompt: str) -> bool:
    """A general question that was mis-routed into a search that found
    nothing. The tool was a mistake; the question still deserves an answer."""
    if not trace or is_corpus_scoped(prompt):
        return False
    searched = [t for t in trace if t.get("retrieval")]
    return bool(searched) and not any(t.get("citations") for t in trace)


UNAIDED_NOTE = (
    "*Answered from the model's own knowledge — this is not from your "
    "documents, and it carries no citation.*\n\n"
)

# Everything the kernel adds to a reply: the ungrounded banner and the Sources
# block. These are the kernel speaking ABOUT the model, not the model speaking.
# Replayed into the next turn as though the assistant had written them, the
# model imitates the pattern and stamps the banner on its own output — so the
# user sees it twice. Strip them at both boundaries: before text re-enters the
# context, and before the kernel prepends the banner itself.
_KERNEL_NOTE_RE = re.compile(
    r"\*Answered from the model's own knowledge[^\n]*\*\s*"
    r"|\n*---\n\*\*Sources\*\*\n(?:- .*\n?)*",
)


def strip_kernel_notes(text: str) -> str:
    """Remove kernel-authored annotations from a reply."""
    return _KERNEL_NOTE_RE.sub("", text or "").strip()


def sources_block(trace: list) -> str:
    """Citations, appended by the kernel rather than requested of the model.

    A cited answer whose citation is optional is an uncited answer with
    good manners.
    """
    citations = []
    for call in trace or []:
        for citation in call.get("citations") or []:
            if citation not in citations:
                citations.append(citation)
    if not citations:
        return ""
    return "\n\n---\n**Sources**\n" + "\n".join(f"- {c}" for c in citations)


def blocked_reply(trace: list) -> Optional[str]:
    """The reply to show when a tool is waiting on a human.

    The gate itself is mechanical, so its *message* must be too. Asked to
    write a note, a small model will happily print the finished document
    into the chat even though the write was blocked — the user then reads
    a completed note and reasonably believes it was saved. Nothing was.
    So when anything is pending we do not let the model narrate the
    outcome at all: the kernel states it, from the trace, deterministically.
    """
    pending = [t for t in (trace or []) if t.get("status") == "awaiting_approval"]
    if not pending:
        return None
    lines = [
        "**Waiting for your approval — nothing has run, and nothing has been "
        "created, written, or changed.**",
        "",
    ]
    for call in pending:
        lines.append(f"- `{call['tool']}` — approval `{call.get('approval_id', '?')}`")
    lines.append("")
    lines.append(
        "Review it above and choose **Approve & run** or **Reject**. "
        "The content shown is a *preview*: it exists only in this request."
    )
    return "\n".join(lines)


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
        past tool payloads, which would bloat context for a small model, and
        never the kernel's own annotations, which the model would imitate.
        """
        history = [
            {**turn, "content": strip_kernel_notes(turn.get("content", ""))}
            if turn.get("role") == "assistant" else turn
            for turn in (history or [])
        ]
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
                if needs_unaided_answer(trace, prompt):
                    # The router sent a general question into a search tool and
                    # it found nothing. The tool was the mistake — the question
                    # still deserves an answer.
                    unaided = strip_kernel_notes(self._answer_unaided(prompt, history))
                    if unaided:
                        reply = UNAIDED_NOTE + unaided
                        self.audit.append(
                            "unaided_answer",
                            {"prompt": prompt, "reason": "retrieval empty; question not corpus-scoped"},
                        )
                self.audit.append(
                    "chat" if not trace else "chat_after_tools",
                    {
                        "prompt": prompt,
                        "tools_used": [t["tool"] for t in trace],
                        "memories_recalled": len(memories),
                    },
                )
                self._archive_exchange(prompt, reply, trace)
                return self._result(reply, trace, memories, started, prompt)

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
                # Something is waiting on a human, or retrieval came back
                # empty. The model does not get to narrate either one: left to
                # itself it prints the unsaved document, or invents the spec it
                # could not find. Stream the kernel's statement of fact instead.
                held = blocked_reply(trace) or ungrounded_reply(trace, prompt)
                if held:
                    reply = held
                    for piece in held.split(" "):
                        yield {"type": "token", "text": piece + " "}
                elif needs_unaided_answer(trace, prompt):
                    unaided = strip_kernel_notes(self._answer_unaided(prompt, history))
                    reply = (UNAIDED_NOTE + unaided) if unaided else reply
                    for piece in reply.split(" "):
                        yield {"type": "token", "text": piece + " "}
                    self.audit.append(
                        "unaided_answer",
                        {"prompt": prompt, "reason": "retrieval empty; question not corpus-scoped"},
                    )
                elif not trace:
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
                yield {"type": "done", **self._result(reply.strip(), trace, memories, started, prompt)}
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

    def _answer_unaided(self, prompt: str, history: Optional[list] = None) -> str:
        """Answer a general question the router wrongly sent to a search tool.

        Small models cannot reliably decline to call a tool — llama3.2 scores
        0% on that discrimination in our own evals — so 'what is 5G?' ends up
        in the spec corpus, finds nothing, and the user gets a refusal to a
        question that never needed a document. Re-ask with no tools attached
        and answer it plainly, labelled as ungrounded so it is never mistaken
        for something the corpus supports.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": prompt})
        try:
            response = self.client.chat(
                model=self.model, messages=messages, options={"temperature": 0}
            )
        except Exception:
            return ""
        return (_get(_get(response, "message", {}), "content", "") or "").strip()

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

        # Grounding gate. If this tool answered with evidence, weak chunks are
        # removed here — before the result is handed back to the model — so it
        # cannot build an answer on material that did not clear the bar.
        retrieval = retrieval_matches(result)
        citations: list = []
        if retrieval is not None:
            result, strong = filter_weak_matches(result, retrieval)
            citations = [m["citation"] for m in strong if m.get("citation")]

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
            "retrieval": retrieval is not None,
            "citations": citations,
            "produced": _produced_content(name, args) if status == "success" else None,
        }

    def _result(
        self, reply: str, trace: list, memories: list, started: float,
        prompt: str = "",
    ) -> dict:
        return {
            # The kernel — not the model — has the last word on whether an
            # action ran and whether an answer is grounded.
            "reply": (
                blocked_reply(trace)
                or ungrounded_reply(trace, prompt)
                or (reply + sources_block(trace))
            ),
            "trace": trace,
            "memories": memories,
            "model": self.model,
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
