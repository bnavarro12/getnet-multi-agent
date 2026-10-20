"""Tool abstraction shared by all agents.

A Tool = name + description + JSON schema (what the LLM sees) + a Python function
(what actually runs). The `ToolContext` carries request-scoped data.

Security principle: the `user_id` is injected from the authenticated request context,
never taken from the LLM's arguments. The model cannot be tricked into reading another
merchant's data, because no tool accepts a user_id parameter.
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


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def run(self, ctx: ToolContext, args: dict) -> dict:
        allowed = set(self.parameters.get("properties", {}))
        clean = {k: v for k, v in (args or {}).items() if k in allowed}  # drop hallucinated args
        return self.fn(ctx, **clean)


def no_params() -> dict:
    return {"type": "object", "properties": {}}
