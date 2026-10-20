"""Offline evaluation of the multi-agent system against a labelled dataset (real LLM).

    python -m evals.run_evals                # inside the container or a venv with GEMINI_API_KEY
    python -m evals.run_evals --judge        # also score groundedness/helpfulness with an LLM judge

What it measures per case and in aggregate:
  * routing accuracy        - did the Router pick the expected agent?
  * tool selection          - were the expected tools called?
  * content check           - does the answer mention at least one expected key term?
  * LLM-as-judge (optional) - groundedness (no invented facts vs. tool outputs) and helpfulness, 1-5
  * latency and tokens      - operational cost of each scenario

The report is written to evals/report.md and evals/report.json. Run it before every prompt or
model change and compare with the previous report (regression testing for LLM behaviour).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from app.config import get_settings
from app.main import build_orchestrator
from app.observability import setup_logging

HERE = Path(__file__).resolve().parent

JUDGE_PROMPT = """You are grading a customer-support assistant for a payments company (Getnet).
Given the user question, the evidence the assistant retrieved (tool outputs) and its answer, score:
- groundedness (1-5): 5 = every factual claim is supported by the evidence or is generic advice; 1 = invents facts.
- helpfulness (1-5): 5 = directly and fully addresses the question in the user's language; 1 = useless.
Return JSON only."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"groundedness": {"type": "integer"}, "helpfulness": {"type": "integer"}, "comment": {"type": "string"}},
    "required": ["groundedness", "helpfulness", "comment"],
}


def actual_route(result: dict) -> str:
    if result["guardrails"].get("blocked") and not result["route"]:
        return "blocked"
    return result["route"].get("route", "error")


def judge(llm, case: dict, result: dict) -> dict:
    evidence = [s.get("result", "") for s in result["trace"]["steps"] if s["step"].startswith("tool:")]
    prompt = (f"Question: {case['message']}\n\nEvidence:\n{json.dumps(evidence, ensure_ascii=False)[:6000]}\n\n"
              f"Answer:\n{result['response']}")
    resp = llm.chat(system=JUDGE_PROMPT, history=[llm.user_message(prompt)], json_schema=JUDGE_SCHEMA)
    return json.loads(resp.text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", action="store_true", help="score answers with an LLM judge")
    parser.add_argument("--delay", type=float, default=8.0, help="seconds between cases (free-tier rate limits)")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("WARNING")
    orch = build_orchestrator(settings)
    cases = [json.loads(line) for line in (HERE / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    rows = []
    for case in cases:
        result = orch.handle(case["message"], case["user_id"])
        route = actual_route(result)
        tools_ok = all(t in result["tools_used"] for t in case["expected_tools"])
        text = result["response"].lower()
        content_ok = any(term.lower() in text for term in case["must_mention_any"])
        row = {
            "id": case["id"], "message": case["message"], "expected_route": case["expected_route"], "route": route,
            "route_ok": route == case["expected_route"], "tools": result["tools_used"], "tools_ok": tools_ok,
            "content_ok": content_ok, "escalated": result["escalated"], "latency_ms": result["trace"]["total_ms"],
            "tokens": result["trace"]["tokens"], "response": result["response"],
        }
        if args.judge and route not in ("blocked", "out_of_scope"):
            try:
                row["judge"] = judge(orch.services.llm, case, result)
            except Exception as exc:
                row["judge"] = {"error": str(exc)[:100]}
        rows.append(row)
        status = "PASS" if row["route_ok"] and tools_ok and content_ok else "FAIL"
        print(f"[{status}] {case['id']:<7} route={route:<12} tools={result['tools_used']} {row['latency_ms']:.0f}ms")
        time.sleep(args.delay)

    n = len(rows)
    summary = {
        "cases": n,
        "routing_accuracy": round(sum(r["route_ok"] for r in rows) / n, 3),
        "tool_selection_accuracy": round(sum(r["tools_ok"] for r in rows) / n, 3),
        "content_check_rate": round(sum(r["content_ok"] for r in rows) / n, 3),
        "latency_p50_ms": round(statistics.median(r["latency_ms"] for r in rows)),
        "latency_max_ms": round(max(r["latency_ms"] for r in rows)),
        "avg_tokens": round(statistics.mean(r["tokens"]["input"] + r["tokens"]["output"] for r in rows)),
    }
    judged = [r["judge"] for r in rows if "groundedness" in r.get("judge", {})]
    if judged:
        summary["avg_groundedness"] = round(statistics.mean(j["groundedness"] for j in judged), 2)
        summary["avg_helpfulness"] = round(statistics.mean(j["helpfulness"] for j in judged), 2)

    (HERE / "report.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2, ensure_ascii=False))
    lines = ["# Evaluation report", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += ["", "| Case | Route (expected) | Tools | Route | Tools | Content | Latency |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        ok = lambda b: "✅" if b else "❌"  # noqa: E731
        lines.append(f"| {r['id']} | {r['route']} ({r['expected_route']}) | {', '.join(r['tools']) or '-'} | "
                     f"{ok(r['route_ok'])} | {ok(r['tools_ok'])} | {ok(r['content_ok'])} | {r['latency_ms']:.0f} ms |")
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
