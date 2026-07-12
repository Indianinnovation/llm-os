"""System console API: stats, memory browse/forget, console page."""

import json

import pytest
from fastapi.testclient import TestClient

from llm_os import api
from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.tools import default_registry
from tests.test_kernel import FakeClient, text_response
from tests.test_memory import fake_embedder


class StubMemory:
    def __init__(self):
        self.records = [
            {"id": "a1", "text": "The user's company is Acme Legal.", "kind": "fact", "ts": "2026-07-11 10:00:00"},
            {"id": "b2", "text": "User asked about disk usage.", "kind": "episode", "ts": "2026-07-11 09:00:00"},
        ]

    def count(self):
        return len(self.records)

    def list_records(self, query="", limit=50):
        if query:
            return [r for r in self.records if query.lower() in r["text"].lower()][:limit]
        return self.records[:limit]

    def forget(self, record_id):
        before = len(self.records)
        self.records = [r for r in self.records if r["id"] != record_id]
        return len(self.records) < before

    def forget_all(self):
        removed = len(self.records)
        self.records = []
        return removed

    def recall(self, *a, **k):
        return []

    def archive(self, *a, **k):
        return "x"


@pytest.fixture
def client(tmp_path, monkeypatch):
    audit = AuditLog(tmp_path)
    # A couple of tool executions so /stats has something to aggregate.
    audit.append("tool_execution", {"tool": "calculator", "status": "success", "duration_ms": 2.0})
    audit.append("tool_execution", {"tool": "calculator", "status": "success", "duration_ms": 4.0})
    audit.append("tool_execution", {"tool": "write_markdown", "status": "tool_error", "duration_ms": 1.0})

    kernel = Kernel(
        registry=default_registry(),
        client=FakeClient([text_response("hi")]),
        model="fake",
        audit=audit,
        memory=StubMemory(),
    )
    monkeypatch.setattr(api, "_kernel", kernel)
    monkeypatch.setattr(api, "_mcp", None)
    monkeypatch.setattr(api, "_sentinel", None)
    return TestClient(api.app)


def test_console_page_served(client):
    response = client.get("/console")
    assert response.status_code == 200
    assert "System Console" in response.text
    assert "Trust posture" in response.text


def test_stats_aggregates_tool_usage(client):
    stats = client.get("/stats").json()["tools"]
    by_tool = {t["tool"]: t for t in stats}
    assert by_tool["calculator"]["calls"] == 2
    assert by_tool["calculator"]["avg_ms"] == 3.0
    assert by_tool["calculator"]["failures"] == 0
    assert by_tool["write_markdown"]["failures"] == 1


def test_memory_browse_and_search(client):
    listing = client.get("/memory").json()
    assert listing["enabled"] is True
    assert listing["count"] == 2

    found = client.get("/memory?q=acme").json()["records"]
    assert len(found) == 1
    assert "Acme" in found[0]["text"]


def test_memory_forget_one_is_audited(client):
    assert client.delete("/memory/a1").json() == {"forgotten": 1}
    assert client.get("/memory").json()["count"] == 1
    events = [r["event"] for r in client.get("/audit?n=10").json()["records"]]
    assert "memory_forget" in events


def test_memory_forget_unknown_404(client):
    assert client.delete("/memory/nope").status_code == 404


def test_memory_forget_all(client):
    assert client.delete("/memory/all").json() == {"forgotten": 2}
    assert client.get("/memory").json()["count"] == 0


def test_audit_export_is_ndjson(client):
    response = client.get("/audit/export")
    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "tool_execution"
    assert "attachment" in response.headers["content-disposition"]
