"""Retrieval evaluation: does search return the right pages? Dense, hybrid and reranked modes, side by side.

    python -m evals.run_retrieval_evals          # needs the index and GEMINI_API_KEY (query embeddings)

Metrics over evals/retrieval_dataset.jsonl (page-level relevance: a chunk is relevant when its source URL
contains one of the labelled substrings):
  * Recall@K (hit rate) - share of queries with at least one relevant chunk in the top K
  * MRR@K               - mean of 1/rank of the first relevant chunk (0 if none in the top K)
  * Precision@K         - share of the returned chunks that are relevant
  * Empty rate          - share of queries where the relevance floor returned nothing

Relevance labels (hand-written by the author from the indexed pages - a small, self-labelled set):
  * relevant_url_contains - page level: the chunk's URL contains one of these substrings, OR
  * relevant_text_all     - chunk level: the chunk's text contains ALL these terms (e.g. a home-page chunk
                            that lists both machines is relevant for "Get Clássica vs Get Smart").
Written to evals/retrieval_report.md and .json.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from app.config import get_settings
from app.llm import GeminiClient
from app.observability import setup_logging
from app.rag.retriever import MODES, Retriever
from app.rag.store import VectorStore

HERE = Path(__file__).resolve().parent


def is_relevant(hit: dict, case: dict) -> bool:
    if any(lbl in hit["url"] for lbl in case["relevant_url_contains"]):
        return True
    terms = case.get("relevant_text_all") or []
    return bool(terms) and all(t.lower() in hit.get("text", "").lower() for t in terms)


def score(hits: list[dict], case: dict, k: int) -> dict:
    rel = [is_relevant(h, case) for h in hits[:k]]
    first = next((i + 1 for i, r in enumerate(rel) if r), None)
    return {"hit": first is not None, "rr": 1 / first if first else 0.0,
            "precision": (sum(rel) / len(rel)) if rel else 0.0, "empty": not hits}


def main(k: int = 5) -> None:
    settings = get_settings()
    setup_logging("WARNING")
    llm = GeminiClient(settings)
    store = VectorStore.load(settings.index_path)
    retriever = Retriever(store, llm, settings)
    cases = [json.loads(x) for x in (HERE / "retrieval_dataset.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    results = {mode: [] for mode in MODES}
    rows = []
    for case in cases:
        row = {"id": case["id"], "query": case["query"]}
        for mode in results:
            hits = retriever.search(case["query"], top_k=k, mode=mode)
            s = score(hits, case, k)
            results[mode].append(s)
            row[mode] = {**s, "top_urls": [h["url"] for h in hits[:3]]}
        rows.append(row)
        time.sleep(1.5)  # query embeddings are cheap, but stay polite with per-minute quotas

    summary = {mode: {
        f"recall@{k}": round(statistics.mean(r["hit"] for r in rs), 3),
        f"mrr@{k}": round(statistics.mean(r["rr"] for r in rs), 3),
        f"precision@{k}": round(statistics.mean(r["precision"] for r in rs), 3),
        "empty_rate": round(statistics.mean(r["empty"] for r in rs), 3),
    } for mode, rs in results.items()}
    report = {"summary": summary, "cases": rows, "chunks": len(store), "k": k,
              "generated_at": time.strftime("%Y-%m-%d %H:%M"), "embedding_model": settings.embedding_model}
    (HERE / "retrieval_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    best = max(summary, key=lambda m: (summary[m][f"mrr@{k}"], summary[m][f"recall@{k}"], -list(MODES).index(m)))
    report["best_mode"] = best
    (HERE / "retrieval_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Retrieval evaluation", "", f"{len(cases)} queries · top {k} · {len(store)} chunks · {report['generated_at']}",
             "", f"Best mode on this set (MRR, then recall): **{best}** · configured mode: **{settings.retrieval_mode}**", "",
             "| Mode | " + " | ".join(summary["dense"]) + " |", "|---|" + "---|" * len(summary["dense"])]
    for mode, m in summary.items():
        lines.append(f"| {mode} | " + " | ".join(str(v) for v in m.values()) + " |")
    lines += ["", "| Query | " + " | ".join(MODES) + " |", "|---|" + "---|" * len(MODES)]
    for r in rows:
        f = lambda s: f"rank {round(1 / s['rr'])}" if s["rr"] else "miss"  # noqa: E731
        lines.append(f"| {r['query']} | " + " | ".join(f(r[m]) for m in MODES) + " |")
    (HERE / "retrieval_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
