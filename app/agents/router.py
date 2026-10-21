"""Agent 1 - Router: classifies the message and picks the next agent.

It uses structured output (JSON schema), so the decision is machine-readable and
validated, never parsed out of free text. A small/fast model is enough for this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.agents.base import format_input
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
    plan: list[dict] = field(default_factory=list)   # [{"agent": "support", "task": "..."}, ...]
    injection_suspected: bool = False                # semantic prompt-injection detector

    def to_dict(self) -> dict:
        d = {"route": self.route, "confidence": round(self.confidence, 2),
             "language": self.language, "reason": self.reason}
        if len(self.plan) > 1:
            d["plan"] = self.plan
        if self.injection_suspected:
            d["injection_suspected"] = True
        return d


class RouterAgent:
    name = "router"

    def __init__(self, llm: LLMClient, model: str | None = None):
        self.llm = llm
        self.model = model

    def route(self, ctx: ToolContext, message: str, history: list[dict] | None = None) -> RouteDecision:
        with ctx.trace.span("llm:router", history_turns=len(history or [])) as span:
            resp = self.llm.chat(system=ROUTER_PROMPT, history=[self.llm.user_message(format_input(message, history))],
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
                                 data.get("reason", ""), data.get("reply", "") or "",
                                 RouterAgent._clean_plan(route, data.get("plan")),
                                 bool(data.get("injection_suspected", False)))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return RouteDecision("knowledge", 0.0, "en", f"router_parse_error: {exc}")

    @staticmethod
    def _clean_plan(route: str, raw) -> list[dict]:
        """Validate the plan: known specialists only, no repeated agent, starts with the route."""
        if route not in ("knowledge", "support"):
            return []
        steps, seen = [], set()
        for step in raw if isinstance(raw, list) else []:
            agent = (step or {}).get("agent") if isinstance(step, dict) else None
            if agent in ("knowledge", "support") and agent not in seen:
                seen.add(agent)
                steps.append({"agent": agent, "task": str(step.get("task", ""))[:300]})
        if not steps or steps[0]["agent"] != route:
            steps = [s for s in steps if s["agent"] != route]
            steps.insert(0, {"agent": route, "task": ""})
        return steps
