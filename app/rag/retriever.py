"""Retrieval step of the RAG pipeline (query -> embedding -> top-k chunks)."""

from __future__ import annotations

from app.config import Settings
from app.rag.store import VectorStore


class Retriever:
    def __init__(self, store: VectorStore, llm, settings: Settings):
        self.store = store
        self.llm = llm
        self.s = settings

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        if not len(self.store):
            return []
        [qvec] = self.llm.embed([query], is_query=True)
        hits = self.store.search(qvec, top_k or self.s.retrieval_top_k)
        # Low-similarity chunks are worse than nothing: they invite hallucination.
        return [
            {"text": h.chunk.text, "url": h.chunk.url, "title": h.chunk.title, "score": round(h.score, 3)}
            for h in hits if h.score >= self.s.retrieval_min_score
        ]
