"""Preflight validation: verify the recommended privacy and runtime
settings BEFORE the kernel or Docker session is allowed to start.

Every guarantee in the README depends on configuration that can drift
(the Ollama desktop app relaunching from Login Items, a config edit
re-enabling telemetry, the engine binding to 0.0.0.0). This module
turns each one into a startup gate: FAIL blocks launch, WARN informs.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import requests

from . import config, modeltrust, sentinel

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


@dataclass
class PreflightReport:
    checks: List[Check] = field(default_factory=list)

    def add(self, name, status, detail, hint=""):
        self.checks.append(Check(name, status, detail, hint))

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)


def _engine_tags():
    response = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=3)
    response.raise_for_status()
    return [m["name"] for m in response.json().get("models", [])]


def _listener_addresses(port: int) -> List[str]:
    out = subprocess.run(
        ["lsof", "-n", "-P", "-i", f"TCP:{port}", "-s", "TCP:LISTEN"],
        capture_output=True, text=True, timeout=10,
    ).stdout
    return [line.split()[-2].rsplit(":", 1)[0] for line in out.splitlines()[1:]]


def _external_connections(pattern: str) -> List[str]:
    pids = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    ).stdout.split()
    if not pids:
        return []
    out = subprocess.run(
        ["lsof", "-n", "-P", "-i", "TCP", "-a", "-p", ",".join(pids)],
        capture_output=True, text=True, timeout=10,
    ).stdout
    remotes = []
    for line in out.splitlines():
        if "->" in line:
            host = line.split("->")[1].split()[0].rsplit(":", 1)[0].strip("[]")
            if not sentinel.is_loopback_host(host):
                remotes.append(host)
    return remotes


def check_engine(report: PreflightReport) -> list:
    try:
        tags = _engine_tags()
    except requests.RequestException:
        report.add(
            "Engine reachable", FAIL,
            f"No Ollama at {config.OLLAMA_HOST}",
            "Start it loopback-only: OLLAMA_HOST=127.0.0.1:11434 ollama serve",
        )
        return []
    report.add("Engine reachable", PASS, f"{config.OLLAMA_HOST} · {len(tags)} models")
    return tags


def check_models(report: PreflightReport, tags: list) -> None:
    if any(t.startswith(config.MODEL_NAME) for t in tags):
        report.add("Routing model", PASS, config.MODEL_NAME)
    else:
        report.add(
            "Routing model", FAIL,
            f"'{config.MODEL_NAME}' not found in engine",
            f"ollama pull {config.MODEL_NAME}",
        )
    if any(t.startswith(config.EMBED_MODEL) for t in tags):
        report.add("Embedding model (memory)", PASS, config.EMBED_MODEL)
    else:
        report.add(
            "Embedding model (memory)", WARN,
            f"'{config.EMBED_MODEL}' missing — episodic memory will be disabled",
            f"ollama pull {config.EMBED_MODEL}",
        )


def check_model_integrity(report: PreflightReport) -> None:
    try:
        models = modeltrust.engine_models()
    except requests.RequestException:
        return  # engine check already reported the failure
    status, detail = modeltrust.verify_model(config.MODEL_NAME, models)
    hint = ""
    if status != PASS:
        hint = "Approve current models deliberately: python scripts/launch.py --approve-models"
    report.add("Model digest pinned", status, detail, hint)


def check_desktop_app(report: PreflightReport) -> None:
    if sys.platform != "darwin":
        return
    out = subprocess.run(
        ["pgrep", "-f", "Ollama.app/Contents/MacOS/Ollama"],
        capture_output=True, text=True,
    ).stdout.strip()
    if out:
        report.add(
            "No vendor update channel", FAIL,
            "Ollama DESKTOP APP is running (it auto-updates via ollama.com)",
            "Quit the menu-bar app and remove it from Login Items; "
            "run the bare daemon instead: OLLAMA_HOST=127.0.0.1:11434 ollama serve",
        )
    else:
        report.add("No vendor update channel", PASS, "bare ollama daemon (no desktop app)")


def check_engine_loopback(report: PreflightReport) -> None:
    try:
        addresses = _listener_addresses(11434)
    except (subprocess.SubprocessError, OSError):
        report.add("Engine bound to loopback", WARN, "could not inspect listener")
        return
    exposed = [a for a in addresses if a in ("*", "0.0.0.0", "::")]
    if exposed:
        report.add(
            "Engine bound to loopback", FAIL,
            f"engine listens on {exposed} — reachable from your network",
            "Restart it with OLLAMA_HOST=127.0.0.1:11434",
        )
    elif addresses:
        report.add("Engine bound to loopback", PASS, ", ".join(sorted(set(addresses))))


def check_egress_monitoring(report: PreflightReport) -> None:
    """The continuous egress sentinel needs pgrep + lsof. Where they are
    absent it cannot watch anything — the trust page must say so, not imply
    the machine is clean when it is merely blind."""
    from . import sentinel

    if sentinel.monitoring_available():
        report.add("Egress monitoring", PASS, "pgrep + lsof present")
    else:
        report.add(
            "Egress monitoring", FAIL,
            "pgrep/lsof missing — egress cannot be watched on this platform",
            "Install lsof and procps, or run in the Docker sandbox",
        )


def check_engine_egress(report: PreflightReport) -> None:
    remotes = _external_connections("ollama serve")
    if remotes:
        report.add(
            "Engine egress", WARN,
            f"engine has live external connection(s): {sorted(set(remotes))}",
            "Expected only during an explicit 'ollama pull'",
        )
    else:
        report.add("Engine egress", PASS, "zero non-loopback connections")


def check_engine_cloud_disabled(report: PreflightReport) -> None:
    """Ollama ships cloud/remote features ON by default — even the bare
    daemon reaches ollama.com. The engine must run with OLLAMA_NO_CLOUD=1."""
    pids = subprocess.run(
        ["pgrep", "-f", "ollama serve"], capture_output=True, text=True
    ).stdout.split()
    if not pids:
        return  # engine check already covers a missing daemon
    for pid in pids:
        env = subprocess.run(
            ["ps", "-p", pid, "-wwE", "-o", "command="],
            capture_output=True, text=True,
        ).stdout
        if "OLLAMA_NO_CLOUD=1" in env or "OLLAMA_NO_CLOUD=true" in env:
            report.add("Engine cloud features off", PASS, "OLLAMA_NO_CLOUD=1")
            return
    report.add(
        "Engine cloud features off", FAIL,
        "engine is running WITHOUT OLLAMA_NO_CLOUD=1 — it can reach ollama.com",
        "Restart it: OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 ollama serve",
    )


def check_streamlit_telemetry(report: PreflightReport, config_path: Path) -> None:
    try:
        text = config_path.read_text()
    except OSError:
        text = ""
    normalized = text.replace(" ", "").lower()
    if "gatherusagestats=false" in normalized:
        report.add("UI telemetry disabled", PASS, "gatherUsageStats = false")
    else:
        report.add(
            "UI telemetry disabled", FAIL,
            f"gatherUsageStats not disabled in {config_path}",
            'Add to .streamlit/config.toml:\n  [browser]\n  gatherUsageStats = false',
        )


def check_memory_telemetry(report: PreflightReport) -> None:
    source = (Path(__file__).parent / "memory.py").read_text()
    if "anonymized_telemetry=False" in source:
        report.add("Vector-store telemetry disabled", PASS, "anonymized_telemetry=False")
    else:
        report.add(
            "Vector-store telemetry disabled", FAIL,
            "memory client no longer disables ChromaDB telemetry",
            "Construct PersistentClient with Settings(anonymized_telemetry=False)",
        )


def check_mcp_config(report: PreflightReport) -> None:
    path = config.MCP_CONFIG
    if not path.exists():
        report.add("MCP config", WARN, f"{path} missing — built-in tools only")
        return
    try:
        servers = json.loads(path.read_text()).get("mcpServers", {})
    except (json.JSONDecodeError, OSError) as exc:
        report.add("MCP config", FAIL, f"unreadable: {exc}", f"Fix JSON in {path}")
        return
    missing = [
        name for name, spec in servers.items()
        if spec.get("command") != "python" and not shutil.which(spec.get("command", ""))
    ]
    if missing:
        report.add(
            "MCP config", FAIL,
            f"command not found for server(s): {missing}",
            f"Fix 'command' in {path}",
        )
    else:
        report.add("MCP config", PASS, f"{len(servers)} server(s): {list(servers)}")


def check_mcp_pinning(report: PreflightReport) -> None:
    """Every configured MCP server must match its pinned file hashes —
    the same posture as model digest pinning, applied to the executables
    the kernel spawns."""
    from . import mcptrust

    path = config.MCP_CONFIG
    if not path.exists():
        report.add("MCP pinning", PASS, "no MCP config — nothing to pin")
        return
    try:
        servers = json.loads(path.read_text()).get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return  # unreadable config is already a FAIL in 'MCP config'
    if not servers:
        report.add("MCP pinning", PASS, "no servers configured")
        return
    verdicts = {name: mcptrust.verify_server(name, spec) for name, spec in servers.items()}
    failed = [detail for status, detail in verdicts.values() if status == mcptrust.FAIL]
    unpinned = [name for name, (status, _) in verdicts.items() if status == mcptrust.WARN]
    if failed:
        report.add(
            "MCP pinning", FAIL,
            "; ".join(failed),
            "Inspect the server, then re-approve: python scripts/launch.py --approve-mcp",
        )
    elif unpinned:
        report.add(
            "MCP pinning", WARN,
            f"{len(unpinned)} server(s) unpinned: {unpinned}",
            "Pin them: python scripts/launch.py --approve-mcp",
        )
    else:
        report.add("MCP pinning", PASS, f"{len(verdicts)} server(s) match pinned hashes")


def check_disk_space(report: PreflightReport, min_free_gb: float = 5.0) -> None:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / 2**30
    status = PASS if free_gb >= min_free_gb else WARN
    report.add(
        "Disk space", status, f"{free_gb:.1f} GB free",
        "" if status == PASS else "Models and memory need headroom; free some space",
    )


def check_docker(report: PreflightReport) -> None:
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        report.add("Docker daemon", FAIL, "not running", "Start Docker Desktop")
        return
    report.add("Docker daemon", PASS, f"v{probe.stdout.strip()}")
    compose = Path(config.BASE_DIR) / "docker-compose.yml"
    if compose.exists() and "internal: true" in compose.read_text():
        report.add("Zero-egress network", PASS, "engine network is internal: true")
    else:
        report.add(
            "Zero-egress network", FAIL,
            "compose file lacks an internal-only engine network",
            "The llm-engine service must sit on a network with 'internal: true'",
        )


def _guard(report: PreflightReport, name: str, fn):
    """Run one check; a missing platform tool (pgrep/lsof/ps on Windows) or
    any unexpected error degrades to a WARN instead of tracebacking out of
    the whole preflight. Checks add their own PASS/FAIL on success and only
    reach here on a hard failure, so there is no duplicate entry."""
    try:
        return fn()
    except FileNotFoundError as exc:
        report.add(name, WARN, f"skipped — a required tool is not available here ({exc})",
                   "Uses POSIX tools (pgrep/lsof/ps) absent on this platform; "
                   "run in the Docker sandbox for full verification")
    except Exception as exc:
        report.add(name, WARN, f"check could not complete: {exc}")
    return None


def run_preflight(mode: str = "native") -> PreflightReport:
    report = PreflightReport()
    if mode == "docker":
        _guard(report, "Docker daemon", lambda: check_docker(report))
        _guard(report, "Disk space", lambda: check_disk_space(report))
        return report

    tags = _guard(report, "Engine reachable", lambda: check_engine(report))
    if tags:
        _guard(report, "Routing model", lambda: check_models(report, tags))
        _guard(report, "Model integrity", lambda: check_model_integrity(report))
    _guard(report, "Update channel", lambda: check_desktop_app(report))
    _guard(report, "Engine loopback", lambda: check_engine_loopback(report))
    _guard(report, "Engine cloud features off", lambda: check_engine_cloud_disabled(report))
    _guard(report, "Egress monitoring", lambda: check_egress_monitoring(report))
    _guard(report, "Engine egress", lambda: check_engine_egress(report))
    _guard(report, "UI telemetry disabled",
           lambda: check_streamlit_telemetry(report, Path(config.BASE_DIR) / ".streamlit" / "config.toml"))
    _guard(report, "Vector-store telemetry disabled", lambda: check_memory_telemetry(report))
    _guard(report, "MCP config", lambda: check_mcp_config(report))
    _guard(report, "MCP pinning", lambda: check_mcp_pinning(report))
    _guard(report, "Disk space", lambda: check_disk_space(report))
    return report
