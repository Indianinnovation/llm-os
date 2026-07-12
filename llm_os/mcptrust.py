"""MCP server trust: supply-chain pinning for tool servers.

Model weights are digest-pinned (modeltrust); MCP servers deserve the
same suspicion — each one is an arbitrary executable the kernel spawns
and hands a stdio channel. This module pins the SHA-256 of a server's
resolved command binary and of every file named in its args
(`mcp_manifest.json`); at startup a server that fails verification is
NOT spawned. That catches a swapped binary, an edited server script,
or a config that quietly grew a new server.

Approval is an explicit ceremony:  python scripts/launch.py --approve-mcp
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import config

MANIFEST_PATH = config.BASE_DIR / "mcp_manifest.json"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_command(command: str) -> Optional[Path]:
    # Mirror the runtime convenience in mcp_client: bare "python" means
    # the interpreter running the kernel.
    if command == "python":
        return Path(sys.executable).resolve()
    found = shutil.which(command)
    if found:
        return Path(found).resolve()
    path = Path(command)
    return path.resolve() if path.is_file() else None


def pin_server(spec: dict) -> Dict[str, str]:
    """{absolute path: sha256} for the command binary plus every arg
    that names an existing file. Flags and module names are skipped —
    only content that will actually execute gets pinned."""
    pins: Dict[str, str] = {}
    command = _resolve_command(spec.get("command", ""))
    if command is not None:
        pins[str(command)] = _sha256(command)
    for arg in spec.get("args", []):
        path = Path(arg)
        if path.is_file():
            resolved = path.resolve()
            pins[str(resolved)] = _sha256(resolved)
    return pins


def load_manifest(manifest_path: Path = None) -> Optional[dict]:
    path = manifest_path or MANIFEST_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("approved_servers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def approve_config(config_path: Path, manifest_path: Path = None) -> dict:
    """Pin every server in the MCP config. Explicit human action."""
    try:
        servers = json.loads(Path(config_path).read_text()).get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        servers = {}
    approved = {name: {"files": pin_server(spec)} for name, spec in servers.items()}
    (manifest_path or MANIFEST_PATH).write_text(
        json.dumps(
            {
                "_comment": "MCP servers approved to run. File hashes are "
                "pinned; re-approve deliberately after any server update.",
                "approved_servers": approved,
            },
            indent=2,
        )
        + "\n"
    )
    return approved


def verify_server(
    name: str, spec: dict, manifest_path: Path = None
) -> Tuple[str, str]:
    """Verify one configured server against the pinned manifest."""
    manifest = load_manifest(manifest_path)
    if manifest is None:
        return WARN, (
            "no mcp_manifest.json — MCP servers are unpinned "
            "(run: python scripts/launch.py --approve-mcp)"
        )
    entry = manifest.get(name)
    if entry is None:
        return FAIL, (
            f"MCP server '{name}' is NOT on the approved list "
            f"({(manifest_path or MANIFEST_PATH).name})"
        )
    pinned = entry.get("files", {})
    try:
        current = pin_server(spec)
    except OSError as exc:
        return FAIL, f"'{name}': cannot hash server files ({exc})"
    if set(current) != set(pinned):
        added = set(current) - set(pinned)
        gone = set(pinned) - set(current)
        parts = []
        if added:
            parts.append(f"new: {', '.join(Path(p).name for p in sorted(added))}")
        if gone:
            parts.append(f"missing: {', '.join(Path(p).name for p in sorted(gone))}")
        return FAIL, f"'{name}': the file set changed since approval ({'; '.join(parts)})"
    for path, digest in pinned.items():
        if current[path] != digest:
            return FAIL, (
                f"PIN MISMATCH for '{name}': {Path(path).name} changed since "
                f"approval (pinned {digest[:20]}…, on disk {current[path][:20]}…)"
            )
    return PASS, f"'{name}' matches {len(pinned)} pinned file hash(es)"
