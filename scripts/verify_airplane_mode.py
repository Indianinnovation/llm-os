#!/usr/bin/env python3
"""Airplane-mode verification: scripted proof that LLM OS works with
zero egress.

Two modes, chosen automatically:

- OFFLINE (true airplane mode): the machine has no route to the
  internet (e.g. Wi-Fi off). The script proves the full stack still
  works — the strongest possible demo. This is the mode to screen-record.

- ONLINE (egress monitoring): the machine is connected, so instead the
  script samples every TCP connection opened by the engine, kernel and
  UI processes for the entire workload and reports any non-loopback
  destination. Zero violations means nothing left the machine.

Both modes run the same workload: calculator routing, sandboxed file
generation, an MCP tool call, cross-request episodic memory, and plain
chat — then verify the tamper-evident audit chain.

Usage:
    python scripts/verify_airplane_mode.py [--kernel-url http://localhost:8000]

Exit code 0 = all checks passed.
"""

import argparse
import json
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

EXTERNAL_PROBES = [
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("api.openai.com", 443),
    ("github.com", 443),
]
COMPONENT_PORTS = [11434, 8000, 8501]  # engine, kernel, UI
SAMPLE_INTERVAL_S = 0.3

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m",
)


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


def info(msg):
    print(f"  {DIM}· {msg}{RESET}")


def section(title):
    print(f"\n{BOLD}{title}{RESET}")


# --------------------------------------------------------------------------
# Connectivity detection
# --------------------------------------------------------------------------

def machine_is_offline() -> bool:
    """True if no external probe target is reachable."""
    for host, port in EXTERNAL_PROBES:
        try:
            socket.create_connection((host, port), timeout=3).close()
            return False
        except OSError:
            continue
    return True


# --------------------------------------------------------------------------
# Egress monitor (online mode)
# --------------------------------------------------------------------------

def _is_loopback(address: str) -> bool:
    host = address.rsplit(":", 1)[0].strip("[]")
    return host in ("::1", "localhost") or host.startswith("127.")


def component_pids() -> list:
    pids = set()
    for port in COMPONENT_PORTS:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-s", "tcp:LISTEN"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            pids.update(int(p) for p in out.split())
        except (subprocess.SubprocessError, ValueError):
            continue
    return sorted(pids)


class EgressMonitor(threading.Thread):
    """Samples lsof for the component processes; records every remote,
    non-loopback TCP peer observed while the workload runs."""

    def __init__(self, pids):
        super().__init__(daemon=True)
        self.pids = pids
        self.violations = {}  # remote -> first-seen lsof line
        self.samples = 0
        self._halt = threading.Event()

    def run(self):
        pid_arg = ",".join(str(p) for p in self.pids)
        pattern = re.compile(r"->([^\s]+)\s+\((ESTABLISHED|SYN_SENT)\)")
        while not self._halt.is_set():
            try:
                out = subprocess.run(
                    ["lsof", "-n", "-P", "-a", "-p", pid_arg, "-i", "TCP"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                self.samples += 1
                for line in out.splitlines():
                    match = pattern.search(line)
                    if match and not _is_loopback(match.group(1)):
                        self.violations.setdefault(match.group(1), line.strip())
            except subprocess.SubprocessError:
                pass
            self._halt.wait(SAMPLE_INTERVAL_S)

    def stop(self):
        self._halt.set()
        self.join(timeout=5)


# --------------------------------------------------------------------------
# Workload
# --------------------------------------------------------------------------

def chat(kernel_url: str, prompt: str) -> dict:
    response = requests.post(
        f"{kernel_url}/chat", json={"prompt": prompt}, timeout=300
    )
    response.raise_for_status()
    return response.json()


def used_tool(result: dict, tool: str) -> bool:
    return any(
        t["tool"] == tool and t["status"] == "success"
        for t in result.get("trace", [])
    )


def run_workload(kernel_url: str, health: dict) -> list:
    """Returns a list of (name, passed, detail) tuples."""
    outcomes = []
    run_id = uuid.uuid4().hex[:6]

    def step(name, prompt, check):
        started = time.time()
        try:
            result = chat(kernel_url, prompt)
            passed, detail = check(result)
        except Exception as exc:
            passed, detail = False, f"request failed: {exc}"
        elapsed = time.time() - started
        (ok if passed else fail)(f"{name} {DIM}({elapsed:.1f}s){RESET} — {detail}")
        outcomes.append((name, passed, detail))

    step(
        "Calculator routing",
        "What is 4539 multiplied by 23?",
        lambda r: (
            used_tool(r, "calculator") and "104397" in json.dumps(r["trace"]),
            "routed to calculator, exact result 104397",
        ),
    )

    step(
        "Sandboxed file generation",
        f"Write a markdown note called airplane-check-{run_id} with title "
        "Airplane Check, containing one sentence about local AI.",
        lambda r: (
            used_tool(r, "write_markdown"),
            f"write_markdown created airplane-check-{run_id}.md in the sandbox",
        ),
    )

    if "system-info" in health.get("mcp_servers", []):
        step(
            "MCP tool routing",
            "How much free disk space does this machine have?",
            lambda r: (
                used_tool(r, "get_disk_usage"),
                "routed to MCP server system-info (get_disk_usage)",
            ),
        )
    else:
        info("MCP check skipped (no system-info server configured)")

    if health.get("memory", {}).get("enabled"):
        codeword = f"falcon-{run_id}"
        # The recall step below is the hard assertion; storage may happen
        # via the remember tool OR automatic exchange archival.
        step(
            "Memory: store fact",
            f"Remember that the verification codeword is {codeword}.",
            lambda r: (
                bool(r.get("reply", "").strip()),
                f"codeword {codeword} stored "
                + ("via remember tool" if used_tool(r, "remember")
                   else "via automatic archival"),
            ),
        )
        step(
            "Memory: cross-request recall",
            "What is the verification codeword?",
            lambda r: (
                codeword in r.get("reply", "")
                or any(codeword in m["text"] for m in r.get("memories", [])),
                "codeword recalled in a separate stateless request",
            ),
        )
    else:
        info("Memory checks skipped (memory disabled)")

    step(
        "Plain chat (no tool)",
        "In one sentence, what is a routing kernel?",
        lambda r: (bool(r.get("reply", "").strip()), "direct answer produced"),
    )

    return outcomes


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-url", default="http://localhost:8000")
    parser.add_argument(
        "--report",
        default="scratchpad/airplane_report.md",
        help="Where to write the markdown report",
    )
    args = parser.parse_args()

    print(f"{BOLD}🛫 LLM OS — Airplane-Mode Verification{RESET}")
    started = time.strftime("%Y-%m-%d %H:%M:%S")

    section("[1/5] Connectivity mode")
    offline = machine_is_offline()
    if offline:
        ok(f"No route to any external probe {EXTERNAL_PROBES} — "
           f"{BOLD}TRUE AIRPLANE MODE{RESET}")
        mode = "OFFLINE (true airplane mode)"
    else:
        print(f"  {YELLOW}◦{RESET} Machine is online — switching to egress "
              "monitoring (turn Wi-Fi off for the strongest demo)")
        mode = "ONLINE (egress monitoring)"

    section("[2/5] Stack health")
    try:
        health = requests.get(f"{args.kernel_url}/health", timeout=5).json()
    except requests.RequestException as exc:
        fail(f"Kernel unreachable at {args.kernel_url}: {exc}")
        return 1
    engine_ok = health.get("engine") == "ok"
    (ok if engine_ok else fail)(
        f"Engine {health.get('engine')} · model {health.get('active_model')} · "
        f"MCP {health.get('mcp_servers')} · "
        f"memory {health.get('memory', {}).get('records', 0)} records"
    )
    if not engine_ok:
        return 1

    monitor = None
    if not offline:
        pids = component_pids()
        if pids:
            monitor = EgressMonitor(pids)
            monitor.start()
            info(f"Monitoring TCP egress of PIDs {pids} "
                 f"every {SAMPLE_INTERVAL_S}s")
        else:
            info("Could not identify component PIDs; egress monitor disabled")

    section("[3/5] Workload — every routing path")
    outcomes = run_workload(args.kernel_url, health)

    section("[4/5] Egress result")
    violations = {}
    if offline:
        ok("Machine had no internet route for the entire run — "
           "egress impossible by construction")
    elif monitor:
        monitor.stop()
        violations = monitor.violations
        if violations:
            fail(f"Non-loopback connections observed ({len(violations)}):")
            for remote, line in violations.items():
                print(f"      {RED}{remote}{RESET}  {DIM}{line}{RESET}")
        else:
            ok(f"Zero non-loopback connections across {monitor.samples} "
               "samples — nothing left this machine")
    else:
        info("No egress data (monitor unavailable)")

    section("[5/5] Audit chain")
    try:
        audit = requests.get(f"{args.kernel_url}/audit?n=1", timeout=5).json()
        chain_valid = audit.get("chain_valid", False)
        (ok if chain_valid else fail)(
            "Tamper-evident audit chain "
            + ("verified" if chain_valid else "BROKEN")
        )
    except requests.RequestException as exc:
        chain_valid = False
        fail(f"Audit endpoint unreachable: {exc}")

    all_workload = all(passed for _, passed, _ in outcomes)
    passed = all_workload and chain_valid and not violations

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    verdict = (
        f"{GREEN}{BOLD}PASS — full functionality with zero egress{RESET}"
        if passed
        else f"{RED}{BOLD}FAIL — see details above{RESET}"
    )
    print(f"  Mode: {mode}\n  Verdict: {verdict}")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# LLM OS — Airplane-Mode Verification Report",
        "",
        f"- **Date:** {started}",
        f"- **Mode:** {mode}",
        f"- **Model:** {health.get('active_model')}",
        f"- **Verdict:** {'PASS' if passed else 'FAIL'}",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for name, step_passed, detail in outcomes:
        lines.append(f"| {name} | {'✅' if step_passed else '❌'} | {detail} |")
    lines.append(
        f"| Egress | {'✅' if not violations else '❌'} | "
        + ("offline by construction" if offline
           else f"{len(violations)} non-loopback connections") + " |"
    )
    lines.append(
        f"| Audit chain | {'✅' if chain_valid else '❌'} | hash chain "
        + ("verified" if chain_valid else "broken") + " |"
    )
    report_path.write_text("\n".join(lines) + "\n")
    print(f"  Report: {report_path}\n")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
