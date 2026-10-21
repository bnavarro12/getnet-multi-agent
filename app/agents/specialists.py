"""Agents 2, 3 and 4: Knowledge, Customer Support and Human Escalation, plus the Supervisor
that merges the answers when the Router's plan needs more than one specialist."""

from __future__ import annotations

import json
from datetime import date

from app.agents.base import ROLE_LABELS, ToolAgent
from app.agents.prompts import (COMMON_RULES, ESCALATION_PROMPT, ESCALATION_SCHEMA, KNOWLEDGE_PROMPT,
                                SUPERVISOR_PROMPT, SUPPORT_PROMPT)
from app.llm import LLMClient
from app.tools.base import ToolContext
from app.tools.escalation_tools import ESCALATE_TO_HUMAN
from app.tools.knowledge_tools import SEARCH_KNOWLEDGE_BASE, WEB_SEARCH
from app.tools.support_tools import (GET_CUSTOMER_PROFILE, GET_RECENT_TRANSACTIONS, GET_SALES_AND_SETTLEMENTS,
                                     GET_TERMINAL_STATUS, OPEN_SUPPORT_TICKET)


def _common(today: date) -> str:
    return COMMON_RULES.format(today=today.isoformat())


class KnowledgeAgent(ToolAgent):
    """Agent 2 - answers product/service questions with RAG, and general questions with web search."""
    name = "knowledge"

    def __init__(self, llm: LLMClient, max_iterations: int, today: date, model: str | None = None):
        super().__init__(llm, KNOWLEDGE_PROMPT.format(common=_common(today)),
                         [SEARCH_KNOWLEDGE_BASE, WEB_SEARCH, ESCALATE_TO_HUMAN], max_iterations, model)


class SupportAgent(ToolAgent):
    """Agent 3 - resolves account-specific issues using the merchant's data."""
    name = "support"

    def __init__(self, llm: LLMClient, max_iterations: int, today: date, model: str | None = None):
        tools = [GET_CUSTOMER_PROFILE, GET_SALES_AND_SETTLEMENTS, GET_RECENT_TRANSACTIONS, GET_TERMINAL_STATUS,
                 OPEN_SUPPORT_TICKET, SEARCH_KNOWLEDGE_BASE, ESCALATE_TO_HUMAN]
        super().__init__(llm, SUPPORT_PROMPT.format(common=_common(today)), tools, max_iterations, model)


class EscalationAgent:
    """Agent 4 (bonus) - prepares a human hand-off: case summary, priority, ticket and user message."""
    name = "escalation"

    FALLBACK_MESSAGES = {
        "pt": "Vou transferir seu atendimento para um especialista humano. Seu protocolo é {ticket_id}. "
              "Você será contatado em até 1 dia útil.",
        "en": "I'm transferring you to a human specialist. Your protocol number is {ticket_id}. "
              "You'll be contacted within 1 business day.",
    }

    def __init__(self, llm: LLMClient, today: date, model: str | None = None):
        self.llm = llm
        self.today = today
        self.model = model

    def run(self, ctx: ToolContext, message: str, language: str, reason: str, partial_answer: str = "",
            history: list[dict] | None = None) -> dict:
        lang = (language or "en")[:2]
        prompt = ESCALATION_PROMPT.format(language=lang, common=_common(self.today))
        earlier = "\n".join(f"{ROLE_LABELS.get(t['role'], t['role'])}: {t['text'][:500]}" for t in history or [])
        case = (f"Earlier conversation:\n{earlier or '(none)'}\n\nUser message: {message}\nEscalation reason: {reason}\n"
                f"Assistant work so far: {partial_answer or '(none)'}")
        with ctx.trace.span("llm:escalation", reason=reason) as span:
            try:
                resp = self.llm.chat(system=prompt, history=[self.llm.user_message(case)],
                                     json_schema=ESCALATION_SCHEMA, model=self.model)
                ctx.trace.add_usage(resp.input_tokens, resp.output_tokens)
                data = json.loads(resp.text)
            except Exception as exc:  # hand-off must work even if the LLM fails
                span.attrs["fallback"] = str(exc)[:150]
                data = {"summary": f"{reason} | user said: {message}"[:500], "priority": "normal",
                        "category": "other", "user_message": ""}
            span.attrs.update(priority=data.get("priority"), category=data.get("category"))

        ticket = ctx.services.repo.create_ticket(
            ctx.user_id, data.get("category", "other"), data.get("summary", reason),
            priority=data.get("priority", "normal"), kind="human_handoff",
        )
        ctx.tickets.append(ticket["ticket_id"])
        template = data.get("user_message") or self.FALLBACK_MESSAGES.get(lang, self.FALLBACK_MESSAGES["en"])
        if "{ticket_id}" not in template:
            template += " (protocol: {ticket_id})"
        return {"text": template.replace("{ticket_id}", ticket["ticket_id"]), "ticket": ticket,
                "summary": data.get("summary", reason), "priority": data.get("priority", "normal"),
                "category": data.get("category", "other")}


class SupervisorAgent:
    """Merges the findings of several specialists into one answer (no tools: synthesis only)."""
    name = "supervisor"

    def __init__(self, llm: LLMClient, today: date, model: str | None = None):
        self.llm = llm
        self.today = today
        self.model = model

    def run(self, ctx: ToolContext, message: str, language: str, findings: list[tuple[str, str]]) -> str:
        prompt = SUPERVISOR_PROMPT.format(language=(language or "en")[:2], common=_common(self.today))
        body = "\n\n".join(f"Findings from the {name} specialist:\n{text}" for name, text in findings)
        with ctx.trace.span("llm:supervisor", specialists=[n for n, _ in findings]) as span:
            try:
                resp = self.llm.chat(system=prompt, history=[self.llm.user_message(
                    f"Customer message: {message}\n\n{body}")], model=self.model)
                ctx.trace.add_usage(resp.input_tokens, resp.output_tokens)
                span.attrs.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
                if resp.text.strip():
                    return resp.text.strip()
            except Exception as exc:  # synthesis failed: still answer with the parts
                span.attrs["fallback"] = str(exc)[:150]
        return "\n\n".join(text for _, text in findings)
