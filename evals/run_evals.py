"""Offline evaluation of the multi-agent system against a labelled dataset (real LLM).

    python -m evals.run_evals                # inside the container or a venv with GEMINI_API_KEY
    python -m evals.run_evals --judge        # also score groundedness/helpfulness with an LLM judge

What it measures per case and in aggregate:
  * routing accuracy        - did the Router pick the expected route (and, if labelled, the expected agents)?
  * tool selection          - were the expected tools called? For cases that expect NO tools (refusals,
                              blocked messages, hand-offs) any tool call is a failure.
  * content check           - does the answer mention at least one expected key term?
  * LLM-as-judge (optional) - groundedness (no invented facts vs. tool outputs) and helpfulness, 1-5.
                              The judge uses JUDGE_MODEL, a different model from the agents' one.
Multi-turn cases ("turns") first send the earlier messages in the same session (conversation memory).
  * latency and tokens      - operational cost of each scenario

The report is written to evals/report.md, evals/report.json and storage/eval_report.json
(shown on the dashboard). Run it before every prompt or
model change and compare with the previous report (regression testing for LLM behaviour).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.main import build_orchestrator
from app.observability import setup_logging

HERE = Path(__file__).resolve().parent

JUDGE_PROMPT = """You are grading a customer-support assistant for a payments company (Getnet).
Given the user question, the evidence the assistant retrieved (tool outputs) and its answer, score:
- groundedness (1-5): 5 = every factual claim is supported by the evidence or is generic advice; 1 = invents facts.
  Judge only claims of fact (fees, dates, amounts, product features, procedures). Today's date, polite phrasing and
  generic safety advice ("contact your bank") are not unsupported claims.
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


def judge(llm, case: dict, result: dict, model: str | None = None) -> dict:
    # Full tool outputs (not the 300-character summaries kept in the trace for logging),
    # so the judge sees everything the agent saw.
    evidence = result.get("evidence", [])
    prompt = (f"Today's date: {date.today().isoformat()}\n\nQuestion: {case['message']}\n\nEvidence:\n{json.dumps(evidence, ensure_ascii=False, default=str)[:30000]}\n\n"
              f"Answer:\n{result['response']}")
    resp = llm.chat(system=JUDGE_PROMPT, history=[llm.user_message(prompt)], json_schema=JUDGE_SCHEMA, model=model)
    return json.loads(resp.text)


def tools_match(expected: list[str], used: list[str]) -> bool:
    if not expected:  # refusals / blocks / hand-offs: calling any tool is wrong
        return not used
    return all(t in used for t in expected)


def route_matches(expected, actual: str) -> bool:
    return actual in expected if isinstance(expected, list) else actual == expected


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
        session_id = None
        for earlier in case.get("turns", []):  # multi-turn: build the conversation first
            session_id = orch.handle(earlier, case["user_id"], session_id=session_id)["session_id"]
            time.sleep(args.delay)
        result = orch.handle(case["message"], case["user_id"], session_id=session_id, include_evidence=True)
        route = actual_route(result)
        tools_ok = tools_match(case["expected_tools"], result["tools_used"])
        text = result["response"].lower()
        content_ok = any(term.lower() in text for term in case["must_mention_any"])
        route_ok = route_matches(case["expected_route"], route)
        if case.get("expected_agent_contains"):
            route_ok = route_ok and case["expected_agent_contains"] in result["agent"]
        row = {
            "id": case["id"], "message": case["message"], "turns": case.get("turns", []),
            "expected_route": case["expected_route"], "route": route, "agent": result["agent"],
            "route_ok": route_ok, "tools": result["tools_used"], "tools_ok": tools_ok,
            "content_ok": content_ok, "escalated": result["escalated"], "latency_ms": result["trace"]["total_ms"],
            "tokens": result["trace"]["tokens"], "response": result["response"],
        }
        if args.judge and route not in ("blocked", "out_of_scope", "unsafe"):
            try:
                row["judge"] = judge(orch.services.llm, case, result, model=settings.judge_model)
            except Exception as exc:
                row["judge"] = {"error": str(exc)[:100]}
        rows.append(row)
        status = "PASS" if route_ok and tools_ok and content_ok else "FAIL"
        print(f"[{status}] {case['id']:<7} route={route:<12} tools={result['tools_used']} {row['latency_ms']:.0f}ms")
        time.sleep(args.delay)

    n = len(rows)
    summary = {
        "cases": n,
        "passed": sum(r["route_ok"] and r["tools_ok"] and r["content_ok"] for r in rows),
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

    from app.agents.prompts import PROMPT_VERSION
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "agent_model": settings.llm_model,
            "router_model": settings.router_model, "judge_model": settings.judge_model if args.judge else None,
            "prompt_version": PROMPT_VERSION, "retrieval_mode": settings.retrieval_mode}
    report = json.dumps({"summary": summary, "cases": rows, **meta}, indent=2, ensure_ascii=False)
    (HERE / "report.json").write_text(report)
    # Also saved on the storage volume, where the dashboard (GET /evals) reads it.
    settings.eval_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.eval_report_path.write_text(report, encoding="utf-8")
    lines = ["# Evaluation report", "", " · ".join(f"{k}: {v}" for k, v in meta.items() if v), "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += ["", "| Case | Route (expected) | Agent | Tools | Route | Tools | Content | Judge G/H | Latency |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ok = lambda b: "✅" if b else "❌"  # noqa: E731
        j = r.get("judge", {})
        jg = f"{j['groundedness']}/{j['helpfulness']}" if "groundedness" in j else "-"
        lines.append(f"| {r['id']} | {r['route']} ({r['expected_route']}) | {r['agent']} | {', '.join(r['tools']) or '-'} | "
                     f"{ok(r['route_ok'])} | {ok(r['tools_ok'])} | {ok(r['content_ok'])} | {jg} | {r['latency_ms']:.0f} ms |")
    lines += ["", "## Answers", ""]
    for r in rows:
        lines += [f"### {r['id']} — {r['message']}", "", r["response"], ""]
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
