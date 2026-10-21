"""Conversation analytics: which questions are asked most, and how well they are answered.

Every /chat request is stored in the shared database (app/db.py: SQLite on the Docker volume, or
Postgres) with its outcome. People can rate answers with 👍/👎 in the web console (POST /feedback).
GET /analytics aggregates it all for the dashboard at GET /dashboard.

How "success" (índice de acerto) is measured for each request:
  * if the user rated it  -> 👍 = success, 👎 = failure (explicit feedback wins);
  * otherwise             -> automatic resolution: answered without error, escalation or guardrail
                             block, and grounded (sources returned, or the merchant's data consulted).

Only the masked message is stored (card numbers, CPF, passwords are already hidden by the guardrails).
"""

from __future__ import annotations

import re
import statistics
import time
import unicodedata

from sqlalchemy import select, update

from app.db import Database, requests

SUPPORT_TOOLS = {"get_customer_profile", "get_sales_and_settlements", "get_recent_transactions",
                 "get_terminal_status", "open_support_ticket"}


def normalize_question(text: str) -> str:
    """Group the same question asked with different case, accents or punctuation."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def auto_resolved(result: dict) -> bool:
    route = (result.get("route") or {}).get("route")
    if result.get("agent") == "error" or result.get("escalated"):
        return False
    if (result.get("guardrails") or {}).get("blocked") or route in ("out_of_scope", "unsafe"):
        return False  # refused: correct behaviour, but the question was not answered
    if route == "greeting":
        return True
    tools = set(result.get("tools_used") or [])
    return bool(result.get("sources")) or bool(tools & SUPPORT_TOOLS)


class AnalyticsStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, message: str, result: dict) -> None:
        tokens = result.get("trace", {}).get("tokens", {})
        guard = result.get("guardrails") or {}
        row = dict(
            trace_id=result["trace_id"], ts=time.time(), user_id=result.get("user_id"), message=message[:500],
            question=normalize_question(message),
            route=(result.get("route") or {}).get("route") or ("blocked" if guard.get("blocked") else "error"),
            agent=result.get("agent"), tools=",".join(result.get("tools_used") or []),
            escalated=int(bool(result.get("escalated"))), blocked=guard.get("blocked"),
            error=int(result.get("agent") == "error"), resolved=int(auto_resolved(result)),
            latency_ms=result.get("trace", {}).get("total_ms", 0.0),
            tokens=int(tokens.get("input", 0)) + int(tokens.get("output", 0)),
            in_tokens=int(tokens.get("input", 0)), out_tokens=int(tokens.get("output", 0)), feedback=None,
        )
        with self.db.engine.begin() as conn:
            conn.execute(requests.delete().where(requests.c.trace_id == row["trace_id"]))
            conn.execute(requests.insert().values(**row))

    def feedback(self, trace_id: str, positive: bool, user_id: str | None = None) -> bool:
        """Rate an answer. With user_id, only the merchant who asked can rate it."""
        q = update(requests).where(requests.c.trace_id == trace_id).values(feedback=int(positive))
        if user_id is not None:
            q = q.where(requests.c.user_id == user_id)
        with self.db.engine.begin() as conn:
            return conn.execute(q).rowcount == 1

    def summary(self, days: float | None = None, top: int = 8) -> dict:
        since = time.time() - days * 86400 if days else 0.0
        c = requests.c
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(c.message, c.question, c.route, c.agent, c.tools, c.escalated, c.blocked, c.error,
                                       c.resolved, c.latency_ms, c.tokens, c.feedback, c.ts, c.in_tokens, c.out_tokens)
                                .where(c.ts >= since).order_by(c.ts)).all()

        def success(r) -> int:
            return r[11] if r[11] is not None else r[8]

        total = len(rows)
        rated = [r for r in rows if r[11] is not None]
        latencies = sorted(r[9] for r in rows)

        # Per question: count, success rate, latest wording seen.
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault(r[1], {"question": r[0], "asks": 0, "successes": 0, "up": 0, "down": 0,
                                         "escalations": 0, "route": r[2]})
            g["question"], g["route"] = r[0], r[2]
            g["asks"] += 1
            g["successes"] += success(r)
            g["escalations"] += r[5]
            if r[11] == 1:
                g["up"] += 1
            elif r[11] == 0:
                g["down"] += 1
        questions = []
        for g in groups.values():
            g["success_rate"] = round(g["successes"] / g["asks"], 3)
            questions.append(g)

        most_asked = sorted(questions, key=lambda g: (-g["asks"], g["question"]))[:top]
        lowest = sorted(questions, key=lambda g: (g["success_rate"], -g["asks"], g["question"]))[:top]

        routes: dict[str, int] = {}
        for r in rows:
            routes[r[2]] = routes.get(r[2], 0) + 1

        # Requests per day (local server time), oldest first.
        per_day: dict[str, dict] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(r[12]))
            d = per_day.setdefault(day, {"day": day, "requests": 0, "successes": 0})
            d["requests"] += 1
            d["successes"] += success(r)

        def pct(values: list[float], p: float) -> float:
            return round(values[min(len(values) - 1, int(p * len(values)))], 1) if values else 0.0

        return {
            "window_days": days,
            "kpis": {
                "requests": total,
                "success_rate": round(sum(success(r) for r in rows) / total, 3) if total else None,
                "auto_resolution_rate": round(sum(r[8] for r in rows) / total, 3) if total else None,
                "approval_rate": round(sum(r[11] for r in rated) / len(rated), 3) if rated else None,
                "ratings": len(rated),
                "escalation_rate": round(sum(r[5] for r in rows) / total, 3) if total else None,
                "error_rate": round(sum(r[7] for r in rows) / total, 3) if total else None,
                "latency_p50_ms": pct(latencies, 0.5),
                "latency_p95_ms": pct(latencies, 0.95),
                "latency_p99_ms": pct(latencies, 0.99),
                "avg_tokens": round(statistics.mean(r[10] for r in rows)) if rows else 0,
                "avg_input_tokens": round(statistics.mean((r[13] or 0) for r in rows)) if rows else 0,
                "avg_output_tokens": round(statistics.mean((r[14] or 0) for r in rows)) if rows else 0,
            },
            "most_asked": most_asked,
            "lowest_success": lowest,
            "routes": [{"route": k, "requests": v} for k, v in sorted(routes.items(), key=lambda kv: -kv[1])],
            "per_day": list(per_day.values()),
        }
