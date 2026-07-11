# 🧠 LLM OS — a private, local-first agentic kernel

**Everything runs on your machine. Nothing leaves it.**

![Demo: the script kills the Wi-Fi radio on camera, every routing path works offline, verification passes](docs/demo.gif)

*One unedited take: the demo script disables the Wi-Fi radio on camera → **TRUE AIRPLANE MODE** → math routing, sandboxed file generation, MCP disk tools, cross-session memory — then the verification suite closes with `Mode: OFFLINE — egress impossible by construction`. Full-resolution video: [docs/demo.mp4](docs/demo.mp4). Reproduce it yourself: `./scripts/demo.sh`.*

LLM OS is an implementation of [Andrej Karpathy's LLM OS idea](https://x.com/karpathy/status/1723140519554105733) built for one uncompromising constraint: **zero egress**. A small local language model acts as the CPU — it only *routes intent*. Deterministic, sandboxed tools do the actual work, and every decision is written to a tamper-evident audit log.

> **Built on LLM OS:** [TelecomOS](https://github.com/Indianinnovation/telecomos) — zero-egress root-cause analysis for 5G networks (air-gapped NOC demo inside).

```
┌────────────────────────────────────────────────────────────────────┐
│  UI / any client            HTTP (localhost only)                  │
│      │                                                             │
│      ▼                                                             │
│  KERNEL (FastAPI)                          ┌────────────────────┐  │
│   • routes intent via native tool-calling  │ PREFLIGHT GATE     │  │
│   • validates all tool params (Pydantic)   │ telemetry off ·    │  │
│   • hash-chained audit log of every action │ loopback-only ·    │  │
│   • model digest pinning (refuses drift)   │ model pinned · …   │  │
│   • egress sentinel (watchdog, 3s)         └────────────────────┘  │
│      │                │                       │                    │
│      ▼                ▼                       ▼                    │
│  LLM ENGINE       BUILT-IN TOOLS          MCP SERVERS (stdio)      │
│  (Ollama, frozen   • calculator            • system-info           │
│   GGUF weights,      (AST whitelist)       • disk-inspector        │
│   loopback only)   • markdown writer       • any Claude-Desktop-   │
│                      (jailed dir)            format server         │
│      │             • remember /                                    │
│      ▼               search_memory                                 │
│  EPISODIC MEMORY (local ChromaDB, MemGPT-style paging)             │
└────────────────────────────────────────────────────────────────────┘
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

python scripts/launch.py
```

The launcher is a **preflight gate**: it verifies every recommended
privacy setting — no vendor update channel (desktop app not running),
engine bound to loopback with zero external connections, UI and
vector-store telemetry disabled, **model digests pinned**, valid MCP
config, models present, disk headroom — and refuses to start until critical checks pass,
printing the exact fix for each failure. `--check-only` audits without
starting; `--docker` gates and launches the container sandbox;
`--stop` shuts everything down. To start components by hand instead:

```bash
uvicorn llm_os.api:app --port 8000    # kernel
streamlit run ui/app.py               # web console
```

Open http://localhost:8501 and try:

- *"What is 4539 multiplied by 23?"* → routed to the `calculator` tool
- *"Write a markdown note called project-ideas listing 3 startup ideas"* → routed to `write_markdown`, file appears in `scratchpad/`
- *"How big is my Downloads folder?"* → routed to the `disk-inspector` MCP server
- *"Remember that my favorite city is Mumbai"* → stored; recalled in any later session
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
origin (`builtin` vs `mcp:<server>`). Two fully offline example
servers are bundled:

- **system-info** — local time, whole-disk usage, OS details
- **disk-inspector** — read-only filesystem analytics jailed to your
  home directory: folder sizes ("how big is my Downloads folder?"),
  space breakdowns, and content-hash duplicate detection — with hard
  caps on entries walked and bytes hashed, and forgiving path
  resolution ("download folder" → `~/Downloads`)

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
- **Machine offline:** true airplane mode — full functionality with no
  internet route at all. This is the demo to screen-record, and
  `./scripts/demo.sh` choreographs the whole thing: it disables the
  Wi-Fi radio itself (menu-bar toggles get undone by auto-join) and
  walks every feature on camera.

The offline detection requires **actual response bytes**, not just a
TCP handshake — local VPN agents (e.g. Cisco AnyConnect) accept
connections even with the radio off and will fool naive probes; ours
learned that the hard way. A markdown report is written to
`scratchpad/airplane_report.md`.

## Your data: verifiable guarantees

Cloud providers offer a *policy* ("we don't train on your data") that
can change. LLM OS offers a *physical property* of frozen weights
running offline — and every claim below is a command you can run, not
a promise you have to trust.

**1. The model cannot learn from your data.** Ollama runs GGUF weights
through llama.cpp in inference-only mode: no training loop, no gradient
code path, no way for a prompt to modify the model. Prove it — hash the
weights, feed them a secret, hash again:

```bash
BLOB=$(ls -S ~/.ollama/models/blobs/sha256-* | head -1)
shasum -a 256 "$BLOB"
curl -s -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"prompt": "My secret record number is MRN-778899. What is 12 times 12?"}'
shasum -a 256 "$BLOB"   # byte-for-byte identical
```

**2. There is nowhere to send your data.** Every network destination in
the codebase is loopback:

```bash
grep -rEoh "https?://[a-zA-Z0-9.-]+" llm_os/ ui/ examples/ --include="*.py" | sort -u
# http://localhost:11434
# http://localhost:8000
```

No vendor endpoint, no telemetry, no analytics. The airplane-mode
script above verifies this at the network level on every run.

This includes third-party libraries, which we audit rather than trust:
ChromaDB ships PostHog product telemetry **enabled by default** and
Streamlit gathers usage statistics **by default** — both are disabled
by policy here (`anonymized_telemetry=False` in the memory client,
`gatherUsageStats = false` in `.streamlit/config.toml`), with a
regression test so neither can silently return.

**3. Everything the system knows about you is three local folders.**

```bash
du -sh scratchpad audit memory_store   # documents · audit log · memory
rm -rf scratchpad audit memory_store   # …and now it knows nothing
```

Stored for your benefit (recall, audit), never transmitted, gone the
moment you delete them.

### Untrusted-model containment

LLM OS assumes the model itself could be wrong, tampered with, or
malicious — and contains it mechanically, so **switching models never
changes the security posture**:

- **The model can only emit tool calls, never execute.** Parameters are
  schema-validated; tools are deny-by-default (only registered ones
  exist), sandboxed, and audited.
- **Model digest pinning.** `model_manifest.json` pins the SHA-256
  digest of every approved model. At startup the kernel verifies the
  active model against its pin and **refuses to serve** on any
  mismatch — a swapped, re-tagged, or silently updated model file
  cannot run. Approving models is an explicit human action:
  `python scripts/launch.py --approve-models`.
- **Continuous egress sentinel.** A watchdog inside the kernel samples
  the TCP connections of the whole stack (kernel, MCP servers, engine)
  every few seconds; any non-loopback destination is written to the
  tamper-evident audit chain as an `egress_violation` and surfaced on
  `/health`. Nothing can leak quietly between airplane-mode runs.

### Hardened native mode: no vendor connection at all

*The caveat, found live:* the Ollama **desktop app** auto-updates, and
we caught its engine process holding a TLS connection to `ollama.com`
(verified via certificate CN) on a dev machine. It's connection
metadata, never prompt content — but a privacy-first stack shouldn't
have a live channel to any vendor. The fix is to skip the desktop app
and run the bare daemon (same binary, same models, no updater):

```bash
# 1. Quit the Ollama menu-bar app, and remove it from
#    System Settings → General → Login Items
# 2. Run the bare server, bound to loopback only:
OLLAMA_HOST=127.0.0.1:11434 ollama serve
# 3. Verify: zero non-loopback connections, before and after inference
lsof -n -P -i TCP -a -p $(pgrep -f "ollama serve") | grep -v 127.0.0.1
```

The bare daemon only touches the network when you explicitly
`ollama pull`. Optionally block the vendor domain outright
(model pulls via `registry.ollama.ai` keep working):

```bash
sudo sh -c 'echo "0.0.0.0 ollama.com www.ollama.com" >> /etc/hosts'
```

For client/production deployments, use the Docker topology — the
engine lives on an `internal: true` network with no route out, making
this entire class of connection structurally impossible.

## Routing evals

Small models route imperfectly; this repo measures it instead of hiding
it. A 40-prompt golden set (including trap prompts that mention
calculators, disks and files but need **no** tool) scores tool
selection, execution success, and exact math results:

```bash
python scripts/run_evals.py --models llama3.2,qwen2.5-coder
```

Results on this machine (2026-07-10, temperature 0):

| category | minicpm5 (1B, 0.7GB) | llama3.2 (3B, 2GB) | qwen2.5-coder (7B, 4.7GB) |
|---|---|---|---|
| calculator | 92% | 100% | 100% |
| write_markdown | 62% | 100% | 100% |
| MCP tools | 100% | 100% | 100% |
| memory | 17% | 83% | 100% |
| chat (no tool expected) | 50% | 0% | 75% |
| **overall** | **68%** | **78%** | **95%** |

Routing quality scales with parameters, but not uniformly: the 1B
model beats the 3B at knowing when *not* to call a tool, while being
far weaker at memory tools. Pick per deployment: 1B for constrained
hardware, 7B when routing precision matters.

Two kernel features came directly out of these evals: math-notation
normalization (`5^2`, `√`, `7!`, `math.` → valid expressions) and a
**correction loop** that recovers tool calls emitted as raw JSON text
by models whose chat templates lack structured tool support. The
remaining llama3.2 weakness is over-eager tool calling on general
questions — it still answers them after the wasted call (graceful
degradation), but pick a ~7B model if routing precision matters.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

72 tests: sandbox-escape attempts against the expression evaluator,
path-traversal attacks on the file tool, audit-chain tamper detection,
model digest-pinning enforcement, egress-sentinel behavior, and
telemetry-off regression guards — all with a mocked LLM, no engine
needed.

## Roadmap

- [x] MCP host: third-party tools plug in as MCP servers
- [x] Episodic memory: local vector DB with MemGPT-style paging
- [x] Airplane-mode verification script (scripted proof of zero egress)
- [x] Routing accuracy eval harness across models/quantizations
- [x] Untrusted-model containment: digest pinning + egress sentinel
- [ ] Swappable engine adapter (llama.cpp `llama-server`, vLLM) via OpenAI-compatible API
- [ ] Desktop installer (Tauri)

## License

Apache 2.0
