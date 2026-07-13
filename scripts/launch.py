#!/usr/bin/env python3
"""Guarded launcher: validates the recommended privacy settings, and
only starts LLM OS if every critical check passes.

    python scripts/launch.py                # preflight → start kernel + UI
    python scripts/launch.py --docker       # preflight → docker compose up
    python scripts/launch.py --check-only   # preflight report, start nothing
    python scripts/launch.py --stop         # stop kernel + UI

Any FAIL blocks startup and prints the exact fix. WARNs start anyway.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from llm_os.preflight import FAIL, PASS, run_preflight  # noqa: E402

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m",
)
ICONS = {"PASS": f"{GREEN}✓{RESET}", "WARN": f"{YELLOW}◦{RESET}", "FAIL": f"{RED}✗{RESET}"}


def print_report(report) -> None:
    print(f"\n{BOLD}🔒 LLM OS preflight — recommended settings{RESET}")
    for check in report.checks:
        print(f"  {ICONS[check.status]} {check.name:<32} {DIM}{check.detail}{RESET}")
        if check.status == FAIL and check.hint:
            for line in check.hint.splitlines():
                print(f"      {YELLOW}fix:{RESET} {line}" if line is check.hint.splitlines()[0]
                      else f"           {line}")
    failed = [c for c in report.checks if c.status == FAIL]
    print()
    if failed:
        print(f"  {RED}{BOLD}BLOCKED — {len(failed)} critical check(s) failed. "
              f"Fix them and run again.{RESET}\n")
    else:
        print(f"  {GREEN}{BOLD}All critical checks passed.{RESET}\n")


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_native() -> int:
    env = {**os.environ, "ANONYMIZED_TELEMETRY": "False"}
    python = sys.executable

    if _port_in_use(8000):
        try:
            requests.get("http://localhost:8000/health", timeout=3).json()
            print(f"  {DIM}kernel already running on :8000 — keeping it{RESET}")
        except Exception:
            print(f"  {RED}port 8000 is taken by another process — stop it first{RESET}")
            return 1
    else:
        subprocess.Popen(
            [python, "-m", "uvicorn", "llm_os.api:app", "--port", "8000"],
            cwd=PROJECT_ROOT, env=env, start_new_session=True,
            stdout=open(PROJECT_ROOT / ".llmos_kernel.log", "w"),
            stderr=subprocess.STDOUT,
        )
        print("  starting kernel on :8000 …")

    if _port_in_use(8501):
        print(f"  {DIM}UI already running on :8501 — keeping it{RESET}")
    else:
        subprocess.Popen(
            [python, "-m", "streamlit", "run", "ui/app.py",
             "--server.port", "8501", "--server.headless", "true"],
            cwd=PROJECT_ROOT, env=env, start_new_session=True,
            stdout=open(PROJECT_ROOT / ".llmos_ui.log", "w"),
            stderr=subprocess.STDOUT,
        )
        print("  starting UI on :8501 …")

    for _ in range(30):
        try:
            health = requests.get("http://localhost:8000/health", timeout=2).json()
            if health.get("kernel") == "ok":
                break
        except requests.RequestException:
            time.sleep(1)
    else:
        print(f"  {RED}kernel did not become healthy — see .llmos_kernel.log{RESET}")
        return 1

    print(f"\n{GREEN}{BOLD}LLM OS is up.{RESET}  UI: http://localhost:8501  ·  "
          f"API: http://localhost:8000/docs\n")
    token = _approval_token_from_log()
    if token:
        print(f"  {BOLD}🔑 Approval token: {token}{RESET}")
        print(f"  {DIM}Type it in the console to approve a gated tool "
              f"(disable with LLM_OS_APPROVAL_TOKEN=0).{RESET}\n")
    return 0


def _approval_token_from_log(log_path: Path = None) -> str:
    """Lift the per-boot approval token the kernel printed into its log, so
    the launcher can show it on the operator's actual terminal. It is read
    from a local file by the local user — never sent over HTTP."""
    log_path = log_path or (PROJECT_ROOT / ".llmos_kernel.log")
    try:
        for line in log_path.read_text().splitlines():
            if "Approval token for this session:" in line:
                return line.rsplit(":", 1)[-1].strip()
    except OSError:
        pass
    return ""


def start_docker() -> int:
    print("  starting Docker sandbox …")
    result = subprocess.run(["docker", "compose", "up", "--build", "-d"], cwd=PROJECT_ROOT)
    if result.returncode == 0:
        print(f"\n{GREEN}{BOLD}Sandbox up.{RESET}  UI: http://localhost:8501  "
              f"{DIM}(pull a model once: docker exec llm_engine ollama pull llama3.2){RESET}\n")
    return result.returncode


def stop() -> int:
    subprocess.run(["pkill", "-f", "uvicorn llm_os.api"])
    subprocess.run(["pkill", "-f", "streamlit run ui/app.py"])
    print("stopped kernel and UI (if they were running).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="launch the Docker sandbox")
    parser.add_argument("--check-only", action="store_true", help="report and exit")
    parser.add_argument("--stop", action="store_true", help="stop native kernel + UI")
    parser.add_argument("--approve-models", action="store_true",
                        help="pin the digests of all models currently in the engine")
    parser.add_argument("--approve-mcp", action="store_true",
                        help="pin the file hashes of every configured MCP server")
    args = parser.parse_args()

    if args.stop:
        return stop()

    if args.approve_models:
        from llm_os import modeltrust
        approved = modeltrust.approve_current()
        print(f"{GREEN}Pinned {len(approved)} model(s) in model_manifest.json:{RESET}")
        for name, digest in approved.items():
            print(f"  {name:<36} {DIM}{digest[:24]}…{RESET}")
        return 0

    if args.approve_mcp:
        from llm_os import config as llm_config, mcptrust
        approved = mcptrust.approve_config(llm_config.MCP_CONFIG)
        print(f"{GREEN}Pinned {len(approved)} MCP server(s) in mcp_manifest.json:{RESET}")
        for name, entry in approved.items():
            for path, digest in entry["files"].items():
                print(f"  {name:<20} {Path(path).name:<28} {DIM}{digest[:24]}…{RESET}")
        return 0

    report = run_preflight("docker" if args.docker else "native")
    print_report(report)
    if args.check_only:
        return 0 if report.ok else 1
    if not report.ok:
        return 1
    return start_docker() if args.docker else start_native()


if __name__ == "__main__":
    sys.exit(main())
