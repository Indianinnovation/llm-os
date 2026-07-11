#!/bin/bash
# LLM OS — choreographed airplane-mode demo.
# Turn Wi-Fi OFF, hit record, then run:  ./scripts/demo.sh
# ~90 seconds. Every step is live, nothing is mocked.

cd "$(dirname "$0")/.." || exit 1
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; D=$'\033[2m'; R=$'\033[0m'

banner() { echo; echo "${B}${C}$1${R}"; echo "${D}──────────────────────────────────────────────────────${R}"; }

ask() {
  echo
  echo "${B}You ›${R} $1"
  $PY - "$1" <<'EOF'
import json, sys, urllib.request
req = urllib.request.Request(
    "http://localhost:8000/chat",
    data=json.dumps({"prompt": sys.argv[1]}).encode(),
    headers={"Content-Type": "application/json"},
)
d = json.loads(urllib.request.urlopen(req, timeout=300).read())
for t in d.get("trace", []):
    icon = "\033[32m⚙\033[0m" if t["status"] == "success" else "\033[33m⚠\033[0m"
    print(f"  {icon} \033[2mrouted to\033[0m \033[1m{t['tool']}\033[0m "
          f"\033[2m· audit {t.get('audit_id','-')}\033[0m")
if d.get("memories"):
    print(f"  🧠 \033[2mpaged in {len(d['memories'])} memory(ies) from previous sessions\033[0m")
reply = (d.get("reply") or "").strip()
print(f"\033[1mLLM OS ›\033[0m {reply[:300]}")
EOF
  sleep 1
}

clear
echo "${B}"
echo "   🧠  LLM OS — private AI that provably never phones home"
echo "${R}${D}       model · tools · memory · audit — all on this machine${R}"

banner "① Going dark: disable the Wi-Fi radio, prove we're OFFLINE"
WIFI_DEV=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi|AirPort/{getline; print $2; exit}')
if ping -c 1 -t 2 1.1.1.1 >/dev/null 2>&1; then
  if [ -n "$WIFI_DEV" ]; then
    echo "  ${Y}› networksetup -setairportpower $WIFI_DEV off${R}"
    networksetup -setairportpower "$WIFI_DEV" off 2>/dev/null
    sleep 4
  fi
fi
if ping -c 1 -t 2 1.1.1.1 >/dev/null 2>&1; then
  # A tethered iPhone or wired link can keep the machine online.
  echo "  ${Y}› disabling backup network services (iPhone USB, Thunderbolt Bridge)${R}"
  networksetup -setnetworkserviceenabled "iPhone USB" off 2>/dev/null
  networksetup -setnetworkserviceenabled "Thunderbolt Bridge" off 2>/dev/null
  sleep 3
fi
if ping -c 1 -t 2 1.1.1.1 >/dev/null 2>&1; then
  echo "  ${Y}◦ still ONLINE — unplug tethered phones/ethernet for true airplane mode;${R}"
  echo "  ${Y}  continuing with egress monitoring instead${R}"
else
  echo "  ${G}✈  no route to the internet — TRUE AIRPLANE MODE${R}"
fi
sleep 2

banner "② A math question → routed to a sandboxed calculator"
ask "What is 4539 multiplied by 23?"

banner "③ Generate a real document → written to a jailed sandbox"
ask "Write a markdown note called airplane-demo with title Airplane Demo, saying this file was created with no internet connection."
echo "  ${D}$(ls -la scratchpad/airplane-demo.md 2>/dev/null | awk '{print $NF, "("$5" bytes) — on disk"}')${R}"

banner "④ A plug-in MCP tool inspects the disk — locally"
ask "How much free disk space does this machine have?"

banner "⑤ Persistent memory — across completely separate sessions"
ask "Remember this: my demo codeword is falcon-blue."
ask "What is the demo codeword?"

banner "⑥ Full verification: every path + zero egress + audit chain"
sleep 1
$PY scripts/verify_airplane_mode.py

echo "${D}(recording done? restore network: networksetup -setairportpower ${WIFI_DEV:-en0} on"
echo " && networksetup -setnetworkserviceenabled \"iPhone USB\" on"
echo " && networksetup -setnetworkserviceenabled \"Thunderbolt Bridge\" on)${R}"
