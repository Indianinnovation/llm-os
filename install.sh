#!/bin/bash
# LLM OS — one-command setup.
#
#   ./install.sh          # set up everything, then start
#   ./install.sh --no-run # set up only
#
# Does: check Python + Ollama · start the engine HARDENED (loopback,
# cloud features off) · pull the models · build the venv · install deps ·
# pin the model digests · pin the MCP servers · run preflight · launch.

set -e
cd "$(dirname "$0")"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
step() { echo; echo "${B}▸ $1${N}"; }
ok()   { echo "  ${G}✓${N} $1"; }
warn() { echo "  ${Y}◦${N} $1"; }
die()  { echo "  ${R}✗ $1${N}"; exit 1; }

MODEL="${LLM_OS_MODEL:-llama3.2}"
EMBED="${LLM_OS_EMBED_MODEL:-all-minilm}"

echo "${B}🧠 LLM OS — private, local-first agentic kernel${N}"
echo "${D}   nothing you type will leave this machine${N}"

step "1/6 Prerequisites"
command -v python3 >/dev/null || die "python3 not found."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "Python 3.10+ required (found $PYV)."
ok "python $PYV"

if ! command -v ollama >/dev/null; then
  echo "  ${R}✗ Ollama not found.${N}"
  echo "     Install it (one line, no account needed):"
  echo "       ${B}curl -fsSL https://ollama.com/install.sh | sh${N}   (Linux)"
  echo "       ${B}brew install ollama${N}  or  https://ollama.com/download   (macOS)"
  exit 1
fi
ok "ollama $(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"

step "2/6 Engine (hardened: loopback-only, vendor cloud features OFF)"
if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  if ps -p "$(pgrep -f 'ollama serve' | head -1)" -wwE -o command= 2>/dev/null | grep -q "OLLAMA_NO_CLOUD=1"; then
    ok "engine already running with OLLAMA_NO_CLOUD=1"
  else
    warn "an engine is running WITHOUT OLLAMA_NO_CLOUD=1 — it can reach ollama.com"
    echo "     Restart it with:"
    echo "       ${B}pkill -f 'ollama serve' && OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 ollama serve &${N}"
  fi
else
  OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 nohup ollama serve >/tmp/llm-os-engine.log 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1
  done
  curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
    || die "engine did not start — see /tmp/llm-os-engine.log"
  ok "engine started (127.0.0.1:11434, OLLAMA_NO_CLOUD=1)"
fi

step "3/6 Models"
for m in "$MODEL" "$EMBED"; do
  if ollama list 2>/dev/null | grep -q "^${m%%:*}"; then
    ok "$m already present"
  else
    echo "  ${D}pulling $m …${N}"
    ollama pull "$m" >/dev/null && ok "$m pulled"
  fi
done

step "4/6 Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
ok "venv ready ($(.venv/bin/python -m pip list 2>/dev/null | wc -l | tr -d ' ') packages)"

step "5/6 Pin the models you trust"
.venv/bin/python scripts/launch.py --approve-models | sed 's/^/  /'
.venv/bin/python scripts/launch.py --approve-mcp   | sed 's/^/  /'

step "6/6 Preflight"
if [ "$1" = "--no-run" ]; then
  .venv/bin/python scripts/launch.py --check-only
  echo
  echo "${G}${B}Setup complete.${N}  Start it with:  ${B}python scripts/launch.py${N}"
else
  exec .venv/bin/python scripts/launch.py
fi
