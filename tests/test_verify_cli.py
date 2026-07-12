"""The standalone auditor CLI. It must import nothing from llm_os — an
auditor verifying the kernel's log cannot be made to trust the kernel."""

import json
import subprocess
import sys
from pathlib import Path

from llm_os.audit import AuditLog

CLI = Path(__file__).resolve().parent.parent / "scripts" / "verify_audit.py"


def _run(path):
    return subprocess.run(
        [sys.executable, str(CLI), str(path)], capture_output=True, text=True
    )


def _log(tmp_path, n=5):
    audit = AuditLog(tmp_path)
    for i in range(n):
        audit.append("tool_execution", {"tool": "calculator", "status": "success", "i": i})
    return audit.path


def test_intact_chain_verifies(tmp_path):
    result = _run(_log(tmp_path))
    assert result.returncode == 0
    assert "CHAIN INTACT" in result.stdout


def test_edited_record_is_caught(tmp_path):
    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    record = json.loads(lines[2])
    record["status"] = "success_totally"      # rewrite history
    lines[2] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")

    result = _run(path)
    assert result.returncode == 1
    assert "TAMPERED" in result.stdout


def test_deleted_record_is_caught(tmp_path):
    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n")

    result = _run(path)
    assert result.returncode == 1
    assert "BROKEN CHAIN" in result.stdout


def test_rehashing_a_forged_record_does_not_help(tmp_path):
    """The subtle attack: edit a record AND recompute its own hash. The next
    record already committed to the old hash, so the seam still shows."""
    import hashlib

    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    record = json.loads(lines[2])
    record.pop("hash")
    record["status"] = "forged"
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    record["hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    lines[2] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")

    result = _run(path)
    assert result.returncode == 1
    assert "BROKEN CHAIN" in result.stdout   # record 4 no longer matches


def test_missing_file_is_unusable_not_intact(tmp_path):
    result = _run(tmp_path / "nope.jsonl")
    assert result.returncode == 2


def test_cli_does_not_import_the_kernel():
    """Stdlib only. An auditor can copy this one file to a clean machine and
    it must still run — no llm_os, no third-party packages."""
    import ast

    tree = ast.parse(CLI.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "llm_os" not in imported
    assert imported <= {"argparse", "hashlib", "hmac", "json", "sys", "pathlib"}
