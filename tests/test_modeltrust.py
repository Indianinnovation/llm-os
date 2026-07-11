"""Model digest pinning tests."""

import json

import pytest

from llm_os import modeltrust
from llm_os.modeltrust import FAIL, PASS, WARN, verify_model

MODELS = [
    {"name": "llama3.2:latest", "digest": "sha256:aaa111"},
    {"name": "qwen2.5-coder:latest", "digest": "sha256:bbb222"},
]


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "model_manifest.json"
    monkeypatch.setattr(modeltrust, "MANIFEST_PATH", path)

    def write(approved):
        path.write_text(json.dumps({"approved_models": approved}))

    return write


def test_no_manifest_warns(manifest):
    status, detail = verify_model("llama3.2", MODELS)
    assert status == WARN
    assert "unpinned" in detail


def test_pinned_model_passes(manifest):
    manifest({"llama3.2:latest": "sha256:aaa111"})
    status, detail = verify_model("llama3.2", MODELS)
    assert status == PASS


def test_unapproved_model_fails(manifest):
    manifest({"llama3.2:latest": "sha256:aaa111"})
    status, detail = verify_model("qwen2.5-coder", MODELS)
    assert status == FAIL
    assert "NOT on the approved list" in detail


def test_tampered_model_fails(manifest):
    manifest({"llama3.2:latest": "sha256:DIFFERENT"})
    status, detail = verify_model("llama3.2", MODELS)
    assert status == FAIL
    assert "DIGEST MISMATCH" in detail


def test_missing_model_fails(manifest):
    manifest({"llama3.2:latest": "sha256:aaa111"})
    status, detail = verify_model("phi-4", MODELS)
    assert status == FAIL
    assert "not present in the engine" in detail
