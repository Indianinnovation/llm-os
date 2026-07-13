"""FastAPI service exposing the kernel. Every client (UI, CLI, future
desktop app) goes through this single routed entry point."""

import collections
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from . import config, modeltrust
from .approvals import ApprovalStore
from .audit import AuditLog
from .conversations import ConversationStore
from .documents import create_index
from .kernel import Kernel
from .mcp_client import MCPManager
from .memory import create_memory
from .scheduler import ScheduleStore, Scheduler
from .sentinel import EgressSentinel
from .tools import default_registry
from .tools.document_tools import document_tools
from .tools.memory_tools import memory_tools

app = FastAPI(title="LLM OS Kernel", version="0.2.0")

# Loopback-only is a network property; it does not protect against the
# user's own browser. A malicious webpage can DNS-rebind its domain to
# 127.0.0.1 (the Ollama CVE-2024-28224 class) and its JavaScript becomes
# same-origin with this kernel — able to read memory and documents, and
# to POST /approvals to click Approve itself. Rebinding cannot forge the
# Host header and cross-origin JS cannot forge Origin, so both are
# checked here. "testserver" is FastAPI's TestClient default; a dotless
# name can never be public DNS.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "testserver"}


def _is_local(netloc_or_origin: str) -> bool:
    try:
        value = netloc_or_origin.strip()
        if "//" not in value:
            value = "//" + value
        return urlsplit(value).hostname in _LOCAL_HOSTNAMES
    except ValueError:
        return False


@app.middleware("http")
async def _loopback_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if not _is_local(host) or (origin is not None and not _is_local(origin)):
        try:
            if _kernel is not None:
                _kernel.audit.append(
                    "blocked_request",
                    {
                        "reason": "non-local Host or Origin",
                        "host": host[:200],
                        "origin": (origin or "")[:200],
                        "path": request.url.path,
                    },
                )
        except Exception:
            pass  # the block must hold even if auditing fails
        return JSONResponse(
            status_code=403,
            content={"detail": "Rejected: request did not come from this machine's own loopback origin."},
        )
    return await call_next(request)


_kernel: Kernel = None  # initialized on startup so tests can inject their own
_mcp: MCPManager = None
_sentinel: EgressSentinel = None
_docs = None
_convos: ConversationStore = None
_schedules: ScheduleStore = None
_scheduler: Scheduler = None
# A second factor for approving a gated tool, minted at boot and printed only
# to the server's stdout — never returned over HTTP. It makes the approver a
# different actor from the proposer: a caller that can only reach the API
# cannot read it. None = not required (tests, or LLM_OS_APPROVAL_TOKEN=0).
_approval_token: str = None


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


def _startup() -> None:
    global _kernel, _mcp, _sentinel, _docs, _convos, _schedules, _scheduler
    global _approval_token
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
    # Supply-chain verdicts land in the same chain as everything else:
    # a refused (drifted) server is evidence, not just a log line.
    for server_name, verdict in _mcp.trust_report.items():
        audit.append("mcp_verification", {"server": server_name, **verdict})

    # Human approval gates: a tool listed here cannot run until a person
    # approves it, no matter what the model decides.
    registry.require_approval(*config.APPROVAL_TOOLS)

    _convos = ConversationStore(config.CONVERSATIONS_DIR)
    approvals = ApprovalStore(config.APPROVALS_FILE)
    _kernel = Kernel(registry=registry, memory=memory, audit=audit,
                     approvals=approvals)

    # Second factor for approving a gated tool (see _approval_token). Printed
    # here to stdout only; a legitimate operator reads it from the terminal
    # that launched the kernel, an HTTP-only caller never sees it.
    if config.APPROVAL_TOKEN_REQUIRED and config.APPROVAL_TOOLS:
        _approval_token = secrets.token_hex(4)
        print(f"\n  🔑 Approval token for this session: {_approval_token}\n"
              f"     Enter it in the console to approve a gated tool. "
              f"(disable: LLM_OS_APPROVAL_TOKEN=0)\n", flush=True)

    # Scheduled agents: the OS working while you sleep. Jobs run through
    # this same kernel, so gated tools still need a human — a 3am job
    # cannot authorize itself.
    globals()["_schedules"] = ScheduleStore(config.SCHEDULES_FILE)
    globals()["_scheduler"] = Scheduler(_schedules, _kernel, audit)
    if config.SCHEDULER_ENABLED:
        _scheduler.start()


def _shutdown() -> None:
    if _scheduler is not None:
        _scheduler.stop()
    if _sentinel is not None:
        _sentinel.stop()
    if _mcp is not None:
        _mcp.shutdown()


@asynccontextmanager
async def _lifespan(app):
    _startup()
    yield
    _shutdown()


app.router.lifespan_context = _lifespan


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
    token: str = ""


@app.get("/approvals")
def approvals() -> dict:
    store = _kernel.approvals
    return {
        "pending": store.pending() if store else [],
        "recent": store.recent() if store else [],
        "gated_tools": [
            t["name"] for t in _kernel.registry.describe() if t["requires_approval"]
        ],
        "token_required": _approval_token is not None,
    }


@app.post("/approvals/{approval_id}")
def decide_approval(approval_id: str, decision: Decision) -> dict:
    """Approve or reject a pending tool call. Approving RUNS it."""
    store = _kernel.approvals
    if store is None:
        raise HTTPException(400, "Approvals are not configured.")

    # A gated tool RUNS on approval, so approving needs the out-of-band token
    # (rejecting is always safe and does not). Checked before decide() so a
    # tokenless caller cannot even move the request out of PENDING.
    if decision.decision == "approve" and _approval_token is not None:
        if not hmac.compare_digest(decision.token, _approval_token):
            _kernel.audit.append(
                "approval_denied",
                {"approval_id": approval_id, "reason": "missing or wrong token"},
            )
            raise HTTPException(403, "Approval requires the session token printed "
                                     "to the server console.")

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


# ── scheduled agents: work that happens while you sleep ─────────────────────

class NewSchedule(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    prompt: str = Field(..., min_length=3, max_length=2000)
    every_minutes: int = Field(0, ge=0, le=10080)
    daily_at: str = Field("", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")


@app.get("/schedules")
def schedules() -> dict:
    return {
        "enabled": config.SCHEDULER_ENABLED,
        "schedules": _schedules.list() if _schedules else [],
    }


@app.post("/schedules")
def create_schedule(request: NewSchedule) -> dict:
    if _schedules is None:
        raise HTTPException(400, "Scheduler is not configured.")
    if not request.every_minutes and not request.daily_at:
        raise HTTPException(400, "Give a cadence: every_minutes or daily_at (HH:MM).")
    schedule = _schedules.create(
        request.name, request.prompt, request.every_minutes, request.daily_at
    )
    _kernel.audit.append("schedule_created", {
        "job_id": schedule["id"], "name": schedule["name"],
        "prompt": schedule["prompt"], "next_run": schedule["next_run"],
    })
    return schedule


@app.post("/schedules/{job_id}/run")
def run_schedule_now(job_id: str) -> dict:
    if _scheduler is None:
        raise HTTPException(400, "Scheduler is not configured.")
    result = _scheduler.run_job(job_id, trigger="manual")
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.post("/schedules/{job_id}/toggle")
def toggle_schedule(job_id: str) -> dict:
    schedule = _schedules.get(job_id) if _schedules else None
    if schedule is None:
        raise HTTPException(404, "No such schedule.")
    updated = _schedules.update(job_id, enabled=not schedule["enabled"])
    _kernel.audit.append("schedule_toggled",
                         {"job_id": job_id, "enabled": updated["enabled"]})
    return updated


@app.delete("/schedules/{job_id}")
def delete_schedule(job_id: str) -> dict:
    if _schedules is None or not _schedules.delete(job_id):
        raise HTTPException(404, "No such schedule.")
    _kernel.audit.append("schedule_deleted", {"job_id": job_id})
    return {"deleted": job_id}


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
def audit(n: int = Query(20, ge=1, le=1000)) -> dict:
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
def memory_list(q: str = "", limit: int = Query(50, ge=1, le=1000)) -> dict:
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
