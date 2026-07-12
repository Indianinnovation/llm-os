"""Shared test isolation.

The MCP trust manifest lives at the repo root on a real install; tests
must never read the developer's actual pins (or refuse to spawn their
fixtures because of them). Point the module at a session-temp path that
no test writes unless it explicitly re-patches it.
"""

import pytest
from _pytest.monkeypatch import MonkeyPatch

from llm_os import mcptrust


@pytest.fixture(scope="session", autouse=True)
def _isolated_mcp_manifest(tmp_path_factory):
    patch = MonkeyPatch()
    patch.setattr(
        mcptrust,
        "MANIFEST_PATH",
        tmp_path_factory.mktemp("trust") / "mcp_manifest.json",
    )
    yield
    patch.undo()
