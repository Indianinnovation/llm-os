"""The approval gate must not be self-satisfiable over HTTP.

Without a second factor, any caller that can POST /chat to propose a gated
tool can also POST /approvals/{id} to approve it — proposer and approver are
the same automated actor. A per-boot token, printed only to the server's
stdout and never returned over HTTP, makes proposer != approver structural:
a caller that can only reach the API cannot read the token.
"""

import pytest
from fastapi.testclient import TestClient

from llm_os import api
from llm_os.approvals import ApprovalStore
from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.tools import default_registry
from tests.test_console_api import StubMemory
from tests.test_kernel import FakeClient, text_response, tool_call_response


@pytest.fixture
def client(tmp_path, monkeypatch):
    registry = default_registry()
    registry.require_approval("write_markdown")
    monkeypatch.setattr(api, "config", api.config)
    monkeypatch.setattr(api.config, "SCRATCHPAD_DIR", tmp_path)
    kernel = Kernel(
        registry=registry,
        client=FakeClient([
            tool_call_response("write_markdown",
                               {"filename": "x", "title": "x", "content": "real content, substantive enough"}),
            text_response("done"),
        ]),
        model="fake",
        audit=AuditLog(tmp_path),
        memory=StubMemory(),
        approvals=ApprovalStore(tmp_path / "approvals.json"),
    )
    monkeypatch.setattr(api, "_kernel", kernel)
    monkeypatch.setattr(api, "_mcp", None)
    monkeypatch.setattr(api, "_sentinel", None)
    monkeypatch.setattr(api, "_approval_token", "s3cr3t42")  # as if minted at boot
    return TestClient(api.app)


def _propose(client):
    client.post("/chat", json={"prompt": "write a note"})
    return client.get("/approvals").json()["pending"][0]["id"]


def test_approve_without_token_is_refused_and_does_not_run(client, tmp_path):
    approval_id = _propose(client)
    r = client.post(f"/approvals/{approval_id}", json={"decision": "approve"})
    assert r.status_code == 403
    assert not (tmp_path / "x.md").exists()          # the tool did NOT run
    assert client.get("/approvals").json()["pending"]  # still pending


def test_approve_with_wrong_token_is_refused(client, tmp_path):
    approval_id = _propose(client)
    r = client.post(f"/approvals/{approval_id}",
                    json={"decision": "approve", "token": "guessed"})
    assert r.status_code == 403
    assert not (tmp_path / "x.md").exists()


def test_approve_with_correct_token_runs(client, tmp_path):
    approval_id = _propose(client)
    r = client.post(f"/approvals/{approval_id}",
                    json={"decision": "approve", "token": "s3cr3t42"})
    assert r.status_code == 200
    assert r.json()["executed"] is True
    assert (tmp_path / "x.md").exists()


def test_reject_needs_no_token(client, tmp_path):
    # Rejecting is always safe — it only cancels. It must not be blocked.
    approval_id = _propose(client)
    r = client.post(f"/approvals/{approval_id}", json={"decision": "reject"})
    assert r.status_code == 200
    assert not (tmp_path / "x.md").exists()


def test_the_token_never_appears_in_any_http_response(client):
    approval_id = _propose(client)
    bodies = [
        client.get("/approvals").text,
        client.get("/console").text,
        client.post(f"/approvals/{approval_id}",
                    json={"decision": "approve"}).text,  # the 403 body
    ]
    assert all("s3cr3t42" not in b for b in bodies)


def test_no_token_configured_keeps_the_old_behaviour(client, tmp_path, monkeypatch):
    # Opt-out / tests: when no token was minted, approval works as before.
    monkeypatch.setattr(api, "_approval_token", None)
    approval_id = _propose(client)
    r = client.post(f"/approvals/{approval_id}", json={"decision": "approve"})
    assert r.status_code == 200
    assert (tmp_path / "x.md").exists()
