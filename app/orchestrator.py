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

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from app.agents.base import format_input
from app.agents.prompts import APPROVAL_TASK, HUMAN_REPLY_TASK, SUMMARY_PROMPT
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
from app.tools.knowledge_tools import frankfurter_rate, open_meteo_forecast

logger = logging.getLogger(__name__)

ALREADY_WITH_HUMAN = {
    "pt": "Seu caso já está com um especialista humano (protocolo {ticket_id}). Acrescentei esta mensagem ao atendimento.",
    "en": "Your case is already with a human specialist (protocol {ticket_id}). I've added this message to it.",
}
WAITING_APPROVAL = {
    "pt": "Seu pedido de estorno da transação {tx} ({amount}) foi registrado e precisa da aprovação de um especialista "
          "da Getnet antes de ser executado (protocolo {ticket_id}). Nada foi estornado ainda; avisaremos aqui.",
    "en": "Your refund request for transaction {tx} ({amount}) was recorded and needs a Getnet specialist's approval "
          "before it runs (protocol {ticket_id}). Nothing has been refunded yet; we will update you here.",
}
APPROVAL_FALLBACK = {
    "pt": "Atualização do protocolo {ticket_id}: o estorno foi {decision_pt}. {reply}",
    "en": "Update on protocol {ticket_id}: the refund was {decision}. {reply}",
}
HUMAN_REPLY_FALLBACK = {
    "pt": "Resposta do nosso especialista (protocolo {ticket_id}): {reply}",
    "en": "Reply from our specialist (protocol {ticket_id}): {reply}",
}

FALLBACK_ERROR = {
    "pt": "Desculpe, tive um problema técnico para responder agora. Tente novamente em instantes.",
    "en": "Sorry, I had a technical problem answering right now. Please try again in a moment.",
}


def brl(value) -> str:
    """1234.5 -> "R$ 1.234,50" (the templates below are the only money the code itself writes)."""
    return f"R$ {float(value):,.2f}".replace(",", "~").replace(".", ",").replace("~", ".")


@dataclass
class Services:
    """Dependencies shared by agents and tools (injected, so tests can replace any of them)."""
    llm: LLMClient
    repo: CustomerRepository
    retriever: Retriever
    web_search: Callable[[str], list[dict]]
    today: Callable[[], date] = date.today
    weather: Callable[[str, str], dict] = open_meteo_forecast   # real-time data APIs (replaceable in tests)
    fx: Callable[[str, str], dict] = frankfurter_rate


class Orchestrator:
    def __init__(self, services: Services, settings: Settings, metrics: Metrics | None = None,
                 memory: ConversationStore | None = None):
        self.services = services
        self.s = settings
        self.metrics = metrics or Metrics()
        self.memory = memory
        self.db = memory.db if memory is not None else None
        self.on_handoff: Callable[[dict], None] | None = None   # e.g. post the case to Telegram
        self.on_overdue: Callable[[dict], None] | None = None   # SLA missed: warn the team again
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
            ctx.summary = self._refresh_summary(ctx)
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

            if decision.injection_suspected:  # semantic layer: paraphrased / disguised injection
                agent_name, blocked = "router", "prompt_injection"
                trace.add_flag("semantic_injection_blocked")
                text = BLOCK_MESSAGES["prompt_injection"]

            elif decision.route in ("greeting", "out_of_scope", "unsafe"):
                agent_name = "router"
                blocked = decision.route if decision.route != "greeting" else None
                text = decision.reply or BLOCK_MESSAGES["unsafe"]

            elif decision.route == "escalation" or decision.confidence < self.s.router_confidence_threshold:
                ctx.escalation_requested = True
                ctx.escalation_reason = ("user requested a human / sensitive case" if decision.route == "escalation"
                                         else f"low routing confidence ({decision.confidence:.2f})")

            else:
                # 3) Supervisor: run the plan (fan-out), observe, decide ------------
                text, agent_name, used = self._run_plan(ctx, decision, safe_message, history, today)
                tools_used += used
                if ctx.approval_request and not ctx.escalation_requested:  # risky action: a human decides
                    escalated = True
                    text = self._request_approval(ctx, language, agent_name)

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

    # -- plan execution ---------------------------------------------------------
    def _run_step(self, ctx: ToolContext, kind: str, message: str, history: list[dict], task: str | None, today):
        agent = self._agent(kind, today)
        result = agent.run(ctx, format_input(message, history, task, summary=ctx.summary))
        return agent.name, result

    def _run_plan(self, ctx: ToolContext, decision, message: str, history: list[dict], today) -> tuple[str, str, list[str]]:
        """Supervisor loop: run the plan's specialists, observe the findings, decide to answer or call one more.

            plan (1-2 steps) --fan-out (parallel)--> findings --> supervisor decides
                 answer                     -> done
                 missing = other specialist -> run it (bounded) -> supervisor answers
                 a step asked for a human   -> stop, hand off (re-planning)
        """
        plan = (decision.plan or [{"agent": decision.route, "task": ""}])[: max(1, self.s.max_plan_steps)]
        multi = len(plan) > 1
        findings: list[tuple[str, str]] = []
        used: list[str] = []

        if multi and self.s.plan_parallel:
            with ctx.trace.span("plan:fan_out", steps=[s["agent"] for s in plan]):
                with ThreadPoolExecutor(max_workers=len(plan)) as pool:
                    futures = [pool.submit(self._run_step, ctx, st["agent"], message, history, st["task"], today)
                               for st in plan]
                    results = [f.result() for f in futures]  # plan order, whatever finishes first
            for name, result in results:
                findings.append((name, result.text))
                used += result.tools_used
        else:
            for st in plan:
                name, result = self._run_step(ctx, st["agent"], message, history, st["task"] if multi else None, today)
                findings.append((name, result.text))
                used += result.tools_used
                if ctx.escalation_requested:  # re-plan: the remaining steps go to a human instead
                    break

        agent_name = "+".join(name for name, _ in findings)
        if ctx.escalation_requested or not multi:
            answered = [t for _, t in findings if t]
            return (answered[0] if answered else ""), agent_name, used

        supervisor = SupervisorAgent(self.services.llm, today, model=self.s.llm_model)
        extra = max(0, self.s.supervisor_extra_steps)
        verdict = supervisor.run(ctx, message, decision.language, findings, allow_more=extra > 0)
        rounds = 0
        while verdict["missing"] != "none" and rounds < extra:
            rounds += 1
            name, result = self._run_step(ctx, verdict["missing"], message, history,
                                          verdict["missing_task"] or None, today)
            findings.append((name, result.text))
            used += result.tools_used
            agent_name += f"+{name}"
            if ctx.escalation_requested:
                return verdict["answer"], agent_name, used
            verdict = supervisor.run(ctx, message, decision.language, findings, allow_more=rounds < extra)
        return verdict["answer"], agent_name + "+supervisor", used

    # -- human hand-off ---------------------------------------------------------
    def _hand_off(self, ctx: ToolContext, message: str, language: str, partial: str, agent_name: str,
                  history: list[dict]) -> str:
        lang = (language or "en")[:2]
        existing = self.memory.open_handoff_for(ctx.session_id) if self.memory and ctx.session_id else None
        if existing:  # idempotent: one open hand-off per conversation
            ctx.add_ticket(existing["ticket_id"])
            ctx.add_evidence({"tool": "human_handoff", "args": {"reason": ctx.escalation_reason},
                                 "result": {"ticket_id": existing["ticket_id"], "status": "already_open"}})
            note = ALREADY_WITH_HUMAN.get(lang, ALREADY_WITH_HUMAN["en"]).format(ticket_id=existing["ticket_id"])
            return f"{partial}\n\n{note}".strip() if partial else note
        handoff = EscalationAgent(self.services.llm, self.services.today(), model=self.s.llm_model).run(
            ctx, message, language, ctx.escalation_reason, partial_answer=partial, history=history)
        # The hand-off is a system action: record it as evidence, so the answer's protocol number and
        # response time can be checked (the eval judges otherwise saw them as invented - found by calibration).
        ctx.add_evidence({"tool": "human_handoff", "args": {"reason": ctx.escalation_reason},
                             "result": {"ticket_id": handoff["ticket"]["ticket_id"], "priority": handoff["priority"],
                                        "category": handoff["category"],
                                        "response_time_policy": "high priority: up to 2 hours; normal: up to 1 business day"}})
        if self.memory and ctx.session_id:
            self.memory.create_handoff(
                ticket_id=handoff["ticket"]["ticket_id"], session_id=ctx.session_id, user_id=ctx.user_id,
                agent=agent_name, language=lang, reason=ctx.escalation_reason, summary=handoff["summary"],
                priority=handoff["priority"], category=handoff["category"])
            if self.on_handoff is not None:  # notify humans without delaying the customer's answer
                created = self.memory.get_handoff(handoff["ticket"]["ticket_id"])
                threading.Thread(target=self.on_handoff, args=(created,), daemon=True).start()
        return f"{partial}\n\n{handoff['text']}".strip() if partial else handoff["text"]

    def _request_approval(self, ctx: ToolContext, language: str, agent_name: str) -> str:
        """The agent asked for a risky action (a refund). It is NOT executed: an APR case waits for a human."""
        lang = (language or "en")[:2]
        action = ctx.approval_request
        ticket = self.services.repo.create_ticket(ctx.user_id, f"refund:{action['transaction_id']}"[:32],
                                                  f"Refund approval: {action['transaction_id']} ({brl(action['amount'])}). "
                                                  f"{action.get('reason', '')}", kind="approval")
        ctx.add_ticket(ticket["ticket_id"])
        ctx.add_evidence({"tool": "approval_request", "args": action,
                          "result": {"ticket_id": ticket["ticket_id"], "status": "waiting_for_human_approval"}})
        if self.memory and ctx.session_id:
            self.memory.create_handoff(
                ticket_id=ticket["ticket_id"], session_id=ctx.session_id, user_id=ctx.user_id, agent=agent_name,
                language=lang, reason="approval required: refund", summary=ticket["summary"], priority="normal",
                category="refund", kind="approval", action=action)
            if self.on_handoff is not None:
                created = self.memory.get_handoff(ticket["ticket_id"])
                threading.Thread(target=self.on_handoff, args=(created,), daemon=True).start()
        return WAITING_APPROVAL.get(lang, WAITING_APPROVAL["en"]).format(
            tx=action["transaction_id"], amount=brl(action["amount"]), ticket_id=ticket["ticket_id"])

    def _refresh_summary(self, ctx: ToolContext) -> str:
        """Rolling memory: turns that left the window are folded into a short summary (one cheap LLM call,
        only every few turns). The window keeps recent detail; the summary keeps what turn 1 was about."""
        old = self.memory.summary(ctx.session_id)
        pending = self.memory.turns_to_summarize(ctx.session_id)
        if not pending:
            return old
        convo = "\n".join(f"{t['role']}: {t['text'][:500]}" for t in pending)
        try:
            with ctx.trace.span("memory:summarize", turns=len(pending)):
                resp = self.services.llm.chat(system=SUMMARY_PROMPT, model=self.s.router_model, history=[
                    self.services.llm.user_message(f"Current summary: {old or '(none)'}\n\nNew turns:\n{convo}")])
                ctx.trace.add_usage(resp.input_tokens, resp.output_tokens)
            new = (resp.text or "").strip()
            if new:
                self.memory.save_summary(ctx.session_id, new, pending[-1]["id"])
                return new
        except Exception:  # memory must never break the answer: keep the old summary
            logger.exception("summary_failed")
        return old

    def check_sla(self) -> list[dict]:
        """Open cases past their response-time target are re-escalated once (priority -> high) and the team
        is warned again. Safe with several replicas: the re-escalation is claimed atomically."""
        if self.memory is None:
            return []
        sla = {"high": self.s.sla_high_minutes, "normal": self.s.sla_normal_minutes}
        missed = [h for h in self.memory.overdue_handoffs(sla) if self.memory.mark_reescalated(h["ticket_id"])]
        for h in missed:
            logger.warning(json.dumps({"event": "handoff_sla_missed", "ticket_id": h["ticket_id"],
                                       "priority_was": h["priority"]}))
            if self.on_overdue is not None:
                self.on_overdue(h)
        return missed

    def resume_after_human(self, ticket_id: str, reply: str, close: bool = False, operator: str = "admin",
                           decision: str | None = None) -> dict[str, Any]:
        """Human-in-the-loop, second half: the specialist answered -> the agent continues.

        The human's reply is stored in the conversation, then the specialist agent that had the case
        writes the next message to the customer from it (it may call tools to complement it).
        Approval cases (kind="approval") need decision="approve" or "reject"; only "approve" runs the action.
        `operator` (who answered) is stored for audit.
        Raises KeyError (unknown ticket) or ValueError (already answered / no memory / missing decision).
        """
        if self.memory is None:
            raise ValueError("conversation memory is disabled")
        h = self.memory.get_handoff(ticket_id)
        if h is None:
            raise KeyError(ticket_id)
        approval = h.get("kind") == "approval"
        if approval:
            if decision not in ("approve", "reject"):
                raise ValueError("this case needs a decision: approve or reject")
            close, reply = True, reply or decision
        elif not reply.strip():
            raise ValueError("empty reply")
        if h["status"] != "open" or not self.memory.answer_handoff(ticket_id, reply, close, operator):
            # claimed atomically: with several replicas (or two specialists) only one reply wins
            raise ValueError(f"hand-off already {(self.memory.get_handoff(ticket_id) or h)['status']}")
        trace = Trace(h["user_id"])
        ctx = ToolContext(user_id=h["user_id"], trace=trace, services=self.services, session_id=h["session_id"])
        history = self.memory.history(h["session_id"])
        self.memory.add_turn(h["session_id"], "human_agent", reply, meta={"ticket_id": ticket_id, "operator": operator})
        ctx.summary = self.memory.summary(h["session_id"])

        lang = (h["language"] or "en")[:2]
        result_info = None
        if approval:  # the risky action runs only now, after a named human approved it
            action = json.loads(h["action"] or "{}")
            result_info = (self.services.repo.execute_refund(h["user_id"], action.get("transaction_id", ""), operator)
                           if decision == "approve" else {"status": "rejected", "rejected_by": operator})
            done = "approved" if decision == "approve" else "rejected"
            task = APPROVAL_TASK.format(decision=done, action=f"refund of {action.get('transaction_id')}",
                                        ticket_id=ticket_id, result=json.dumps(result_info), reply=reply)
            fallback = APPROVAL_FALLBACK.get(lang, APPROVAL_FALLBACK["en"]).format(
                ticket_id=ticket_id, decision=done, decision_pt="aprovado" if decision == "approve" else "recusado",
                reply="" if reply == decision else reply)
        else:
            task = HUMAN_REPLY_TASK.format(ticket_id=ticket_id, reply=reply)
            fallback = HUMAN_REPLY_FALLBACK.get(lang, HUMAN_REPLY_FALLBACK["en"]).format(ticket_id=ticket_id, reply=reply)

        kind = "knowledge" if (h["agent"] or "").startswith("knowledge") else "support"
        agent = self._agent(kind, self.services.today())
        text, tools_used = "", []
        try:
            result = agent.run(ctx, format_input("(no new customer message: continue after the specialist's reply)",
                                                 history, task, summary=ctx.summary))
            text, tools_used = result.text, result.tools_used
        except Exception:
            logger.exception("resume_after_human_failed ticket=%s", ticket_id)
        if not text:  # the human's answer must reach the customer even if the LLM fails
            text = fallback
        text, _ = check_output(text)
        status = "closed" if close else "answered"
        self.memory.add_turn(h["session_id"], "assistant", text, trace.trace_id,
                             meta={"agent": agent.name, "after_human_reply": True, "ticket_id": ticket_id})
        self.services.repo.update_ticket(ticket_id, status=status)
        logger.info(json.dumps({"event": "handoff_answered", "ticket_id": ticket_id, "operator": operator,
                                "kind": h.get("kind") or "handoff", "decision": decision if approval else None}))
        return {"ticket_id": ticket_id, "session_id": h["session_id"], "status": status, "answered_by": operator,
                "decision": decision if approval else None, "action_result": result_info,
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
