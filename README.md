# 🧠 LLM OS — a private, local-first agentic kernel

**Everything runs on your machine. Nothing leaves it.**

LLM OS is an implementation of [Andrej Karpathy's LLM OS idea](https://x.com/karpathy/status/1723140519554105733) built for one uncompromising constraint: **zero egress**. A small local language model acts as the CPU — it only *routes intent*. Deterministic, sandboxed tools do the actual work, and every decision is written to a tamper-evident audit log.

```
┌──────────────────────────────────────────────────────────────┐
│  UI / any client                                             │
│      │  HTTP (localhost only)                                │
│      ▼                                                       │
│  KERNEL (FastAPI)                                            │
│   • routes intent via native tool-calling                    │
│   • validates all tool params (Pydantic)                     │
│   • hash-chained audit log of every action                   │
│      │                          │                            │
│      ▼                          ▼                            │
│  LLM ENGINE (Ollama)        TOOLS (deterministic, sandboxed) │
│   internal-only network      • calculator (AST whitelist)    │
│   no route to internet       • markdown writer (jailed dir)  │
└──────────────────────────────────────────────────────────────┘
```

## Why

Regulated teams (legal, healthcare, finance) are blocked from cloud AI by
confidentiality obligations. LLM OS gives them agentic automation with:

- **Zero egress, enforced** — the model runs on a Docker network with `internal: true`; there is no route to the internet, not just a promise.
- **The LLM never executes anything** — it emits structured tool calls; the kernel validates and runs them. No `eval()`, anywhere: math goes through an AST-whitelist evaluator, file writes are jailed to a sandbox directory.
- **Tamper-evident audit** — every routing decision and tool execution is a JSONL record hash-chained to the previous one. Edit one byte of history and `GET /audit` reports the chain broken.
- **Flat cost** — no API tokens. A 3B model on a laptop is enough, because the model only routes.

## Quickstart (native, macOS/Linux)

Requires [Ollama](https://ollama.com) with a tool-calling model:

```bash
ollama pull llama3.2      # the routing model
ollama pull all-minilm    # 46 MB embedding model for episodic memory

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — the kernel
uvicorn llm_os.api:app --port 8000

# Terminal 2 — the web console
streamlit run ui/app.py
```

Open http://localhost:8501 and try:

- *"What is 4539 multiplied by 23?"* → routed to the `calculator` tool
- *"Write a markdown note called project-ideas listing 3 startup ideas"* → routed to `write_markdown`, file appears in `scratchpad/`
- *"What is an LLM OS?"* → answered directly, no tool

## Quickstart (Docker sandbox)

```bash
docker compose up --build -d
docker exec llm_engine ollama pull llama3.2
```

UI: http://localhost:8501 · Kernel API: http://localhost:8000/docs

Hard resource ceilings (4 CPU / 4 GB for the engine) prevent host memory
exhaustion. Both published ports bind to `127.0.0.1` only.

> **Apple Silicon note:** Docker cannot use the Metal GPU, so the engine
> runs CPU-only in the sandbox. For fastest local inference run Ollama
> natively (first quickstart) — the container topology is intended for
> Linux servers and VPC deployment.

## API

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Route a prompt; returns the reply plus the full tool trace |
| `GET /health` | Kernel + engine status |
| `GET /tools` | Registered tools |
| `GET /audit?n=20` | Last N audit records + chain verification result |

## Adding a tool

One module, one `TOOL` object — a name, a description, a Pydantic
parameter model, and a handler (see `llm_os/tools/calculator.py`), then
register it in `llm_os/tools/__init__.py`. Parameters are validated
before your handler runs; execution is automatically audit-logged.

## MCP: plug in any local tool server

The kernel is an **MCP host**. Drop server definitions into
`mcp_servers.json` using the same format as Claude Desktop:

```json
{
  "mcpServers": {
    "system-info": {
      "command": "python",
      "args": ["examples/system_info_server.py"]
    }
  }
}
```

On startup the kernel spawns each server over stdio, discovers its
tools, and routes to them exactly like built-ins — every call still
lands in the hash-chained audit log. `GET /tools` labels each tool's
origin (`builtin` vs `mcp:<server>`). The bundled example server
(`examples/system_info_server.py`) is fully offline: local time, disk
usage, system info.

It works in reverse too: `python -m llm_os.mcp_server` exposes the LLM
OS built-in tools (sandboxed calculator, jailed markdown writer) to any
other MCP host, such as Claude Desktop.

## Episodic memory (MemGPT-style paging)

If the context window is RAM, the local vector store is disk. Every
exchange is archived into a persistent ChromaDB collection under
`memory_store/`, embedded by the local engine (`all-minilm`) — no
external calls. Before routing a new prompt, the kernel **pages in**
the most relevant memories (cosine-filtered) as context, so facts
survive across sessions and restarts:

```
You  › Remember that my company is called Acme Legal.        (session 1)
You  › What is my company called?                            (session 2)
LLM OS › Your company is called Acme Legal.   🧠 paged in 1 memory
```

The model also gets two agentic memory tools: `remember` (save a
durable fact) and `search_memory` (explicit lookup). Disable memory
entirely with `LLM_OS_MEMORY=0`.

## Prove it: airplane-mode verification

"Zero egress" is a claim; this script is the proof:

```bash
python scripts/verify_airplane_mode.py
```

It exercises every routing path (calculator, sandboxed file writes, MCP
tools, cross-request memory, plain chat), verifies the audit hash
chain, and proves nothing left the machine — two ways:

- **Machine online:** samples every TCP connection opened by the
  engine, kernel and UI processes for the entire run and fails on any
  non-loopback destination.
- **Machine offline (turn Wi-Fi off):** true airplane mode — full
  functionality with no internet route at all. This is the demo to
  screen-record.

A markdown report is written to `scratchpad/airplane_report.md`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite includes sandbox-escape attempts against the expression
evaluator, path-traversal attempts against the file tool, and audit
chain tamper detection — all with a mocked LLM, no engine needed.

## Roadmap

- [x] MCP host: third-party tools plug in as MCP servers
- [x] Episodic memory: local vector DB with MemGPT-style paging
- [x] Airplane-mode verification script (scripted proof of zero egress)
- [ ] Routing accuracy eval harness across models/quantizations
- [ ] Desktop installer (Tauri)

## License

Apache 2.0
