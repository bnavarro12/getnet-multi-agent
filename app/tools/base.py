"""Tool abstraction shared by all agents.

A Tool = name + description + JSON schema (what the LLM sees) + a Python function
(what actually runs). The `ToolContext` carries request-scoped data.

Security principle: the `user_id` is injected from the authenticated request context,
never taken from the LLM's arguments. The model cannot be tricked into reading another
merchant's data, because no tool accepts a user_id parameter.

Read vs. write tools: tools that change something outside the conversation (opening a ticket)
are marked `side_effect=True`. The run-time policy allows at most MAX_WRITES_PER_TURN writes per
request, and the write itself is idempotent (see CustomerRepository.create_ticket), so a model
that loops or repeats itself cannot open a pile of duplicate tickets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.observability import Trace


@dataclass
class ToolContext:
    user_id: str
    trace: Trace
    services: Any                      # app.orchestrator.Services (repo, retriever, search...)
    escalation_requested: bool = False
    escalation_reason: str = ""
    sources: list[dict] = field(default_factory=list)   # citations collected during the turn
    tickets: list[str] = field(default_factory=list)
    # Full tool outputs of the turn (the trace keeps only a short summary for logs).
    # Returned only on request, e.g. to the evaluation judge.
    evidence: list[dict] = field(default_factory=list)
    include_evidence: bool = False
    session_id: str | None = None
    writes: int = 0                    # side-effecting tool calls made in this turn


MAX_WRITES_PER_TURN = 1


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]
    side_effect: bool = False          # True = writes/changes data (needs idempotency and limits)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def run(self, ctx: ToolContext, args: dict) -> dict:
        allowed = set(self.parameters.get("properties", {}))
        clean = {k: v for k, v in (args or {}).items() if k in allowed}  # drop hallucinated args
        if self.side_effect:
            if ctx.writes >= MAX_WRITES_PER_TURN:
                return {"error": "write_limit_reached",
                        "note": "An action was already taken in this turn; do not repeat it. Report its result."}
            ctx.writes += 1
        return self.fn(ctx, **clean)


def no_params() -> dict:
    return {"type": "object", "properties": {}}
