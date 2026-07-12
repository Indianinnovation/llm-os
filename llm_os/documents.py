"""Document Q&A over your own files — local, cited, zero egress.

Drop files into `documents/` and the kernel can answer questions about
them with **citations** (file + chunk), never a vague summary. Embeddings
are computed by the local engine; the index is a local ChromaDB. Your
contracts, specs, and case files are read on this machine and stay here.

Supported today: .md .txt .log .csv .json .py (plain text). PDFs are
converted only if `pypdf` is installed — an optional dependency, because
the base install must stay tiny.
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("llm_os.documents")

TEXT_SUFFIXES = {".md", ".txt", ".log", ".csv", ".json", ".py", ".yaml", ".yml"}
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
MAX_FILE_BYTES = 5 * 2**20
MAX_DISTANCE = 0.75

try:
    import chromadb

    CHROMADB_AVAILABLE = True
except ImportError:  # pragma: no cover
    CHROMADB_AVAILABLE = False


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            logger.warning("Skipping %s — install pypdf to index PDFs.", path.name)
            return ""
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        except Exception as exc:
            logger.warning("Cannot read %s: %s", path.name, exc)
            return ""
    if path.suffix.lower() in TEXT_SUFFIXES:
        try:
            return path.read_text(errors="replace")
        except OSError:
            return ""
    return ""


def _chunk(text: str) -> List[str]:
    """Split on paragraph boundaries, then pack to ~CHUNK_CHARS with overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= CHUNK_CHARS:
            current += ("\n\n" if current else "") + paragraph
        else:
            if current:
                chunks.append(current)
            if len(paragraph) <= CHUNK_CHARS:
                current = paragraph
            else:  # a single huge paragraph: hard-split it
                for start in range(0, len(paragraph), CHUNK_CHARS - CHUNK_OVERLAP):
                    chunks.append(paragraph[start:start + CHUNK_CHARS])
                current = ""
    if current:
        chunks.append(current)
    return chunks


class DocumentIndex:
    """A local, citable index over a folder of the user's own documents."""

    def __init__(
        self,
        docs_dir: Path,
        index_dir: Path,
        embedder: Callable[[List[str]], List[List[float]]],
    ):
        if not CHROMADB_AVAILABLE:
            raise RuntimeError("chromadb is not installed.")
        self.docs_dir = Path(docs_dir)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        client = chromadb.PersistentClient(
            path=str(index_dir),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        self._collection = client.get_or_create_collection(
            "documents", metadata={"hnsw:space": "cosine"}
        )

    # -- indexing -----------------------------------------------------------

    def _files(self) -> List[Path]:
        return sorted(
            p for p in self.docs_dir.rglob("*")
            if p.is_file()
            and (p.suffix.lower() in TEXT_SUFFIXES or p.suffix.lower() == ".pdf")
            and p.stat().st_size <= MAX_FILE_BYTES
        )

    def reindex(self) -> dict:
        """Index new or changed files; drop chunks of files that vanished.

        Content-hashed, so re-running is cheap and idempotent.
        """
        existing = self._collection.get(include=["metadatas"])
        indexed = {}
        for record_id, meta in zip(existing["ids"], existing["metadatas"]):
            indexed.setdefault(meta.get("file", ""), set()).add(meta.get("digest", ""))

        files, added, skipped, removed = self._files(), 0, 0, 0
        present = {str(p.relative_to(self.docs_dir)) for p in files}

        # Remove chunks whose file is gone.
        stale = [
            record_id
            for record_id, meta in zip(existing["ids"], existing["metadatas"])
            if meta.get("file") not in present
        ]
        if stale:
            self._collection.delete(ids=stale)
            removed = len(stale)

        for path in files:
            name = str(path.relative_to(self.docs_dir))
            text = _read(path)
            if not text.strip():
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            if digest in indexed.get(name, set()):
                skipped += 1
                continue

            # Content changed: replace this file's chunks.
            old = self._collection.get(where={"file": name})
            if old["ids"]:
                self._collection.delete(ids=old["ids"])

            chunks = _chunk(text)
            for start in range(0, len(chunks), 32):
                batch = chunks[start:start + 32]
                self._collection.add(
                    ids=[f"{digest}-{start + i}" for i in range(len(batch))],
                    documents=batch,
                    embeddings=self.embedder(batch),
                    metadatas=[
                        {"file": name, "digest": digest, "chunk": start + i,
                         "chunks": len(chunks)}
                        for i in range(len(batch))
                    ],
                )
            added += 1

        return {
            "documents_indexed": added,
            "unchanged": skipped,
            "chunks_removed": removed,
            "total_chunks": self._collection.count(),
            "folder": str(self.docs_dir),
        }

    # -- search -------------------------------------------------------------

    def search(self, query: str, k: int = 4) -> dict:
        if self._collection.count() == 0:
            return {
                "matches": [],
                "note": f"No documents indexed. Put files in {self.docs_dir} "
                        "and run reindex.",
            }
        hits = self._collection.query(
            query_embeddings=self.embedder([query]),
            n_results=min(k, self._collection.count()),
        )
        matches = []
        for text, meta, distance in zip(
            hits["documents"][0], hits["metadatas"][0], hits["distances"][0]
        ):
            if distance <= MAX_DISTANCE:
                matches.append({
                    "citation": f"{meta['file']} (chunk {meta['chunk'] + 1}"
                                f"/{meta['chunks']})",
                    "file": meta["file"],
                    "excerpt": text[:CHUNK_CHARS],   # a whole chunk; truncating loses the answer
                    "relevance": round(1 - distance, 3),
                })
        return {"query": query, "matches": matches}

    def documents(self) -> List[dict]:
        existing = self._collection.get(include=["metadatas"])
        by_file = {}
        for meta in existing["metadatas"]:
            entry = by_file.setdefault(meta["file"], {"file": meta["file"], "chunks": 0})
            entry["chunks"] += 1
        return sorted(by_file.values(), key=lambda d: d["file"])

    def count(self) -> int:
        return self._collection.count()


def create_index(docs_dir: Path, index_dir: Path, ollama_host: str,
                 embed_model: str) -> Optional[DocumentIndex]:
    """Build the index, or None (feature off) if unavailable — never crash."""
    if not CHROMADB_AVAILABLE:
        return None
    try:
        from .memory import ollama_embedder

        embedder = ollama_embedder(ollama_host, embed_model)
        index = DocumentIndex(docs_dir, index_dir, embedder)
        embedder(["warmup"])
        logger.info("Document index ready (%d chunks).", index.count())
        return index
    except Exception as exc:
        logger.warning("Document Q&A disabled: %s", exc)
        return None
