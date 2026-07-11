"""Agentic memory tools — the model's explicit handle on its own memory.

Auto-recall pages memories in before routing (see kernel); these tools
let the model deliberately save durable facts and dig deeper on demand.
Built per-instance because they bind to a live EpisodicMemory.
"""

from typing import List

from pydantic import BaseModel, Field

from ..memory import EpisodicMemory
from ..registry import Tool


class RememberParams(BaseModel):
    fact: str = Field(
        ...,
        min_length=3,
        description="The exact fact to store, self-contained and specific, "
        "e.g. 'The user's company is called Acme Legal.'",
    )


class SearchMemoryParams(BaseModel):
    query: str = Field(..., min_length=2, description="What to look for in memory.")


def memory_tools(memory: EpisodicMemory) -> List[Tool]:
    def remember(fact: str) -> dict:
        record_id = memory.archive(fact, kind="fact")
        return {"stored": fact, "id": record_id}

    def search_memory(query: str) -> dict:
        memories = memory.recall(query, k=6)
        return {
            "query": query,
            "matches": [
                {"text": m["text"], "when": m["ts"], "kind": m["kind"]}
                for m in memories
            ],
        }

    return [
        Tool(
            name="remember",
            description=(
                "Permanently save an important fact to local memory. Use this "
                "whenever the user asks you to remember something, or shares a "
                "durable fact about themselves or their work."
            ),
            parameters=RememberParams,
            handler=remember,
        ),
        Tool(
            name="search_memory",
            description=(
                "Search long-term memory of past conversations and saved facts. "
                "Use when the user refers to something from a previous session."
            ),
            parameters=SearchMemoryParams,
            handler=search_memory,
        ),
    ]
