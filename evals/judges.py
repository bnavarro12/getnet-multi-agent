"""LLM judges for the evaluation, from different providers, plus agreement statistics.

Why more than one judge
  A judge from the same model family as the system tends to like its own style (self-preference bias).
  The panel here can mix:
    * "gemini"   - a larger Gemini model than the agents (JUDGE_MODEL, default gemini-flash-latest);
    * "external" - any OpenAI-compatible provider (EXTERNAL_JUDGE_BASE_URL / _API_KEY / _MODEL), e.g. Groq's
                   free tier with an open-weights model - a different company and model family.
  The report shows each judge's scores AND how much they agree. Low agreement means the scores are not
  trustworthy yet; human labels (evals/human_review.py) settle which judge is calibrated.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import date

import httpx

JUDGE_PROMPT = """You are grading a customer-support assistant for a payments company (Getnet).
Given the user question, the evidence the assistant retrieved (tool outputs) and its answer, score:
- groundedness (1-5): 5 = every factual claim is supported by the evidence or is generic advice; 1 = invents facts.
  Judge only claims of fact (fees, dates, amounts, product features, procedures). Today's date, polite phrasing and
  generic safety advice ("contact your bank") are not unsupported claims.
- helpfulness (1-5): 5 = directly and fully addresses the question in the user's language; 1 = useless.
Return JSON only: {"groundedness": <int>, "helpfulness": <int>, "comment": "<one sentence>"}"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"groundedness": {"type": "integer"}, "helpfulness": {"type": "integer"}, "comment": {"type": "string"}},
    "required": ["groundedness", "helpfulness", "comment"],
}


def judge_input(question: str, evidence: list, answer: str, max_chars: int) -> str:
    return (f"Today's date: {date.today().isoformat()}\n\nQuestion: {question}\n\n"
            f"Evidence:\n{json.dumps(evidence, ensure_ascii=False, default=str)[:max_chars]}\n\nAnswer:\n{answer}")


def _clean(data: dict) -> dict:
    g, h = int(data["groundedness"]), int(data["helpfulness"])
    if not (1 <= g <= 5 and 1 <= h <= 5):
        raise ValueError(f"score out of range: {g}, {h}")
    return {"groundedness": g, "helpfulness": h, "comment": str(data.get("comment", ""))[:300]}


class GeminiJudge:
    def __init__(self, llm, model: str, max_chars: int = 30000):
        self.llm, self.model, self.max_chars = llm, model, max_chars
        self.name = f"gemini:{model}"

    def score(self, question: str, evidence: list, answer: str) -> dict:
        resp = self.llm.chat(system=JUDGE_PROMPT, history=[self.llm.user_message(
            judge_input(question, evidence, answer, self.max_chars))], json_schema=JUDGE_SCHEMA, model=self.model)
        return _clean(json.loads(resp.text))


class OpenAICompatibleJudge:
    """Judge on any OpenAI-compatible /chat/completions API (Groq, OpenRouter, OpenAI, a local Ollama...)."""

    def __init__(self, base_url: str, api_key: str, model: str, max_chars: int = 12000, retries: int = 4,
                 http: httpx.Client | None = None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model, self.max_chars, self.retries = model, max_chars, retries
        self.http = http or httpx.Client(timeout=120)
        self.name = f"external:{model}"

    def score(self, question: str, evidence: list, answer: str) -> dict:
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": JUDGE_PROMPT},
                             {"role": "user", "content": judge_input(question, evidence, answer, self.max_chars)}]}
        for attempt in range(1, self.retries + 1):
            r = self.http.post(self.url, headers=self.headers, json=body)
            if r.status_code == 429 or r.status_code >= 500:  # free tiers: tokens per minute
                if attempt == self.retries:
                    r.raise_for_status()
                retry_after = r.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after is not None else 15 * attempt)
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
            match = re.search(r"\{.*\}", content, re.DOTALL)  # tolerate text around the JSON
            return _clean(json.loads(match.group() if match else content))
        raise RuntimeError("unreachable")


def build_judges(settings, llm, which: str = "all") -> list:
    judges = []
    if which in ("all", "gemini"):
        judges.append(GeminiJudge(llm, settings.judge_model))
    if which in ("all", "external") and settings.external_judge_api_key:
        judges.append(OpenAICompatibleJudge(settings.external_judge_base_url, settings.external_judge_api_key,
                                            settings.external_judge_model, settings.external_judge_max_chars))
    return judges


def agreement(a: list[int], b: list[int]) -> dict:
    """How much two raters agree on 1-5 scores (same cases, same order)."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return {"n": 0}
    n = len(pairs)
    exact = sum(x == y for x, y in pairs) / n
    within1 = sum(abs(x - y) <= 1 for x, y in pairs) / n
    mae = sum(abs(x - y) for x, y in pairs) / n
    return {"n": n, "exact": round(exact, 3), "within_1": round(within1, 3), "mean_abs_diff": round(mae, 3),
            "weighted_kappa": round(weighted_kappa(pairs), 3)}


def weighted_kappa(pairs: list[tuple[int, int]], k: int = 5) -> float:
    """Quadratic-weighted Cohen's kappa for 1..k ratings: 1 = perfect agreement, 0 = chance level."""
    n = len(pairs)
    if n == 0:
        return 0.0
    obs = [[0.0] * k for _ in range(k)]
    for x, y in pairs:
        obs[x - 1][y - 1] += 1
    row = [sum(r) for r in obs]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2)
            num += w * obs[i][j]
            den += w * row[i] * col[j] / n
    if den == 0:  # both raters constant: agreement is perfect only if identical
        return 1.0 if all(x == y for x, y in pairs) else 0.0
    return 1 - num / den


def mean_or_none(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 2) if vals else None
