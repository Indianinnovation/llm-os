"""FastAPI service exposing the kernel. Every client (UI, CLI, future
desktop app) goes through this single routed entry point."""

import collections
import json
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config, modeltrust
from .approvals import ApprovalStore
from .audit import AuditLog
from .conversations import ConversationStore
from .documents import create_index
from .kernel import Kernel
from .mcp_client import MCPManager
from .memory import create_memory
from .sentinel import EgressSentinel
from .tools import default_registry
from .tools.document_tools import document_tools
from .tools.memory_tools import memory_tools

app = FastAPI(title="LLM OS Kernel", version="0.2.0")

_kernel: Kernel = None  # initialized on startup so tests can inject their own
_mcp: MCPManager = None
_sentinel: EgressSentinel = None
_docs = None
_convos: ConversationStore = None


def _verify_model_or_die(audit: AuditLog) -> None:
    """Untrusted-model gate: the active model must match its pinned
    digest. Verification outcome is always audited; a FAIL refuses to
    serve. No manifest at all is allowed (dev mode) but audited."""
    try:
        models = modeltrust.engine_models()
    except Exception as exc:
        audit.append("model_verification", {"status": "SKIP", "detail": str(exc)})
        return
    status, detail = modeltrust.verify_model(config.MODEL_NAME, models)
    audit.append("model_verification", {"status": status, "detail": detail})
    if status == modeltrust.FAIL:
        raise RuntimeError(f"Model trust verification failed: {detail}")


@app.on_event("startup")
def _startup() -> None:
    global _kernel, _mcp, _sentinel, _docs, _convos
    audit = AuditLog(config.AUDIT_DIR)
    _verify_model_or_die(audit)
    _sentinel = EgressSentinel(audit)
    _sentinel.start()
    registry = default_registry()

    memory = None
    if config.MEMORY_ENABLED:
        memory = create_memory(
            config.MEMORY_DIR, config.OLLAMA_HOST, config.EMBED_MODEL
        )
        if memory is not None:
            for tool in memory_tools(memory):
                registry.register(tool)

    if config.DOCUMENTS_ENABLED:
        _docs = create_index(
            config.DOCUMENTS_DIR, config.DOCUMENT_INDEX_DIR,
            config.OLLAMA_HOST, config.EMBED_MODEL,
        )
        if _docs is not None:
            for tool in document_tools(_docs):
                registry.register(tool)

    _mcp = MCPManager(config.MCP_CONFIG)
    _mcp.start()
    _mcp.register_tools(registry)

    # Human approval gates: a tool listed here cannot run until a person
    # approves it, no matter what the model decides.
    registry.require_approval(*config.APPROVAL_TOOLS)

    _convos = ConversationStore(config.CONVERSATIONS_DIR)
    approvals = ApprovalStore(config.APPROVALS_FILE)
    _kernel = Kernel(registry=registry, memory=memory, audit=audit,
                     approvals=approvals)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _sentinel is not None:
        _sentinel.stop()
    if _mcp is not None:
        _mcp.shutdown()


class Turn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    history: list[Turn] = Field(default_factory=list)
    # When set, the exchange is persisted and prior turns are replayed —
    # so a refresh (or a reboot) doesn't lose the conversation.
    conversation_id: str | None = None


def _history_for(request: ChatRequest) -> list:
    if request.conversation_id and _convos:
        stored = _convos.history_for(request.conversation_id, config.MAX_HISTORY_TURNS)
        if stored:
            return stored
    return [t.model_dump() for t in request.history]


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    try:
        result = _kernel.handle(request.prompt, _history_for(request))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kernel error: {exc}")
    if request.conversation_id and _convos:
        _convos.append_turn(
            request.conversation_id, request.prompt, result["reply"],
            result.get("trace"), result.get("memories"),
        )
    return result


# ── conversations: chats that survive a refresh, a restart, a reboot ────────

@app.get("/conversations")
def conversations() -> dict:
    return {"conversations": _convos.list() if _convos else []}


@app.post("/conversations")
def create_conversation() -> dict:
    if _convos is None:
        raise HTTPException(400, "Conversations are disabled.")
    return _convos.create()


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    record = _convos.get(conversation_id) if _convos else None
    if record is None:
        raise HTTPException(404, "No such conversation.")
    return record


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict:
    if _convos is None or not _convos.delete(conversation_id):
        raise HTTPException(404, "No such conversation.")
    _kernel.audit.append("conversation_deleted", {"conversation_id": conversation_id})
    return {"deleted": conversation_id}


# ── approvals: the model proposes, a human authorizes ───────────────────────

class Decision(BaseModel):
    decision: str = Field("approve", pattern="^(approve|reject)$")
    who: str = "user"


@app.get("/approvals")
def approvals() -> dict:
    store = _kernel.approvals
    return {
        "pending": store.pending() if store else [],
        "recent": store.recent() if store else [],
        "gated_tools": [
            t["name"] for t in _kernel.registry.describe() if t["requires_approval"]
        ],
    }


@app.post("/approvals/{approval_id}")
def decide_approval(approval_id: str, decision: Decision) -> dict:
    """Approve or reject a pending tool call. Approving RUNS it."""
    store = _kernel.approvals
    if store is None:
        raise HTTPException(400, "Approvals are not configured.")
    record = store.decide(approval_id, decision.decision, decision.who)
    if "error" in record:
        raise HTTPException(400, record["error"])

    _kernel.audit.append(
        "approval_decision",
        {"approval_id": approval_id, "tool": record["tool"],
         "decision": record["status"], "decided_by": decision.who},
    )
    if record["status"] != "APPROVED":
        return {"approval_id": approval_id, "status": record["status"],
                "executed": False}
    return _kernel.execute_approved(approval_id)


# ── documents: answer from the user's own files, with citations ─────────────

@app.get("/documents")
def documents() -> dict:
    if _docs is None:
        return {"enabled": False, "documents": []}
    return {
        "enabled": True,
        "folder": str(config.DOCUMENTS_DIR),
        "chunks": _docs.count(),
        "documents": _docs.documents(),
    }


@app.post("/documents/reindex")
def reindex_documents() -> dict:
    if _docs is None:
        raise HTTPException(400, "Document Q&A is disabled (needs chromadb + engine).")
    result = _docs.reindex()
    _kernel.audit.append("documents_reindexed", result)
    return result


@app.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Server-sent events: tool calls as they happen, then the answer,
    token by token. A local model is slow — show the work."""
    history = _history_for(request)

    def events():
        try:
            for event in _kernel.stream(request.prompt, history):
                if event.get("type") == "done" and request.conversation_id and _convos:
                    _convos.append_turn(
                        request.conversation_id, request.prompt, event.get("reply", ""),
                        event.get("trace"), event.get("memories"),
                    )
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health() -> dict:
    try:
        response = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=3)
        engine_ok = response.status_code == 200
        models = [m["name"] for m in response.json().get("models", [])] if engine_ok else []
    except requests.RequestException:
        engine_ok, models = False, []
    mcp_servers = sorted(
        {t["server"] for t in _mcp.discovered} if _mcp else set()
    )
    return {
        "kernel": "ok",
        "engine": "ok" if engine_ok else "unreachable",
        "engine_host": config.OLLAMA_HOST,
        "active_model": config.MODEL_NAME,
        "available_models": models,
        "mcp_servers": mcp_servers,
        "memory": {
            "enabled": _kernel is not None and _kernel.memory is not None,
            "records": _kernel.memory.count()
            if _kernel is not None and _kernel.memory is not None
            else 0,
        },
        "model_pinning": "enforced" if modeltrust.load_manifest() is not None else "unpinned",
        "egress_sentinel": _sentinel.status() if _sentinel else {"active": False},
    }


@app.get("/tools")
def tools() -> list:
    return _kernel.registry.describe()


@app.get("/audit")
def audit(n: int = 20) -> dict:
    log = _kernel.audit if _kernel else AuditLog(config.AUDIT_DIR)
    return {"chain_valid": log.verify_chain(), "records": log.tail(n)}


# ── System console: the control plane ────────────────────────────────────────

CONSOLE_HTML = Path(__file__).parent / "console" / "index.html"


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    """The System console — trust, audit, memory, tools, models."""
    return FileResponse(CONSOLE_HTML, media_type="text/html")


@app.get("/preflight")
def preflight() -> dict:
    """Live privacy/runtime posture — the same gate the launcher enforces."""
    from .preflight import run_preflight

    report = run_preflight("native")
    return {
        "ok": report.ok,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
            for c in report.checks
        ],
    }


@app.get("/audit/export", include_in_schema=False)
def audit_export() -> PlainTextResponse:
    """Download the raw hash-chained log — verifiable offline by an auditor."""
    log = _kernel.audit if _kernel else AuditLog(config.AUDIT_DIR)
    content = log.path.read_text() if log.path.exists() else ""
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": (
                f'attachment; filename="llm-os-audit-'
                f'{time.strftime("%Y%m%d")}.jsonl"'
            )
        },
    )


@app.get("/stats")
def stats() -> dict:
    """Per-tool call counts and latency, derived from the audit log."""
    log = _kernel.audit if _kernel else AuditLog(config.AUDIT_DIR)
    counts = collections.Counter()
    durations = collections.defaultdict(list)
    failures = collections.Counter()
    for record in log.tail(1000):
        if record.get("event") != "tool_execution":
            continue
        tool = record.get("tool", "?")
        counts[tool] += 1
        if record.get("status") != "success":
            failures[tool] += 1
        if isinstance(record.get("duration_ms"), (int, float)):
            durations[tool].append(record["duration_ms"])
    return {
        "tools": [
            {
                "tool": tool,
                "calls": count,
                "failures": failures[tool],
                "avg_ms": round(sum(durations[tool]) / len(durations[tool]), 1)
                if durations[tool]
                else None,
            }
            for tool, count in counts.most_common()
        ]
    }


@app.get("/models")
def models() -> dict:
    """Engine models with their pinning status."""
    try:
        engine_models = modeltrust.engine_models()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Engine unreachable: {exc}")
    manifest = modeltrust.load_manifest()
    return {
        "active": config.MODEL_NAME,
        "pinning": "enforced" if manifest is not None else "unpinned",
        "models": [
            {
                "name": m["name"],
                "digest": m["digest"],
                "pinned": manifest is not None and manifest.get(m["name"]) == m["digest"],
                "approved": manifest is not None and m["name"] in manifest,
            }
            for m in engine_models
        ],
    }


@app.get("/memory")
def memory_list(q: str = "", limit: int = 50) -> dict:
    """Browse or search everything the system remembers."""
    if _kernel is None or _kernel.memory is None:
        return {"enabled": False, "records": []}
    return {
        "enabled": True,
        "count": _kernel.memory.count(),
        "records": _kernel.memory.list_records(q, limit),
    }


@app.delete("/memory/{record_id}")
def memory_forget(record_id: str) -> dict:
    """Forget one memory."""
    if _kernel is None or _kernel.memory is None:
        raise HTTPException(status_code=400, detail="Memory is disabled.")
    if record_id == "all":
        removed = _kernel.memory.forget_all()
        _kernel.audit.append("memory_forget", {"scope": "all", "removed": removed})
        return {"forgotten": removed}
    if not _kernel.memory.forget(record_id):
        raise HTTPException(status_code=404, detail="No such memory.")
    _kernel.audit.append("memory_forget", {"scope": "one", "id": record_id})
    return {"forgotten": 1}
