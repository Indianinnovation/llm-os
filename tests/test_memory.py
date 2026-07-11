"""Episodic memory tests with a deterministic offline embedder."""

import hashlib
import math

import pytest

from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.memory import EpisodicMemory
from llm_os.registry import ToolRegistry
from llm_os.tools import default_registry
from llm_os.tools.memory_tools import memory_tools
from tests.test_kernel import FakeClient, text_response

DIM = 64


def fake_embedder(texts):
    """Bag-of-words hashing: texts sharing words land close in cosine space."""
    vectors = []
    for text in texts:
        vector = [0.0] * DIM
        for word in text.lower().split():
            digest = int(hashlib.md5(word.encode()).hexdigest(), 16)
            vector[digest % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        vectors.append([x / norm for x in vector])
    return vectors


@pytest.fixture
def memory(tmp_path):
    return EpisodicMemory(tmp_path / "store", embedder=fake_embedder)


def test_archive_and_recall_roundtrip(memory):
    memory.archive("The user's company is called Acme Legal.", kind="fact")
    memory.archive("Completely unrelated gardening tulip bulbs.", kind="episode")
    assert memory.count() == 2

    matches = memory.recall("what is the user's company called?")
    assert matches, "expected at least one recalled memory"
    assert "Acme Legal" in matches[0]["text"]
    assert matches[0]["kind"] == "fact"


def test_unrelated_query_recalls_nothing(memory):
    memory.archive("The user's company is called Acme Legal.")
    assert memory.recall("zebra quantum harmonica") == []


def test_empty_memory_recall(memory):
    assert memory.recall("anything") == []


def test_memory_tools_remember_and_search(memory):
    registry = ToolRegistry()
    for tool in memory_tools(memory):
        registry.register(tool)

    outcome = registry.get("remember").run(
        {"fact": "The user's favorite model is llama3.2."}
    )
    assert "stored" in outcome
    assert memory.count() == 1

    found = registry.get("search_memory").run({"query": "favorite model llama3.2"})
    assert any("llama3.2" in m["text"] for m in found["matches"])


def test_kernel_archives_and_pages_in_memories(tmp_path, memory):
    audit = AuditLog(tmp_path / "audit")

    # Exchange 1: kernel should archive the exchange automatically.
    kernel = Kernel(
        registry=default_registry(),
        client=FakeClient([text_response("Nice, Acme Legal noted.")]),
        model="fake-model",
        audit=audit,
        memory=memory,
    )
    result = kernel.handle("My company is called Acme Legal")
    assert result["memories"] == []
    assert memory.count() == 1

    # Exchange 2 (fresh client = fresh session): memory gets paged in.
    client = FakeClient([text_response("Your company is Acme Legal.")])
    kernel = Kernel(
        registry=default_registry(),
        client=client,
        model="fake-model",
        audit=audit,
        memory=memory,
    )
    result = kernel.handle("What is my company called?")
    assert len(result["memories"]) >= 1
    assert "Acme Legal" in result["memories"][0]["text"]

    sent_messages = client.calls[0]["messages"]
    assert any(
        "Possibly relevant memories" in m["content"]
        for m in sent_messages
        if m["role"] == "system"
    )


def test_kernel_without_memory_still_works(tmp_path):
    kernel = Kernel(
        registry=default_registry(),
        client=FakeClient([text_response("Hello.")]),
        model="fake-model",
        audit=AuditLog(tmp_path / "audit"),
        memory=None,
    )
    result = kernel.handle("Hi")
    assert result["reply"] == "Hello."
    assert result["memories"] == []
