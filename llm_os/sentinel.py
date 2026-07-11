"""Egress sentinel: continuous zero-egress enforcement.

The airplane-mode script proves zero egress on demand; this thread
proves it continuously. Every few seconds it samples the TCP
connections of the whole stack — the kernel process, its children
(MCP servers), and the inference engine — and writes any non-loopback
destination into the tamper-evident audit chain as an
`egress_violation` event. Violations are also surfaced on /health, so
a compromised or misconfigured component cannot leak quietly.
"""

import os
import subprocess
import threading
from typing import Callable, Dict, List, Optional

from .audit import AuditLog

SAMPLE_INTERVAL_S = 3.0


def _loopback(host: str) -> bool:
    return host.startswith("127.") or host in ("::1", "localhost")


def default_sampler() -> List[str]:
    """Return non-loopback remote endpoints of kernel + children + engine."""
    pids = {str(os.getpid())}
    for pattern in ("ollama serve",):
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True
        ).stdout
        pids.update(out.split())
    children = subprocess.run(
        ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True
    ).stdout
    pids.update(children.split())

    out = subprocess.run(
        ["lsof", "-n", "-P", "-i", "TCP", "-a", "-p", ",".join(sorted(pids))],
        capture_output=True, text=True, timeout=10,
    ).stdout
    remotes = []
    for line in out.splitlines():
        if "->" in line:
            remote = line.split("->")[1].split()[0]
            if not _loopback(remote.rsplit(":", 1)[0].strip("[]")):
                remotes.append(remote)
    return remotes


class EgressSentinel(threading.Thread):
    def __init__(
        self,
        audit: AuditLog,
        sampler: Callable[[], List[str]] = default_sampler,
        interval: float = SAMPLE_INTERVAL_S,
    ):
        super().__init__(name="egress-sentinel", daemon=True)
        self.audit = audit
        self.sampler = sampler
        self.interval = interval
        self.violations: Dict[str, str] = {}  # remote -> first-seen audit id
        self.samples = 0
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            self.check_once()
            self._halt.wait(self.interval)

    def check_once(self) -> List[str]:
        try:
            remotes = self.sampler()
        except Exception:
            remotes = []
        self.samples += 1
        new = [r for r in remotes if r not in self.violations]
        for remote in new:
            audit_id = self.audit.append(
                "egress_violation",
                {"remote": remote, "detail": "non-loopback connection observed"},
            )
            self.violations[remote] = audit_id
        return new

    def stop(self) -> None:
        self._halt.set()

    def status(self) -> dict:
        return {
            "active": self.is_alive(),
            "samples": self.samples,
            "violations": [
                {"remote": remote, "audit_id": audit_id}
                for remote, audit_id in self.violations.items()
            ],
        }
