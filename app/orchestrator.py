"""The orchestrator: owns the workflow and the data flow between agents.

    POST /chat
        |
        v
    [Input guardrails] --blocked--> safe reply
        |
        v
    [Router Agent] --greeting / out_of_scope / unsafe--> router's short reply
        |
        +--> knowledge --> [Knowledge Agent]  (RAG + web search)
        +--> support   --> [Support Agent]    (customer-data tools + help center)
        +--> escalation, low confidence, or a specialist asked for a human
                       --> [Human Escalation Agent] (summary, priority, ticket)
        |
        v
    [Output guardrails] --> JSON response (+ sources, tools used, trace)

Communication mechanism: direct, synchronous function calls with a shared, request-scoped
`ToolContext`. It is the simplest thing that is easy to trace and test; a message queue
would only add value with async/long-running agents (see README "Production evolution").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from app.agents.router import RouterAgent
from app.agents.specialists import EscalationAgent, KnowledgeAgent, SupportAgent
from app.config import Settings
from app.data.customer_repository import CustomerRepository
from app.guardrails import BLOCK_MESSAGES, SENSITIVE_DATA_NOTICE, check_input, check_output
from app.llm import LLMClient
from app.observability import Metrics, Trace
from app.rag.retriever import Retriever
from app.tools.base import ToolContext

logger = logging.getLogger(__name__)

FALLBACK_ERROR = {
    "pt": "Desculpe, tive um problema técnico para responder agora. Tente novamente em instantes.",
    "en": "Sorry, I had a technical problem answering right now. Please try again in a moment.",
}


@dataclass
class Services:
    """Dependencies shared by agents and tools (injected, so tests can replace any of them)."""
    llm: LLMClient
    repo: CustomerRepository
    retriever: Retriever
    web_search: Callable[[str], list[dict]]
    today: Callable[[], date] = date.today


class Orchestrator:
    def __init__(self, services: Services, settings: Settings, metrics: Metrics | None = None):
        self.services = services
        self.s = settings
        self.metrics = metrics or Metrics()
        self.router = RouterAgent(services.llm, model=settings.router_model)

    def handle(self, message: str, user_id: str) -> dict[str, Any]:
        trace = Trace(user_id)
        ctx = ToolContext(user_id=user_id, trace=trace, services=self.services)
        warnings: list[str] = []
        route_info: dict = {}
        agent_name, text, tools_used, escalated, blocked = "guardrail", "", [], False, None
        language = "en"

        try:
            # 1) Input guardrails ---------------------------------------------------
            with trace.span("guardrail:input") as span:
                gr = check_input(message)
                warnings += gr.warnings
                span.attrs.update(allowed=gr.allowed, reason=gr.reason, warnings=gr.warnings)
            if not gr.allowed:
                blocked = gr.reason
                text = BLOCK_MESSAGES[gr.reason]
                return self._respond(ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings, blocked)
            safe_message = gr.text

            # 2) Routing ------------------------------------------------------------
            decision = self.router.route(ctx, safe_message)
            route_info = decision.to_dict()
            language = decision.language
            today = self.services.today()

            if decision.route in ("greeting", "out_of_scope", "unsafe"):
                agent_name = "router"
                blocked = decision.route if decision.route != "greeting" else None
                text = decision.reply or BLOCK_MESSAGES["unsafe"]

            elif decision.route == "escalation" or decision.confidence < self.s.router_confidence_threshold:
                ctx.escalation_requested = True
                ctx.escalation_reason = ("user requested a human / sensitive case" if decision.route == "escalation"
                                         else f"low routing confidence ({decision.confidence:.2f})")

            else:
                # 3) Specialist agent ----------------------------------------------
                agent_cls = KnowledgeAgent if decision.route == "knowledge" else SupportAgent
                agent = agent_cls(self.services.llm, self.s.max_tool_iterations, today, model=self.s.llm_model)
                agent_name = agent.name
                result = agent.run(ctx, safe_message)
                text, tools_used = result.text, result.tools_used

            # 4) Human hand-off (explicit, low confidence, or requested by a specialist)
            if ctx.escalation_requested:
                escalated = True
                handoff = EscalationAgent(self.services.llm, today, model=self.s.llm_model).run(
                    ctx, safe_message, language, ctx.escalation_reason, partial_answer=text)
                text = f"{text}\n\n{handoff['text']}".strip() if text else handoff["text"]
                agent_name = f"{agent_name}+escalation" if agent_name not in ("guardrail",) else "escalation"

            if not text:
                text = FALLBACK_ERROR.get(language[:2], FALLBACK_ERROR["en"])

            if any(w.startswith("sensitive_data_masked") for w in warnings):
                text = f"{SENSITIVE_DATA_NOTICE}\n\n{text}"

            return self._respond(ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings, blocked)

        except Exception as exc:
            logger.exception("orchestrator_error trace_id=%s", trace.trace_id)
            self.metrics.record(route=route_info.get("route", "error"), latency_ms=trace.total_ms,
                                trace=trace, escalated=False, error=True)
            return {
                "trace_id": trace.trace_id, "user_id": user_id,
                "response": FALLBACK_ERROR.get(language[:2], FALLBACK_ERROR["en"]),
                "agent": "error", "route": route_info, "sources": [], "tools_used": tools_used,
                "escalated": False, "tickets": [], "guardrails": {"warnings": warnings, "blocked": None},
                "error": type(exc).__name__, "trace": trace.to_dict(),
            }

    def _respond(self, ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings, blocked):
        with ctx.trace.span("guardrail:output") as span:
            text, out_warnings = check_output(text)
            span.attrs["warnings"] = out_warnings
        warnings = warnings + out_warnings
        trace = ctx.trace
        self.metrics.record(route=route_info.get("route", "blocked"), latency_ms=trace.total_ms, trace=trace,
                            escalated=escalated, blocked_reason=blocked)
        return {
            "trace_id": trace.trace_id,
            "user_id": user_id,
            "response": text,
            "agent": agent_name,
            "route": route_info,
            "sources": ctx.sources[:5],
            "tools_used": tools_used,
            "escalated": escalated,
            "tickets": ctx.tickets,
            "guardrails": {"warnings": warnings, "blocked": blocked},
            "trace": trace.to_dict(),
        }
