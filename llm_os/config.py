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

# Kernel API endpoint (used by the UI).
KERNEL_URL = os.environ.get("KERNEL_URL", "http://localhost:8000")

# Hard ceiling on agentic tool iterations per request.
MAX_TOOL_CALLS = int(os.environ.get("LLM_OS_MAX_TOOL_CALLS", "5"))
