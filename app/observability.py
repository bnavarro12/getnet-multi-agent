"""Tracing, structured logging and in-process metrics.

Every request gets a `Trace` with a unique id. Each step (guardrail, routing, LLM call,
tool call, escalation) is recorded as a span with its latency and metadata. The trace is:
  * emitted as one JSON log line per span (easy to ship to Loki/ELK/Datadog), and
  * returned in the API response (`trace` field) so evaluators can see the agent path.

`Metrics` keeps simple counters/latencies exposed at GET /metrics. In production these
would be Prometheus/OpenTelemetry metrics; the shape is intentionally the same.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("trace")


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@dataclass
class Span:
    name: str
    started: float
    duration_ms: float = 0.0
    attrs: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self, user_id: str):
        self.trace_id = uuid.uuid4().hex[:16]
        self.user_id = user_id
        self.spans: list[Span] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.flags: dict[str, int] = {}
        self._t0 = time.perf_counter()

    @contextmanager
    def span(self, name: str, **attrs):
        s = Span(name=name, started=time.perf_counter(), attrs=dict(attrs))
        try:
            yield s
        except Exception as exc:
            s.attrs["error"] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            s.duration_ms = round((time.perf_counter() - s.started) * 1000, 1)
            self.spans.append(s)
            logger.info(json.dumps({
                "trace_id": self.trace_id, "user_id": self.user_id, "span": name,
                "duration_ms": s.duration_ms, **s.attrs,
            }, ensure_ascii=False, default=str))

    def add_flag(self, name: str, count: int = 1) -> None:
        """Security/quality signals raised during the request (e.g. removed injection text)."""
        self.flags[name] = self.flags.get(name, 0) + count

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "total_ms": self.total_ms,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            **({"flags": self.flags} if self.flags else {}),
            "steps": [{"step": s.name, "ms": s.duration_ms, **s.attrs} for s in self.spans],
        }


class Metrics:
    """Thread-safe counters for GET /metrics."""

    def __init__(self, price_input_per_mtok: float = 0.10, price_output_per_mtok: float = 0.40):
        self.prices = (price_input_per_mtok, price_output_per_mtok)
        self._lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.routes: Counter[str] = Counter()
        self.guardrail_blocks: Counter[str] = Counter()
        self.escalations = 0
        self.tool_calls: Counter[str] = Counter()
        self.latencies_ms: dict[str, list[float]] = defaultdict(list)
        self.tokens = {"input": 0, "output": 0}
        self.flags: Counter[str] = Counter()

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.prices[0] + output_tokens * self.prices[1]) / 1_000_000

    def record(self, *, route: str, latency_ms: float, trace: Trace, escalated: bool,
               blocked_reason: str | None = None, error: bool = False) -> None:
        with self._lock:
            self.requests += 1
            self.errors += int(error)
            self.routes[route] += 1
            self.escalations += int(escalated)
            if blocked_reason:
                self.guardrail_blocks[blocked_reason] += 1
            lat = self.latencies_ms[route]
            lat.append(latency_ms)
            del lat[:-500]  # keep a rolling window
            self.tokens["input"] += trace.input_tokens
            self.tokens["output"] += trace.output_tokens
            self.flags.update(trace.flags)
            for s in trace.spans:
                if s.name.startswith("tool:"):
                    self.tool_calls[s.name[5:]] += 1

    def snapshot(self) -> dict:
        def pct(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(p * len(ordered)))]

        with self._lock:
            return {
                "requests_total": self.requests,
                "errors_total": self.errors,
                "escalations_total": self.escalations,
                "routes": dict(self.routes),
                "guardrail_blocks": dict(self.guardrail_blocks),
                "tool_calls": dict(self.tool_calls),
                "tokens": dict(self.tokens),
                "estimated_cost_usd": {
                    "total": round(self.cost_usd(self.tokens["input"], self.tokens["output"]), 6),
                    "per_request": round(self.cost_usd(self.tokens["input"], self.tokens["output"]) / self.requests, 6)
                    if self.requests else 0.0,
                    "prices_per_mtok_usd": {"input": self.prices[0], "output": self.prices[1]},
                },
                "security_flags": dict(self.flags),
                "latency_ms": {
                    r: {"p50": pct(v, 0.5), "p95": pct(v, 0.95), "p99": pct(v, 0.99), "count": len(v)}
                    for r, v in self.latencies_ms.items()
                },
            }
