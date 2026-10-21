"""Human calibration of the LLM judges: do the judges score like a person would?

    python -m evals.human_review label            # shows each judged answer, you type your scores (resumable)
    python -m evals.human_review label --n 20     # label at most 20 cases
    python -m evals.human_review score            # agreement between you and each judge -> calibration_report.md

How it works
  1. `label` reads the last evals/report.json and shows, for each judged case, the question, an excerpt of
     the evidence the agent retrieved and the agent's answer. The judges' scores are HIDDEN (blind review),
     so they cannot anchor you. You type groundedness and helpfulness (1-5), using the same rubric as the
     judges (below). Labels are saved after every case in evals/human_labels.csv.
  2. `score` compares your labels with each judge: exact agreement, within one point, mean absolute
     difference and quadratic-weighted Cohen's kappa (κ ≥ 0.6 is usually read as substantial agreement).
     A judge that disagrees with people is not trusted, whatever its average says.
"""

from __future__ import annotations

import argparse
import csv
import json
import textwrap
import time
from pathlib import Path

from evals.judges import agreement

HERE = Path(__file__).resolve().parent
LABELS = HERE / "human_labels.csv"
FIELDS = ["id", "answer_sha", "human_groundedness", "human_helpfulness", "notes", "labeled_at"]


def answer_sha(case: dict) -> str:
    """Labels belong to one specific answer: after a new eval run the answer changes and must be re-labelled."""
    import hashlib
    return hashlib.sha256(case["response"].encode("utf-8")).hexdigest()[:12]

RUBRIC = """Rubric (same as the judges):
  groundedness 1-5: ONLY "did it invent facts?". 5 = every fact (fee, date, amount, feature, procedure) is
                    supported by the evidence or is generic advice; 1 = invents facts.
                    An answer that honestly says "I couldn't find it" invents nothing -> high groundedness.
  helpfulness  1-5: "did it solve the customer's question?". 5 = directly and fully answers it, in the
                    user's language; 1 = useless. Not finding the information lowers THIS score."""


def load_report() -> dict:
    path = HERE / "report.json"
    if not path.exists():
        raise SystemExit("No evals/report.json yet: run  python -m evals.run_evals --judge  first.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_labels() -> dict[str, dict]:
    if not LABELS.exists():
        return {}
    with LABELS.open(encoding="utf-8", newline="") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def save_labels(labels: dict[str, dict]) -> None:
    with LABELS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in labels.values():
            w.writerow({k: r.get(k, "") for k in FIELDS})


def readable_evidence(raw: str, limit: int = 2200) -> str:
    """Turn the stored JSON excerpt into plain text: one block per source, without escapes and tags."""
    import re
    blocks = []
    for m in re.finditer(r'"tool": "([^"]+)"', raw):
        blocks.append((m.start(), f"[tool: {m.group(1)}]"))
    for m in re.finditer(r'"text": "(.*?)(?:(?<!\\)"|$)', raw, re.S):
        blocks.append((m.start(), m.group(1)))
    for m in re.finditer(r'"snippet": "(.*?)(?:(?<!\\)"|$)', raw, re.S):
        blocks.append((m.start(), m.group(1)))
    if not any(not b.startswith("[tool:") for _, b in blocks):  # customer data (no page text): show it as is
        text = raw
    else:
        text = "\n\n".join(b for _, b in sorted(blocks))
    text = text.replace("\\n", "\n").replace('\\"', '"')
    text = re.sub(r"<untrusted_content source=\"([^\"]*)\">", r"(source: \1)\n", text)
    text = re.sub(r"</?untrusted_content[^>]*>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + (" …" if len(text) > limit else "")


def ask_score(prompt: str) -> int | None:
    while True:
        raw = input(prompt).strip().lower()
        if raw in ("s", "skip"):
            return None
        if raw in ("q", "quit"):
            raise KeyboardInterrupt
        if raw in {"1", "2", "3", "4", "5"}:
            return int(raw)
        print("  type 1-5, s to skip, q to quit")


def label(limit: int) -> None:
    report = load_report()
    labels = load_labels()
    todo = [c for c in report["cases"] if "judge" in c
            and labels.get(c["id"], {}).get("answer_sha") != answer_sha(c)][:limit]
    if not todo:
        print("Nothing to label (all judged cases already have your scores).")
        return
    print(RUBRIC, "\n")
    try:
        for n, c in enumerate(todo, 1):
            wrap = lambda t: textwrap.fill(t, 100, subsequent_indent="  ")  # noqa: E731
            print("=" * 100)
            print(f"[{n}/{len(todo)}] {c['id']}  ({c['category']})")
            if c.get("turns"):
                print(wrap("Earlier turns: " + " | ".join(c["turns"])))
            print(wrap("QUESTION: " + c["message"]))
            print("-" * 100)
            print("EVIDENCE (excerpt - the judges saw the full text):")
            for para in readable_evidence(c.get("evidence_excerpt", "")).split("\n"):
                if para.strip():
                    print(wrap("  " + para.strip()))
            print("-" * 100)
            print(wrap("ANSWER: " + c["response"]))
            print()
            g = ask_score("  groundedness 1-5 (s = skip, q = quit): ")
            if g is None:
                continue
            h = ask_score("  helpfulness  1-5: ")
            if h is None:
                continue
            notes = input("  notes (optional): ").strip()
            labels[c["id"]] = {"id": c["id"], "answer_sha": answer_sha(c), "human_groundedness": g, "human_helpfulness": h, "notes": notes,
                               "labeled_at": time.strftime("%Y-%m-%d %H:%M")}
            save_labels(labels)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped - progress saved.")
    print(f"{len(labels)} labels in {LABELS.name}. Next: python -m evals.human_review score")


def score() -> None:
    report = load_report()
    labels = load_labels()
    cases = [c for c in report["cases"] if "judges" in c
             and labels.get(c["id"], {}).get("answer_sha") == answer_sha(c)]
    if not cases:
        raise SystemExit("No overlap between your labels and the judged cases in report.json.")
    judge_names = sorted({name for c in cases for name in c["judges"]} | {"panel (mean)"})
    lines = ["# Judge calibration against human labels", "",
             f"{len(cases)} cases labelled by a person (blind to the judges' scores) · report of "
             f"{report.get('generated_at', '?')}", "",
             "| Judge | Metric | n | Exact | Within 1 | Mean abs. diff | Weighted κ |", "|---|---|---|---|---|---|---|"]
    out = {}
    for name in judge_names:
        for metric in ("groundedness", "helpfulness"):
            human = [int(labels[c["id"]][f"human_{metric}"]) for c in cases]
            if name == "panel (mean)":
                judge = [round(c["judge"][metric]) if "judge" in c else None for c in cases]
            else:
                judge = [c["judges"].get(name, {}).get(metric) for c in cases]
            a = agreement(human, judge)
            out.setdefault(name, {})[metric] = a
            if a.get("n"):
                lines.append(f"| {name} | {metric} | {a['n']} | {a['exact']} | {a['within_1']} | "
                             f"{a['mean_abs_diff']} | {a['weighted_kappa']} |")
    lines += ["", "Reading: within-1 ≥ 0.9 and κ ≥ 0.6 → the judge can be trusted for regression testing; "
              "lower → fix the judge prompt or change the judge before trusting its averages.", ""]
    disagreements = [(c["id"], labels[c["id"]], c["judge"]) for c in cases if "judge" in c
                     and abs(int(labels[c["id"]]["human_groundedness"]) - c["judge"]["groundedness"]) >= 2]
    if disagreements:
        lines += ["## Largest disagreements (groundedness, ≥ 2 points)", ""]
        for cid, h, j in disagreements:
            lines.append(f"- {cid}: human {h['human_groundedness']} vs panel {j['groundedness']} — {h.get('notes', '')}")
    (HERE / "calibration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (HERE / "calibration_report.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["label", "score"])
    parser.add_argument("--n", type=int, default=100, help="max cases to label in this session")
    args = parser.parse_args()
    label(args.n) if args.command == "label" else score()


if __name__ == "__main__":
    main()
