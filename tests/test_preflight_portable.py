"""Preflight and the launcher must not traceback on a platform without the
POSIX tools they use (pgrep/lsof/ps/pkill). CI runs pytest on Windows, but
never the launcher — so a Windows dev who installs from the README and runs
`python scripts/launch.py` was hitting an uncaught FileNotFoundError at the
very first step. Each check now degrades to a clear WARN instead.
"""

import subprocess

from llm_os import preflight


def test_run_preflight_survives_missing_posix_tools(monkeypatch):
    # Simulate Windows: no pgrep/lsof/ps, and no engine reachable.
    def no_tools(*args, **kwargs):
        raise FileNotFoundError(args[0][0] if args and args[0] else "tool")

    monkeypatch.setattr(subprocess, "run", no_tools)
    monkeypatch.setattr(preflight, "check_engine", lambda report: [])

    # Must return a report, not raise.
    report = preflight.run_preflight("native")
    assert report is not None
    # The subprocess-dependent checks degraded rather than crashing.
    statuses = {entry.name: entry.status for entry in report.checks}
    assert statuses  # something was reported
    assert all(s in (preflight.PASS, preflight.WARN, preflight.FAIL)
               for s in statuses.values())


def test_docker_mode_also_survives_missing_tools(monkeypatch):
    def no_tools(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", no_tools)
    report = preflight.run_preflight("docker")
    assert report is not None  # no traceback


def test_launcher_stop_does_not_crash_without_pkill(monkeypatch):
    import importlib
    launch = importlib.import_module("scripts.launch")

    monkeypatch.setattr(launch.shutil, "which", lambda name: None)  # no pkill
    # Must return cleanly with a helpful message, not raise.
    assert launch.stop() in (0, 1)
