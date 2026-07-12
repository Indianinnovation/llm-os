"""Episodic memory: a local vector store with MemGPT-style paging.

The context window is RAM; this module is the disk. Every exchange is
archived into a persistent ChromaDB collection on this machine, and
before routing a new prompt the kernel "pages in" the most relevant
memories as context. Embeddings are computed by the local Ollama engine
(`all-minilm`), so memory adds no external dependency and no egress.

Two kinds of records:
- kind="episode": automatic archive of each conversation exchange
- kind="fact":    explicitly saved via the `remember` tool
"""

import logging
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("llm_os.memory")

# Cosine distance (1 - similarity); recalls above this are discarded.
DEFAULT_MAX_DISTANCE = 0.7
DEFAULT_RECALL_K = 4

try:
    import chromadb

    CHROMADB_AVAILABLE = True
except ImportError:  # pragma: no cover
    CHROMADB_AVAILABLE = False


def ollama_embedder(host: str, model: str) -> Callable[[List[str]], List[List[float]]]:
    from ollama import Client

    client = Client(host=host)

    def embed(texts: List[str]) -> List[List[float]]:
        return list(client.embed(model=model, input=texts).embeddings)

    return embed


class EpisodicMemory:
    """Wrapper around a persistent, local-only vector collection.

    `embedder` is injectable so tests run without an engine; embeddings
    are computed explicitly (not via Chroma's embedding_function) to
    keep the persisted collection independent of client-side config.
    """

    def __init__(
        self,
        persist_dir: Path,
        embedder: Callable[[List[str]], List[List[float]]],
    ):
        if not CHROMADB_AVAILABLE:
            raise RuntimeError("chromadb is not installed.")
        self.embedder = embedder
        # ChromaDB ships product telemetry (PostHog) enabled by default.
        # In this environment it happens to be inert (the posthog package
        # is absent), but we disable it by policy, not by accident.
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name="episodic_memory", metadata={"hnsw:space": "cosine"}
        )

    def archive(self, text: str, kind: str = "episode") -> str:
        record_id = uuid.uuid4().hex[:16]
        self._collection.add(
            ids=[record_id],
            documents=[text],
            embeddings=self.embedder([text]),
            metadatas=[{"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}],
        )
        return record_id

    def recall(
        self,
        query: str,
        k: int = DEFAULT_RECALL_K,
        max_distance: float = DEFAULT_MAX_DISTANCE,
    ) -> List[dict]:
        if self.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=self.embedder([query]),
            n_results=min(k, self.count()),
        )
        memories = []
        for text, meta, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            if distance <= max_distance:
                memories.append(
                    {
                        "text": text,
                        "kind": meta.get("kind", "episode"),
                        "ts": meta.get("ts", ""),
                        "distance": round(distance, 3),
                    }
                )
        return memories

    def count(self) -> int:
        return self._collection.count()

    def list_records(self, query: str = "", limit: int = 50) -> List[dict]:
        """Browse memory. With a query, semantic search; without, most recent."""
        if self.count() == 0:
            return []
        if query:
            found = self.recall(query, k=limit, max_distance=1.0)
            return found
        result = self._collection.get(
            limit=limit, include=["documents", "metadatas"]
        )
        records = [
            {
                "id": record_id,
                "text": text,
                "kind": (meta or {}).get("kind", "episode"),
                "ts": (meta or {}).get("ts", ""),
            }
            for record_id, text, meta in zip(
                result["ids"], result["documents"], result["metadatas"]
            )
        ]
        records.sort(key=lambda r: r["ts"], reverse=True)
        return records

    def forget(self, record_id: str) -> bool:
        """Delete one memory. The privacy promise, made operable."""
        existing = self._collection.get(ids=[record_id])
        if not existing["ids"]:
            return False
        self._collection.delete(ids=[record_id])
        return True

    def forget_all(self) -> int:
        """Erase every memory."""
        total = self.count()
        if total:
            all_ids = self._collection.get(include=[])["ids"]
            self._collection.delete(ids=all_ids)
        return total


def create_memory(
    persist_dir: Path, ollama_host: str, embed_model: str
) -> Optional[EpisodicMemory]:
    """Build the default memory, or None (memory disabled) if the vector
    store or embedding model is unavailable — the kernel must still run."""
    if not CHROMADB_AVAILABLE:
        logger.warning("chromadb not installed; episodic memory disabled.")
        return None
    try:
        embedder = ollama_embedder(ollama_host, embed_model)
        memory = EpisodicMemory(Path(persist_dir), embedder)
        embedder(["warmup"])  # fail fast if the embed model is missing
        logger.info("Episodic memory ready (%d records).", memory.count())
        return memory
    except Exception as exc:
        logger.warning("Episodic memory disabled: %s", exc)
        return None
