"""Central configuration, driven by environment variables so the same
code runs natively on a laptop and inside the Docker sandbox."""

import os
from pathlib import Path

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL_NAME = os.environ.get("LLM_OS_MODEL", "llama3.2")

# All tool file I/O is confined to the scratchpad; audit logs are
# written outside it so tools can never touch their own audit trail.
BASE_DIR = Path(os.environ.get("LLM_OS_HOME", Path(__file__).resolve().parent.parent))
SCRATCHPAD_DIR = Path(os.environ.get("SCRATCHPAD_DIR", BASE_DIR / "scratchpad"))
AUDIT_DIR = Path(os.environ.get("AUDIT_DIR", BASE_DIR / "audit"))

# Episodic memory (local vector store + Ollama embeddings).
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", BASE_DIR / "memory_store"))
EMBED_MODEL = os.environ.get("LLM_OS_EMBED_MODEL", "all-minilm")
MEMORY_ENABLED = os.environ.get("LLM_OS_MEMORY", "1") == "1"

# Document Q&A: drop files here; answers cite them. Nothing is uploaded.
DOCUMENTS_DIR = Path(os.environ.get("DOCUMENTS_DIR", BASE_DIR / "documents"))
DOCUMENT_INDEX_DIR = Path(os.environ.get("DOCUMENT_INDEX", BASE_DIR / "document_index"))
DOCUMENTS_ENABLED = os.environ.get("LLM_OS_DOCUMENTS", "1") == "1"

# Conversations (persisted chats) and human approval gates.
CONVERSATIONS_DIR = Path(os.environ.get("CONVERSATIONS_DIR", BASE_DIR / "conversations"))
APPROVALS_FILE = Path(os.environ.get("APPROVALS_FILE", BASE_DIR / "approvals.json"))

# Scheduled agents — jobs the kernel runs on its own (LLM_OS_SCHEDULER=0 to disable).
SCHEDULES_FILE = Path(os.environ.get("SCHEDULES_FILE", BASE_DIR / "schedules.json"))
SCHEDULER_ENABLED = os.environ.get("LLM_OS_SCHEDULER", "1") == "1"

# Tools that may not run until a human approves them. Comma-separated.
#
# Gated BY DEFAULT: every built-in that changes something outside the
# kernel. "The model proposes, a human authorizes" has to be the state a
# fresh install is already in — a guarantee you must remember to switch
# on is not a guarantee. Add your own side-effecting tools here (an MCP
# server's execute_remediation, send_email, …):
#   LLM_OS_APPROVAL_TOOLS=write_markdown,execute_remediation
# Ungating is possible but deliberate: LLM_OS_APPROVAL_TOOLS=""
DEFAULT_APPROVAL_TOOLS = "write_markdown"
APPROVAL_TOOLS = [
    t.strip()
    for t in os.environ.get("LLM_OS_APPROVAL_TOOLS", DEFAULT_APPROVAL_TOOLS).split(",")
    if t.strip()
]

# Approving a gated tool requires a per-boot token printed to the server's
# stdout, so the caller that PROPOSED an action (over HTTP) cannot also
# APPROVE it. Default on; LLM_OS_APPROVAL_TOKEN=0 for a pure single-user box.
APPROVAL_TOKEN_REQUIRED = os.environ.get("LLM_OS_APPROVAL_TOKEN", "1") != "0"

# Retrieval grounding: a floor, not a filter.
#
# A single threshold cannot referee heterogeneous corpora — measured on this
# machine, a legitimate NDA answer scores 0.421 while a *worthless* spec
# chunk scored 0.434. The bad match outranks the good one, so any number
# that blocks the second also blocks the first. Relevance calibration
# belongs to the tool that owns the corpus and its embeddings.
#
# What the kernel keeps is a floor against obvious noise, and the rules that
# do not depend on a number at all: no matches means no answer, and every
# answer built on matches carries their citations.
MIN_RELEVANCE = float(os.environ.get("LLM_OS_MIN_RELEVANCE", "0.25"))

# What the audit log retains. "redacted" (default) stores an HMAC commitment
# to every prompt, document body and excerpt instead of the words themselves:
# the chain still proves what happened and that nothing was altered, and a
# specific prompt can still be PROVEN to have produced a record — but the log
# holds no readable content, and destroying audit/.salt makes it permanently
# unlinkable (crypto-erasure). Set to "plaintext" for full forensic detail.
AUDIT_CONTENT = os.environ.get("LLM_OS_AUDIT_CONTENT", "redacted")

# MCP server definitions, Claude-Desktop-compatible {"mcpServers": {...}}.
MCP_CONFIG = Path(os.environ.get("MCP_CONFIG", BASE_DIR / "mcp_servers.json"))

# Kernel API endpoint (used by the UI).
KERNEL_URL = os.environ.get("KERNEL_URL", "http://localhost:8000")

# Hard ceiling on agentic tool iterations per request.
MAX_TOOL_CALLS = int(os.environ.get("LLM_OS_MAX_TOOL_CALLS", "5"))

# Recent conversation turns replayed for follow-ups ("and cell 5?").
MAX_HISTORY_TURNS = int(os.environ.get("LLM_OS_HISTORY_TURNS", "6"))
