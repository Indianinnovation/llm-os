"""Loopback origin guard: binding to 127.0.0.1 stops the network, not the
user's own browser. A DNS-rebinding page (Ollama CVE-2024-28224 class)
reaches the kernel as same-origin JavaScript — these tests prove the
Host/Origin guard shuts that door, including on the approval endpoint."""

import json

import pytest
from fastapi.testclient import TestClient

from llm_os import api
from llm_os.approvals import ApprovalStore
from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.tools import default_registry
from tests.test_console_api import StubMemory
from tests.test_kernel import FakeClient, text_response


@pytest.fixture
def client(tmp_path, monkeypatch):
    audit = AuditLog(tmp_path)
    kernel = Kernel(
        registry=default_registry(),
        client=FakeClient([text_response("hi")]),
        model="fake",
        audit=audit,
        memory=StubMemory(),
        approvals=ApprovalStore(tmp_path / "approvals.json"),
    )
    monkeypatch.setattr(api, "_kernel", kernel)
    monkeypatch.setattr(api, "_mcp", None)
    monkeypatch.setattr(api, "_sentinel", None)
    return TestClient(api.app)


def test_rebound_host_is_rejected(client):
    # DNS rebinding: the browser resolves evil.com to 127.0.0.1, so the
    # request arrives on loopback — but Host still says evil.com.
    response = client.get("/health", headers={"host": "evil.com:8000"})
    assert response.status_code == 403


def test_cross_origin_request_is_rejected(client):
    response = client.get("/audit", headers={"origin": "http://evil.com"})
    assert response.status_code == 403


def test_rebound_page_cannot_click_approve(client):
    # The one that matters: the human-approval gate must not be clickable
    # by a webpage. Same-origin-after-rebinding POST → refused.
    store = api._kernel.approvals
    record = store.request(
        tool="write_markdown",
        params={"filename": "x", "content": "y"},
        prompt="write x",
    )
    response = client.post(
        f"/approvals/{record['id']}",
        json={"decision": "approve"},
        headers={"host": "evil.com:8000"},
    )
    assert response.status_code == 403
    assert store.get(record["id"])["status"] == "PENDING"


def test_blocked_request_lands_in_audit_chain(client):
    client.get("/health", headers={"host": "evil.com:8000"})
    events = [json.loads(l) for l in api._kernel.audit.path.read_text().splitlines()]
    blocked = [e for e in events if e["event"] == "blocked_request"]
    assert blocked and blocked[-1]["host"] == "evil.com:8000"


def test_local_origins_still_work(client):
    assert client.get("/health", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/health", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/health", headers={"host": "[::1]:8000"}).status_code == 200
    assert (
        client.get("/health", headers={"origin": "http://localhost:8000"}).status_code
        == 200
    )


def test_malformed_host_is_rejected(client):
    assert client.get("/health", headers={"host": ""}).status_code == 403
