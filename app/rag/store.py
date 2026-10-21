"""A small, dependency-free vector store.

For a knowledge base of a few hundred chunks, brute-force cosine similarity with numpy
is exact, takes well under a millisecond and has nothing to operate. The interface
(`add`, `search`, `save`, `load`) is the same you would implement on top of Chroma,
pgvector or Qdrant when the corpus grows - swapping it is a one-file change.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class Chunk:
    id: str
    text: str
    url: str
    title: str


@dataclass
class SearchHit:
    chunk: Chunk
    score: float


class VectorStore:
    def __init__(self, chunks: list[Chunk] | None = None, vectors: np.ndarray | None = None, meta: dict | None = None):
        self.chunks: list[Chunk] = chunks or []
        self.vectors = vectors if vectors is not None else np.zeros((0, 0), dtype=np.float32)
        self.meta = meta or {}

    def __len__(self) -> int:
        return len(self.chunks)

    @staticmethod
    def _normalize(m: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return m / norms

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        new = self._normalize(np.asarray(vectors, dtype=np.float32))
        self.vectors = new if self.vectors.size == 0 else np.vstack([self.vectors, new])
        self.chunks.extend(chunks)

    def scores(self, query_vector: list[float]) -> np.ndarray:
        """Cosine similarity of the query against every chunk (vectors are unit-norm)."""
        q = np.asarray(query_vector, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        return self.vectors @ q

    def search(self, query_vector: list[float], top_k: int = 5) -> list[SearchHit]:
        if not self.chunks:
            return []
        scores = self.scores(query_vector)
        order = np.argsort(-scores)[:top_k]
        return [SearchHit(self.chunks[i], float(scores[i])) for i in order]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": self.meta,
            "chunks": [asdict(c) for c in self.chunks],
            "vectors": self.vectors.round(6).tolist(),
        }
        # atomic: write a temp file, then rename - a reader (or another replica) never sees half a file
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "VectorStore":
        if path.suffix == ".gz":  # committed snapshot (data/index_snapshot.json.gz)
            import gzip
            data = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        chunks = [Chunk(**c) for c in data["chunks"]]
        vectors = np.asarray(data["vectors"], dtype=np.float32)
        return cls(chunks, vectors, data.get("meta", {}))
