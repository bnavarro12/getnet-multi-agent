"""Agent 1 - Router: classifies the message and picks the next agent.

It uses structured output (JSON schema), so the decision is machine-readable and
validated, never parsed out of free text. A small/fast model is enough for this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.agents.prompts import ROUTER_PROMPT, ROUTER_SCHEMA
from app.llm import LLMClient
from app.tools.base import ToolContext

VALID_ROUTES = set(ROUTER_SCHEMA["properties"]["route"]["enum"])


@dataclass
class RouteDecision:
    route: str
    confidence: float
    language: str
    reason: str
    reply: str = ""

    def to_dict(self) -> dict:
        return {"route": self.route, "confidence": round(self.confidence, 2),
                "language": self.language, "reason": self.reason}


class RouterAgent:
    name = "router"

    def __init__(self, llm: LLMClient, model: str | None = None):
        self.llm = llm
        self.model = model

    def route(self, ctx: ToolContext, message: str) -> RouteDecision:
        with ctx.trace.span("llm:router") as span:
            resp = self.llm.chat(system=ROUTER_PROMPT, history=[self.llm.user_message(message)],
                                 json_schema=ROUTER_SCHEMA, model=self.model)
            ctx.trace.add_usage(resp.input_tokens, resp.output_tokens)
            decision = self._parse(resp.text)
            span.attrs.update(decision.to_dict(), input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
        return decision

    @staticmethod
    def _parse(raw: str) -> RouteDecision:
        """Defensive parsing: an invalid answer becomes a low-confidence decision, not a crash."""
        try:
            data = json.loads(raw)
            route = data.get("route", "")
            if route not in VALID_ROUTES:
                raise ValueError(f"invalid route {route!r}")
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
            return RouteDecision(route, confidence, data.get("language") or "en",
                                 data.get("reason", ""), data.get("reply", "") or "")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return RouteDecision("knowledge", 0.0, "en", f"router_parse_error: {exc}")
