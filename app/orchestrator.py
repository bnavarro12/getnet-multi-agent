"""The orchestrator: owns the workflow, the data flow between agents and the conversation state.

    POST /chat {message, user_id, session_id?}
        |
        v
    [Input guardrails] --blocked--> safe reply
        |
        v
    [Conversation memory] last N turns of this session (SQLite, survives restarts)
        |
        v
    [Router Agent] -> route + confidence + PLAN (1-2 specialists)
        |   greeting / out_of_scope / unsafe --> router's short reply
        |   escalation or low confidence     --> human hand-off
        v
    [Supervisor] runs the plan in order:
        support   --> [Support Agent]    (customer-data tools + help center)
        knowledge --> [Knowledge Agent]  (RAG + web search)
        (a step that asks for a human stops the plan: re-planning to a hand-off)
        2+ answers --> [Supervisor synthesis] one combined answer
        |
        v
    [Human Escalation Agent] when requested: summary, priority, HUM- ticket linked to the session
        |
        v
    [Output guardrails] --> JSON response (+ session_id, sources, tools used, trace)

    Human-in-the-loop: POST /handoffs/{ticket}/reply -> resume_after_human(): the human's answer
    is written into the same session and the specialist agent continues the conversation from it.

Communication mechanism: direct, synchronous function calls with a shared, request-scoped
`ToolContext`. It is the simplest thing that is easy to trace and test; a message queue
would only add value with async/long-running agents (see README "Production evolution").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from app.agents.base import format_input
from app.agents.prompts import HUMAN_REPLY_TASK
from app.agents.router import RouterAgent
from app.agents.specialists import EscalationAgent, KnowledgeAgent, SupervisorAgent, SupportAgent
from app.config import Settings
from app.data.customer_repository import CustomerRepository
from app.guardrails import BLOCK_MESSAGES, SENSITIVE_DATA_NOTICE, check_input, check_output
from app.llm import LLMClient
from app.memory import BLOCKED_PLACEHOLDER, ConversationStore
from app.observability import Metrics, Trace
from app.rag.retriever import Retriever
from app.tools.base import ToolContext

logger = logging.getLogger(__name__)

ALREADY_WITH_HUMAN = {
    "pt": "Seu caso já está com um especialista humano (protocolo {ticket_id}). Acrescentei esta mensagem ao atendimento.",
    "en": "Your case is already with a human specialist (protocol {ticket_id}). I've added this message to it.",
}
HUMAN_REPLY_FALLBACK = {
    "pt": "Resposta do nosso especialista (protocolo {ticket_id}): {reply}",
    "en": "Reply from our specialist (protocol {ticket_id}): {reply}",
}

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
    def __init__(self, services: Services, settings: Settings, metrics: Metrics | None = None,
                 memory: ConversationStore | None = None):
        self.services = services
        self.s = settings
        self.metrics = metrics or Metrics()
        self.memory = memory
        self.router = RouterAgent(services.llm, model=settings.router_model)

    def _agent(self, kind: str, today):
        cls = KnowledgeAgent if kind == "knowledge" else SupportAgent
        return cls(self.services.llm, self.s.max_tool_iterations, today, model=self.s.llm_model)

    def handle(self, message: str, user_id: str, session_id: str | None = None,
               include_evidence: bool = False) -> dict[str, Any]:
        """Run one message through the workflow. include_evidence=True adds the full tool outputs
        to the response (used by the evaluation judge; the public API never sets it).
        Raises PermissionError if session_id belongs to another merchant."""
        trace = Trace(user_id)
        ctx = ToolContext(user_id=user_id, trace=trace, services=self.services, include_evidence=include_evidence)
        history: list[dict] = []
        if self.memory is not None:
            ctx.session_id = self.memory.resolve_session(user_id, session_id)
            history = self.memory.history(ctx.session_id)
        warnings: list[str] = []
        route_info: dict = {}
        agent_name, text, tools_used, escalated, blocked = "guardrail", "", [], False, None
        language = "en"
        stored_message = BLOCKED_PLACEHOLDER

        try:
            # 1) Input guardrails ---------------------------------------------------
            with trace.span("guardrail:input") as span:
                gr = check_input(message)
                warnings += gr.warnings
                span.attrs.update(allowed=gr.allowed, reason=gr.reason, warnings=gr.warnings)
            if not gr.allowed:
                blocked = gr.reason
                text = BLOCK_MESSAGES[gr.reason]
                return self._respond(ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings,
                                     blocked, stored_message)
            safe_message = stored_message = gr.text

            # Deterministic identity check: asking for ANOTHER merchant's account is refused before any LLM
            # call (the tools could only read the caller's own data anyway, but the answer would mislead).
            others = self.services.repo.other_accounts_mentioned(safe_message, user_id)
            if others:
                blocked = "other_account"
                trace.add_flag("other_account_requested")
                return self._respond(ctx, user_id, BLOCK_MESSAGES["other_account"], agent_name, route_info,
                                     tools_used, escalated, warnings, blocked, stored_message)

            # 2) Routing + plan (with the conversation so far) --------------------
            decision = self.router.route(ctx, safe_message, history)
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
                # 3) Supervisor: run the plan's specialists in order --------------
                plan = (decision.plan or [{"agent": decision.route, "task": ""}])[: max(1, self.s.max_plan_steps)]
                multi = len(plan) > 1
                findings: list[tuple[str, str]] = []
                for step in plan:
                    agent = self._agent(step["agent"], today)
                    result = agent.run(ctx, format_input(safe_message, history, step["task"] if multi else None))
                    tools_used += result.tools_used
                    findings.append((agent.name, result.text))
                    if ctx.escalation_requested:  # re-plan: the remaining steps go to a human instead
                        break
                agent_name = "+".join(name for name, _ in findings)
                answered = [(n, t) for n, t in findings if t]
                if len(answered) > 1:
                    text = SupervisorAgent(self.services.llm, today, model=self.s.llm_model).run(
                        ctx, safe_message, language, answered)
                    agent_name += "+supervisor"
                else:
                    text = answered[0][1] if answered else ""

            # 4) Human hand-off (explicit, low confidence, or requested by a specialist)
            if ctx.escalation_requested:
                escalated = True
                text = self._hand_off(ctx, safe_message, language, text, agent_name, history)
                agent_name = f"{agent_name}+escalation" if agent_name not in ("guardrail",) else "escalation"

            if not text:
                text = FALLBACK_ERROR.get(language[:2], FALLBACK_ERROR["en"])

            if any(w.startswith("sensitive_data_masked") for w in warnings):
                text = f"{SENSITIVE_DATA_NOTICE}\n\n{text}"

            return self._respond(ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings,
                                 blocked, stored_message)

        except Exception as exc:
            logger.exception("orchestrator_error trace_id=%s", trace.trace_id)
            self.metrics.record(route=route_info.get("route", "error"), latency_ms=trace.total_ms,
                                trace=trace, escalated=False, error=True)
            text = FALLBACK_ERROR.get(language[:2], FALLBACK_ERROR["en"])
            self._remember(ctx, stored_message, text, {"agent": "error"})
            return {
                "trace_id": trace.trace_id, "user_id": user_id, "session_id": ctx.session_id,
                "response": text,
                "agent": "error", "route": route_info, "sources": [], "tools_used": tools_used,
                "escalated": False, "tickets": [], "guardrails": {"warnings": warnings, "blocked": None},
                "error": type(exc).__name__, "trace": trace.to_dict(),
            }

    # -- human hand-off ---------------------------------------------------------
    def _hand_off(self, ctx: ToolContext, message: str, language: str, partial: str, agent_name: str,
                  history: list[dict]) -> str:
        lang = (language or "en")[:2]
        existing = self.memory.open_handoff_for(ctx.session_id) if self.memory and ctx.session_id else None
        if existing:  # idempotent: one open hand-off per conversation
            ctx.tickets.append(existing["ticket_id"])
            note = ALREADY_WITH_HUMAN.get(lang, ALREADY_WITH_HUMAN["en"]).format(ticket_id=existing["ticket_id"])
            return f"{partial}\n\n{note}".strip() if partial else note
        handoff = EscalationAgent(self.services.llm, self.services.today(), model=self.s.llm_model).run(
            ctx, message, language, ctx.escalation_reason, partial_answer=partial, history=history)
        if self.memory and ctx.session_id:
            self.memory.create_handoff(
                ticket_id=handoff["ticket"]["ticket_id"], session_id=ctx.session_id, user_id=ctx.user_id,
                agent=agent_name, language=lang, reason=ctx.escalation_reason, summary=handoff["summary"],
                priority=handoff["priority"], category=handoff["category"])
        return f"{partial}\n\n{handoff['text']}".strip() if partial else handoff["text"]

    def resume_after_human(self, ticket_id: str, reply: str, close: bool = False) -> dict[str, Any]:
        """Human-in-the-loop, second half: the specialist answered -> the agent continues.

        The human's reply is stored in the conversation, then the specialist agent that had the case
        writes the next message to the customer from it (it may call tools to complement it).
        Raises KeyError (unknown ticket) or ValueError (already answered / no memory).
        """
        if self.memory is None:
            raise ValueError("conversation memory is disabled")
        h = self.memory.get_handoff(ticket_id)
        if h is None:
            raise KeyError(ticket_id)
        if h["status"] != "open":
            raise ValueError(f"hand-off already {h['status']}")
        trace = Trace(h["user_id"])
        ctx = ToolContext(user_id=h["user_id"], trace=trace, services=self.services, session_id=h["session_id"])
        history = self.memory.history(h["session_id"])
        self.memory.add_turn(h["session_id"], "human_agent", reply, meta={"ticket_id": ticket_id})

        kind = "knowledge" if (h["agent"] or "").startswith("knowledge") else "support"
        agent = self._agent(kind, self.services.today())
        task = HUMAN_REPLY_TASK.format(ticket_id=ticket_id, reply=reply)
        text, tools_used = "", []
        try:
            result = agent.run(ctx, format_input("(no new customer message: continue after the specialist's reply)",
                                                 history, task))
            text, tools_used = result.text, result.tools_used
        except Exception:
            logger.exception("resume_after_human_failed ticket=%s", ticket_id)
        lang = (h["language"] or "en")[:2]
        if not text:  # the human's answer must reach the customer even if the LLM fails
            text = HUMAN_REPLY_FALLBACK.get(lang, HUMAN_REPLY_FALLBACK["en"]).format(ticket_id=ticket_id, reply=reply)
        text, _ = check_output(text)
        self.memory.add_turn(h["session_id"], "assistant", text, trace.trace_id,
                             meta={"agent": agent.name, "after_human_reply": True, "ticket_id": ticket_id})
        self.memory.answer_handoff(ticket_id, reply, close)
        self.services.repo.update_ticket(ticket_id, status="closed" if close else "answered")
        return {"ticket_id": ticket_id, "session_id": h["session_id"], "status": "closed" if close else "answered",
                "message_to_customer": text, "agent": agent.name, "tools_used": tools_used, "trace": trace.to_dict()}

    # -- response -----------------------------------------------------------------
    def _remember(self, ctx: ToolContext, user_text: str, answer: str, meta: dict) -> None:
        if self.memory is None or not ctx.session_id:
            return
        try:
            self.memory.add_turn(ctx.session_id, "user", user_text, ctx.trace.trace_id)
            self.memory.add_turn(ctx.session_id, "assistant", answer, ctx.trace.trace_id, meta=meta)
        except Exception:  # memory must never break the answer
            logger.exception("memory_write_failed")

    def _respond(self, ctx, user_id, text, agent_name, route_info, tools_used, escalated, warnings, blocked,
                 stored_message=BLOCKED_PLACEHOLDER):
        with ctx.trace.span("guardrail:output") as span:
            text, out_warnings = check_output(text)
            span.attrs["warnings"] = out_warnings
        warnings = warnings + out_warnings
        trace = ctx.trace
        self.metrics.record(route=route_info.get("route", "blocked"), latency_ms=trace.total_ms, trace=trace,
                            escalated=escalated, blocked_reason=blocked)
        self._remember(ctx, stored_message, text, {"agent": agent_name, "tickets": ctx.tickets})
        open_handoff = self.memory.open_handoff_for(ctx.session_id) if self.memory and ctx.session_id else None
        response = {
            "trace_id": trace.trace_id,
            "user_id": user_id,
            "session_id": ctx.session_id,
            "response": text,
            "agent": agent_name,
            "route": route_info,
            "sources": ctx.sources[:5],
            "tools_used": tools_used,
            "escalated": escalated,
            "tickets": ctx.tickets,
            "open_handoff": open_handoff["ticket_id"] if open_handoff else None,
            "guardrails": {"warnings": warnings, "blocked": blocked},
            "trace": trace.to_dict(),
        }
        if ctx.include_evidence:
            response["evidence"] = ctx.evidence
        return response
