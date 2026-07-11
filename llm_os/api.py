"""FastAPI service exposing the kernel. Every client (UI, CLI, future
desktop app) goes through this single routed entry point."""

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config
from .audit import AuditLog
from .kernel import Kernel
from .mcp_client import MCPManager
from .memory import create_memory
from .tools import default_registry
from .tools.memory_tools import memory_tools

app = FastAPI(title="LLM OS Kernel", version="0.1.0")

_kernel: Kernel = None  # initialized on startup so tests can inject their own
_mcp: MCPManager = None


@app.on_event("startup")
def _startup() -> None:
    global _kernel, _mcp
    registry = default_registry()

    memory = None
    if config.MEMORY_ENABLED:
        memory = create_memory(
            config.MEMORY_DIR, config.OLLAMA_HOST, config.EMBED_MODEL
        )
        if memory is not None:
            for tool in memory_tools(memory):
                registry.register(tool)

    _mcp = MCPManager(config.MCP_CONFIG)
    _mcp.start()
    _mcp.register_tools(registry)
    _kernel = Kernel(registry=registry, memory=memory)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _mcp is not None:
        _mcp.shutdown()


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    try:
        return _kernel.handle(request.prompt)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kernel error: {exc}")


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
    }


@app.get("/tools")
def tools() -> list:
    return _kernel.registry.describe()


@app.get("/audit")
def audit(n: int = 20) -> dict:
    log = _kernel.audit if _kernel else AuditLog(config.AUDIT_DIR)
    return {"chain_valid": log.verify_chain(), "records": log.tail(n)}
