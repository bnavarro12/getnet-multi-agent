"""Conversation analytics: which questions are asked most, and how well they are answered.

Every /chat request is stored in a small SQLite database (storage/analytics.db, on the Docker
volume) with its outcome. People can rate answers with 👍/👎 in the web console (POST /feedback).
GET /analytics aggregates it all for the dashboard at GET /dashboard.

How "success" (índice de acerto) is measured for each request:
  * if the user rated it  -> 👍 = success, 👎 = failure (explicit feedback wins);
  * otherwise             -> automatic resolution: answered without error, escalation or guardrail
                             block, and grounded (sources returned, or the merchant's data consulted).

Only the masked message is stored (card numbers, CPF, passwords are already hidden by the guardrails).
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import threading
import time
import unicodedata
from pathlib import Path

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
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                trace_id   TEXT PRIMARY KEY,
                ts         REAL NOT NULL,
                user_id    TEXT,
                message    TEXT,
                question   TEXT,
                route      TEXT,
                agent      TEXT,
                tools      TEXT,
                escalated  INTEGER,
                blocked    TEXT,
                error      INTEGER,
                resolved   INTEGER,
                latency_ms REAL,
                tokens     INTEGER,
                feedback   INTEGER   -- 1 = 👍, 0 = 👎, NULL = not rated
            )""")
        self._db.commit()

    def record(self, message: str, result: dict) -> None:
        tokens = result.get("trace", {}).get("tokens", {})
        row = (
            result["trace_id"], time.time(), result.get("user_id"), message[:500], normalize_question(message),
            (result.get("route") or {}).get("route") or ("blocked" if (result.get("guardrails") or {}).get("blocked") else "error"),
            result.get("agent"), ",".join(result.get("tools_used") or []), int(bool(result.get("escalated"))),
            (result.get("guardrails") or {}).get("blocked"), int(result.get("agent") == "error"),
            int(auto_resolved(result)), result.get("trace", {}).get("total_ms", 0.0),
            int(tokens.get("input", 0)) + int(tokens.get("output", 0)),
        )
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)", row)
            self._db.commit()

    def feedback(self, trace_id: str, positive: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE requests SET feedback = ? WHERE trace_id = ?", (int(positive), trace_id))
            self._db.commit()
            return cur.rowcount == 1

    def summary(self, days: float | None = None, top: int = 8) -> dict:
        since = time.time() - days * 86400 if days else 0.0
        with self._lock:
            rows = self._db.execute(
                "SELECT message, question, route, agent, tools, escalated, blocked, error, resolved, latency_ms, "
                "tokens, feedback, ts FROM requests WHERE ts >= ? ORDER BY ts", (since,)).fetchall()

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
                "avg_tokens": round(statistics.mean(r[10] for r in rows)) if rows else 0,
            },
            "most_asked": most_asked,
            "lowest_success": lowest,
            "routes": [{"route": k, "requests": v} for k, v in sorted(routes.items(), key=lambda kv: -kv[1])],
            "per_day": list(per_day.values()),
        }
