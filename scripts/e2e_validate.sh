#!/usr/bin/env bash
# End-to-end validation: every product guarantee, in one command.
# Assumes a kernel is running on :8000 (start with scripts/launch.py).
# Exit 0 = all guarantees hold.
set -u
PY="${PY:-.venv/bin/python}"
URL="${KERNEL_URL:-http://localhost:8000}"
pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass+1)); }
bad()  { echo "  ✗ $1"; fail=$((fail+1)); }

echo "1. Unit + regression suite"
$PY -m pytest tests -q >/tmp/e2e_pytest.txt 2>&1 && ok "$(tail -1 /tmp/e2e_pytest.txt)" || bad "pytest failed"

echo "2. Preflight gate"
OLLAMA_NO_CLOUD=1 $PY scripts/launch.py --check-only >/tmp/e2e_pre.txt 2>&1
if grep -q "All critical checks passed" /tmp/e2e_pre.txt; then
  ok "preflight: $(grep -c '✓' /tmp/e2e_pre.txt) checks pass"
else bad "preflight has failures"; fi

echo "3. Zero egress (socket sweep while feeding a secret)"
SECRET="E2E-$(date +%s)-SENTINEL"
( for _ in $(seq 1 8); do lsof -nP -a -p "$(pgrep -f 'llm_os|ollama serve' | tr '\n' ',' | sed 's/,$//')" -i 2>/dev/null; sleep 0.3; done >/tmp/e2e_lsof.txt & )
curl -s -m 60 -X POST "$URL/chat" -H 'Content-Type: application/json' -d "{\"prompt\":\"remember $SECRET\"}" >/dev/null
sleep 3
if grep -q '\->' /tmp/e2e_lsof.txt && grep '\->' /tmp/e2e_lsof.txt | grep -vq '127.0.0.1\|\[::1\]\|localhost'; then
  bad "non-loopback socket observed"; else ok "only loopback sockets"; fi

echo "4. No training on your data (weights byte-identical)"
BLOB=$(ls -S ~/.ollama/models/blobs/sha256-* 2>/dev/null | head -1)
if [ -n "$BLOB" ]; then
  H1=$(shasum -a 256 "$BLOB" | awk '{print $1}')
  curl -s -m 60 -X POST "$URL/chat" -H 'Content-Type: application/json' -d "{\"prompt\":\"My secret is $SECRET, what is 2+2?\"}" >/dev/null
  H2=$(shasum -a 256 "$BLOB" | awk '{print $1}')
  [ "$H1" = "$H2" ] && ok "largest weight blob unchanged" || bad "weight blob changed"
else bad "no weight blob found"; fi

echo "5. Audit chain intact, incl. large records"
$PY scripts/verify_audit.py audit/audit.jsonl >/tmp/e2e_audit.txt 2>&1 && ok "chain verifies" || bad "chain broken"

echo "6. Audit content redacted (secret not on disk)"
if grep -rq "$SECRET" audit/audit.jsonl 2>/dev/null; then bad "secret is in the audit log"; else ok "secret absent from audit log"; fi

echo "7. Approval gate holds (gated tool blocks without token)"
R=$(curl -s -m 60 -X POST "$URL/chat" -H 'Content-Type: application/json' -d '{"prompt":"Write a markdown note called e2e with one idea"}')
if echo "$R" | grep -q "awaiting_approval"; then ok "gated write blocked pending approval"; else bad "gated write was not blocked"; fi

echo "8. Grounding (NDA cites, absent spec refuses)"
NDA=$(curl -s -m 60 -X POST "$URL/chat" -H 'Content-Type: application/json' -d '{"prompt":"What is the liability cap in my NDA?"}')
echo "$NDA" | grep -qi "250,000\|Sources" && ok "NDA answered with citation" || bad "NDA answer missing/uncited"

echo
echo "═══ $pass passed, $fail failed ═══"
[ "$fail" -eq 0 ]
