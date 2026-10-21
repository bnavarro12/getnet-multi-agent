"""Retrieval step of the RAG pipeline: query -> top-k chunks.

Hybrid retrieval (default, RETRIEVAL_MODE=hybrid):
  1. dense ranking   - cosine similarity between query and chunk embeddings (meaning);
  2. sparse ranking  - BM25 over the chunk text (exact words: "D+1", "Get Tap", codes);
  3. fusion          - Reciprocal Rank Fusion: score = sum(1 / (60 + rank)) over both rankings.
     RRF only uses ranks, so the two very different score scales never need calibrating.

Relevance gate (anti-hallucination): a chunk is returned only if its cosine similarity clears
RETRIEVAL_MIN_SCORE - or, for chunks BM25 ranks in its top 3 (strong exact-word match), a
slightly lower floor. An unrelated question therefore still gets "nothing found".
"""

from __future__ import annotations

import threading

from app.config import Settings
from app.rag.bm25 import BM25
from app.rag.store import VectorStore

RRF_K = 60
CANDIDATES = 20
LEXICAL_FLOOR_DISCOUNT = 0.10


class Retriever:
    def __init__(self, store: VectorStore, llm, settings: Settings):
        self.llm = llm
        self.s = settings
        self._lock = threading.Lock()
        self._set(store)

    def _set(self, store: VectorStore) -> None:
        bm25 = BM25([c.text for c in store.chunks]) if len(store) else None
        with self._lock:
            self.store, self.bm25 = store, bm25

    def replace_store(self, store: VectorStore) -> None:
        """Atomic swap used by the background index refresh."""
        self._set(store)

    def search(self, query: str, top_k: int | None = None, mode: str | None = None) -> list[dict]:
        with self._lock:
            store, bm25 = self.store, self.bm25
        if not len(store):
            return []
        top_k = top_k or self.s.retrieval_top_k
        mode = mode or self.s.retrieval_mode
        [qvec] = self.llm.embed([query], is_query=True)
        dense = store.scores(qvec)
        floor = self.s.retrieval_min_score
        dense_order = sorted(range(len(store)), key=lambda i: -dense[i])[:CANDIDATES]

        if mode != "hybrid" or bm25 is None:
            picked = [(i, None) for i in dense_order[:top_k] if dense[i] >= floor]
        else:
            sparse = bm25.scores(query)
            sparse_order = [i for i in sorted(range(len(store)), key=lambda i: -sparse[i]) if sparse[i] > 0][:CANDIDATES]
            fused: dict[int, float] = {}
            for rank, i in enumerate(dense_order, 1):
                fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank)
            for rank, i in enumerate(sparse_order, 1):
                fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank)
            top_lexical = set(sparse_order[:3])

            def relevant(i: int) -> bool:
                return dense[i] >= floor or (i in top_lexical and dense[i] >= floor - LEXICAL_FLOOR_DISCOUNT)

            ranked = sorted((i for i in fused if relevant(i)), key=lambda i: -fused[i])
            picked = [(i, fused[i]) for i in ranked[:top_k]]

        out = []
        for i, rrf in picked:
            c = store.chunks[i]
            hit = {"text": c.text, "url": c.url, "title": c.title, "score": round(float(dense[i]), 3)}
            if rrf is not None:
                hit["rrf"] = round(rrf, 4)
            out.append(hit)
        return out
