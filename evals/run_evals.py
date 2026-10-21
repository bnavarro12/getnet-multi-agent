"""Offline evaluation of the multi-agent system against a labelled dataset (real LLM).

    python -m evals.run_evals                  # rule checks only (route, tools, content)
    python -m evals.run_evals --judge          # + every configured LLM judge (Gemini and, if set, an external one)
    python -m evals.run_evals --judge --repeat 3   # run the dataset 3x: pass rate and stability per case

What it measures per case and in aggregate:
  * routing accuracy   - did the Router pick the expected route (and, if labelled, the expected agents)?
  * tool selection     - were the expected tools called? Cases that expect NO tools (refusals, blocks,
                         hand-offs) fail if any tool is called.
  * content check      - the answer mentions an expected key term and none of the forbidden ones
                         (e.g. a card number the user typed must never come back).
  * LLM judges         - groundedness (no invented facts vs. the full tool outputs) and helpfulness, 1-5,
                         from a PANEL of judges of different providers, with their agreement (evals/judges.py).
  * per category       - knowledge, support, memory, mixed, adversarial... so a regression is easy to locate.
  * stability          - with --repeat, how often each case passes (LLMs are not deterministic).
  * latency and tokens.

Reports: evals/report.md + report.json (committed) and storage/eval_report.json (dashboard).
Human calibration of the judges: evals/human_review.py.
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
from evals.judges import agreement, build_judges, mean_or_none

HERE = Path(__file__).resolve().parent
NOT_JUDGED = ("blocked", "out_of_scope", "unsafe", "greeting")
CATEGORY_BY_PREFIX = {"kb": "knowledge", "web": "web", "sup": "support", "pt": "portuguese", "esc": "escalation",
                      "oos": "out_of_scope", "grd": "guardrail", "unk": "unknown_customer", "mix": "mixed",
                      "mem": "memory", "adv": "adversarial", "grt": "greeting"}


def actual_route(result: dict) -> str:
    if result["guardrails"].get("blocked") in ("prompt_injection", "other_account", "empty_message", "message_too_long"):
        return "blocked"
    return (result["route"] or {}).get("route", "error")


def tools_match(expected: list[str], used: list[str]) -> bool:
    if not expected:  # refusals / blocks / hand-offs: calling any tool is wrong
        return not used
    return all(t in used for t in expected)


def route_matches(expected, actual: str) -> bool:
    return actual in expected if isinstance(expected, list) else actual == expected


def content_matches(case: dict, text: str) -> bool:
    low = text.lower()
    wanted = case.get("must_mention_any") or []
    ok = any(t.lower() in low for t in wanted) if wanted else True
    return ok and not any(t.lower() in low for t in case.get("must_not_mention", []))


def category(case: dict) -> str:
    return case.get("category") or CATEGORY_BY_PREFIX.get(case["id"].split("-")[0], "other")


def run_case(orch, case: dict, delay: float) -> tuple[dict, dict]:
    session_id = None
    for earlier in case.get("turns", []):  # multi-turn: build the conversation first
        session_id = orch.handle(earlier, case["user_id"], session_id=session_id)["session_id"]
        time.sleep(delay)
    result = orch.handle(case["message"], case["user_id"], session_id=session_id, include_evidence=True)
    route = actual_route(result)
    route_ok = route_matches(case["expected_route"], route)
    if case.get("expected_agent_contains"):
        route_ok = route_ok and case["expected_agent_contains"] in result["agent"]
    row = {
        "id": case["id"], "category": category(case), "message": case["message"], "turns": case.get("turns", []),
        "expected_route": case["expected_route"], "route": route, "agent": result["agent"], "route_ok": route_ok,
        "tools": result["tools_used"], "tools_ok": tools_match(case["expected_tools"], result["tools_used"]),
        "content_ok": content_matches(case, result["response"]), "escalated": result["escalated"],
        "latency_ms": result["trace"]["total_ms"], "tokens": result["trace"]["tokens"], "response": result["response"],
        # excerpt kept for human review (evals/human_review.py); the judges get the full evidence
        "evidence_excerpt": json.dumps(result.get("evidence", []), ensure_ascii=False, default=str)[:2500],
    }
    row["passed"] = row["route_ok"] and row["tools_ok"] and row["content_ok"]
    return row, result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", action="store_true", help="score answers with the configured LLM judges")
    parser.add_argument("--judges", default="all", choices=["all", "gemini", "external"], help="which judges to use")
    parser.add_argument("--repeat", type=int, default=1, help="run the dataset N times (stability)")
    parser.add_argument("--only", default="", help="comma-separated case ids or categories")
    parser.add_argument("--delay", type=float, default=6.0, help="seconds between cases (rate limits)")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("WARNING")
    orch = build_orchestrator(settings)
    cases = [json.loads(x) for x in (HERE / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c["id"] in keep or category(c) in keep]
    judges = build_judges(settings, orch.services.llm, args.judges) if args.judge else []
    if args.judge:
        print("Judges:", ", ".join(j.name for j in judges) or "none configured")

    runs: list[list[dict]] = []
    for run in range(1, max(1, args.repeat) + 1):
        rows = []
        for case in cases:
            row, result = run_case(orch, case, args.delay)
            if judges and row["route"] not in NOT_JUDGED:
                row["judges"] = {}
                for j in judges:
                    try:
                        row["judges"][j.name] = j.score(case["message"], result.get("evidence", []), result["response"])
                    except Exception as exc:
                        row["judges"][j.name] = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
                ok = [v for v in row["judges"].values() if "groundedness" in v]
                if ok:  # panel score (mean of the judges) - also what the dashboard shows
                    row["judge"] = {"groundedness": round(statistics.mean(v["groundedness"] for v in ok), 2),
                                    "helpfulness": round(statistics.mean(v["helpfulness"] for v in ok), 2)}
            rows.append(row)
            print(f"[{'PASS' if row['passed'] else 'FAIL'}] run {run} {case['id']:<7} route={row['route']:<12} "
                  f"tools={row['tools']} {row['latency_ms']:.0f}ms")
            time.sleep(args.delay)
        runs.append(rows)

    last = runs[-1]
    n = len(last)
    all_rows = [r for rs in runs for r in rs]
    pass_rate = {c["id"]: sum(r["passed"] for rs in runs for r in rs if r["id"] == c["id"]) / len(runs) for c in cases}
    summary = {
        "cases": n,
        "runs": len(runs),
        "passed": sum(1 for v in pass_rate.values() if v == 1.0),      # passed in EVERY run
        "pass_rate": round(statistics.mean(r["passed"] for r in all_rows), 3),
        "routing_accuracy": round(statistics.mean(r["route_ok"] for r in all_rows), 3),
        "tool_selection_accuracy": round(statistics.mean(r["tools_ok"] for r in all_rows), 3),
        "content_check_rate": round(statistics.mean(r["content_ok"] for r in all_rows), 3),
        "latency_p50_ms": round(statistics.median(r["latency_ms"] for r in all_rows)),
        "latency_p95_ms": round(sorted(r["latency_ms"] for r in all_rows)[min(len(all_rows) - 1, int(0.95 * len(all_rows)))]),
        "avg_tokens": round(statistics.mean(r["tokens"]["input"] + r["tokens"]["output"] for r in all_rows)),
    }
    if len(runs) > 1:
        summary["unstable_cases"] = sorted(k for k, v in pass_rate.items() if 0 < v < 1)

    by_cat: dict[str, dict] = {}
    for r in all_rows:
        b = by_cat.setdefault(r["category"], {"cases": 0, "passed": 0})
        b["cases"] += 1
        b["passed"] += int(r["passed"])
    for b in by_cat.values():
        b["pass_rate"] = round(b["passed"] / b["cases"], 3)
    summary["by_category"] = by_cat

    judge_stats: dict[str, dict] = {}
    if judges:
        judged = [r for r in all_rows if "judges" in r]
        for j in judges:
            g = [r["judges"][j.name].get("groundedness") for r in judged]
            h = [r["judges"][j.name].get("helpfulness") for r in judged]
            judge_stats[j.name] = {"avg_groundedness": mean_or_none(g), "avg_helpfulness": mean_or_none(h),
                                   "scored": sum(v is not None for v in g)}
        if len(judges) >= 2:
            a, b = judges[0].name, judges[1].name
            judge_stats["agreement"] = {
                "between": [a, b],
                "groundedness": agreement([r["judges"][a].get("groundedness") for r in judged],
                                          [r["judges"][b].get("groundedness") for r in judged]),
                "helpfulness": agreement([r["judges"][a].get("helpfulness") for r in judged],
                                         [r["judges"][b].get("helpfulness") for r in judged]),
            }
        panel = [r["judge"] for r in judged if "judge" in r]
        if panel:
            summary["avg_groundedness"] = round(statistics.mean(p["groundedness"] for p in panel), 2)
            summary["avg_helpfulness"] = round(statistics.mean(p["helpfulness"] for p in panel), 2)
    summary["judges"] = judge_stats

    from app.agents.prompts import PROMPT_VERSION
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "agent_model": settings.llm_model,
            "router_model": settings.router_model, "judge_models": [j.name for j in judges] or None,
            "prompt_version": PROMPT_VERSION, "retrieval_mode": settings.retrieval_mode}
    report = json.dumps({"summary": summary, "cases": last, "pass_rate_by_case": pass_rate, **meta},
                        indent=2, ensure_ascii=False)
    (HERE / "report.json").write_text(report, encoding="utf-8")
    settings.eval_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.eval_report_path.write_text(report, encoding="utf-8")   # read by the dashboard (GET /evals)
    write_markdown(summary, last, pass_rate, meta, judges)
    print(json.dumps({k: v for k, v in summary.items() if k != "by_category"}, indent=2))


def write_markdown(summary: dict, rows: list[dict], pass_rate: dict, meta: dict, judges: list) -> None:
    ok = lambda b: "✅" if b else "❌"  # noqa: E731
    lines = ["# Evaluation report", "", " · ".join(f"{k}: {v}" for k, v in meta.items() if v), "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items() if k not in ("by_category", "judges")]
    lines += ["", "## By category", "", "| Category | Cases | Passed | Pass rate |", "|---|---|---|---|"]
    for name, b in sorted(summary["by_category"].items()):
        lines.append(f"| {name} | {b['cases']} | {b['passed']} | {b['pass_rate']} |")
    if summary.get("judges"):
        lines += ["", "## Judges", "", "| Judge | Groundedness | Helpfulness | Scored |", "|---|---|---|---|"]
        for name, st in summary["judges"].items():
            if name != "agreement":
                lines.append(f"| {name} | {st['avg_groundedness']} | {st['avg_helpfulness']} | {st['scored']} |")
        agr = summary["judges"].get("agreement")
        if agr:
            lines += ["", f"Agreement between `{agr['between'][0]}` and `{agr['between'][1]}` "
                          "(exact · within 1 point · mean abs. difference · quadratic-weighted kappa):", ""]
            for metric in ("groundedness", "helpfulness"):
                a = agr[metric]
                if a.get("n"):
                    lines.append(f"- {metric}: {a['exact']} · {a['within_1']} · {a['mean_abs_diff']} · "
                                 f"κ {a['weighted_kappa']} (n={a['n']})")
    lines += ["", "## Cases", "", "| Case | Category | Route (expected) | Agent | Tools | Route | Tools | Content | "
              "Judges G/H | Pass rate | Latency |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        j = r.get("judge", {})
        jg = f"{j['groundedness']}/{j['helpfulness']}" if j else "-"
        lines.append(f"| {r['id']} | {r['category']} | {r['route']} ({r['expected_route']}) | {r['agent']} | "
                     f"{', '.join(r['tools']) or '-'} | {ok(r['route_ok'])} | {ok(r['tools_ok'])} | {ok(r['content_ok'])} | "
                     f"{jg} | {pass_rate[r['id']]:.2f} | {r['latency_ms']:.0f} ms |")
    lines += ["", "## Answers", ""]
    for r in rows:
        lines += [f"### {r['id']} — {r['message']}", "", r["response"], ""]
        for name, v in (r.get("judges") or {}).items():
            if "comment" in v:
                lines.append(f"> {name}: G {v['groundedness']} / H {v['helpfulness']} — {v['comment']}")
        lines.append("")
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
