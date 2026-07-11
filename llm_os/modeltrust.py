"""Model trust: digest pinning for an untrusted-model posture.

The kernel treats every model as untrusted code-shaped data. This
module pins the exact content digests of models a human has approved
(`model_manifest.json`); at startup the active model must match its
pinned digest or the kernel refuses to run. This catches swapped,
re-tagged, or tampered model files — including a registry silently
shipping different weights under the same tag.

Approval is an explicit ceremony:  python scripts/launch.py --approve-models
"""

import json
from typing import List, Optional, Tuple

import requests

from . import config

MANIFEST_PATH = config.BASE_DIR / "model_manifest.json"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def engine_models() -> List[dict]:
    """[{name, digest}, ...] from the local engine."""
    response = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
    response.raise_for_status()
    return [
        {"name": m["name"], "digest": m.get("digest", "")}
        for m in response.json().get("models", [])
    ]


def load_manifest() -> Optional[dict]:
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text()).get("approved_models", {})
    except (json.JSONDecodeError, OSError):
        return {}


def approve_current() -> dict:
    """Pin every model currently in the engine. Explicit human action."""
    approved = {m["name"]: m["digest"] for m in engine_models()}
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "_comment": "Models approved to run. Digests are pinned; "
                "re-approve deliberately after any model update.",
                "approved_models": approved,
            },
            indent=2,
        )
        + "\n"
    )
    return approved


def _find(models: List[dict], name: str) -> Optional[dict]:
    for m in models:
        if m["name"] == name or m["name"] == f"{name}:latest":
            return m
    return None


def verify_model(model_name: str, models: List[dict]) -> Tuple[str, str]:
    """Verify the active model against the pinned manifest."""
    manifest = load_manifest()
    entry = _find(models, model_name)
    if entry is None:
        return FAIL, f"model '{model_name}' not present in the engine"
    if manifest is None:
        return WARN, (
            "no model_manifest.json — model digests are unpinned "
            "(run: python scripts/launch.py --approve-models)"
        )
    pinned = manifest.get(entry["name"])
    if pinned is None:
        return FAIL, (
            f"model '{entry['name']}' is NOT on the approved list "
            f"({MANIFEST_PATH.name})"
        )
    if pinned != entry["digest"]:
        return FAIL, (
            f"DIGEST MISMATCH for '{entry['name']}': the model file changed "
            f"since approval (pinned {pinned[:20]}…, engine has "
            f"{entry['digest'][:20]}…)"
        )
    return PASS, f"'{entry['name']}' matches pinned digest {pinned[:20]}…"
