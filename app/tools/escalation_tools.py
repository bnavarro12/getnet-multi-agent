"""The escalation tool lets any specialist agent ask for a human hand-off.

It does not hand off by itself: it raises a flag on the context, and the orchestrator
then routes the conversation to the Human Escalation Agent. This keeps the decision
"who handles this" in one place (the orchestrator) instead of agents calling each other.
"""

from __future__ import annotations

from app.tools.base import Tool, ToolContext


def _escalate(ctx: ToolContext, reason: str) -> dict:
    ctx.escalation_requested = True
    ctx.escalation_reason = reason[:300]
    return {"status": "escalation_requested", "note": "A human specialist will take over. Tell the user briefly."}


ESCALATE_TO_HUMAN = Tool(
    name="escalate_to_human",
    description=(
        "Request a hand-off to a human specialist. Use ONLY when: the user explicitly asks for a human; "
        "the user is very upset or reports fraud, chargeback dispute, or a legal issue; or you cannot "
        "resolve the request with your tools and knowledge. Do not use it for simple questions."
    ),
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why a human is needed"}},
        "required": ["reason"],
    },
    fn=_escalate,
)
