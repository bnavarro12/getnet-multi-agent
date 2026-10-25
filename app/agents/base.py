"""Base class for tool-using agents: the "agent loop".

    user message -> LLM -> wants a tool? -> run tool -> give result back -> LLM -> ... -> final text

The loop is capped (MAX_TOOL_ITERATIONS) so a confused model can never spin forever or
burn the token budget. Every LLM call and tool call is recorded as a span in the trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.llm import LLMClient
from app.tools.base import Tool, ToolContext


@dataclass
class AgentResult:
    text: str
    tools_used: list[str] = field(default_factory=list)
    hit_iteration_limit: bool = False


ROLE_LABELS = {"user": "Customer", "assistant": "Assistant", "human_agent": "Human specialist"}


def format_input(message: str, history: list[dict] | None = None, task: str | None = None,
                 summary: str = "") -> str:
    """Build the text an agent receives: summary of older turns + recent turns + the current message (+ task)."""
    parts = []
    if summary:
        parts.append(f"Summary of the earlier conversation: {summary}")
    if history:
        lines = [f"{ROLE_LABELS.get(t['role'], t['role'])}: {t['text'][:800]}" for t in history]
        parts.append("Conversation so far (oldest first, for context only):\n" + "\n".join(lines))
    parts.append(f"Current customer message: {message}" if history or task or summary else message)
    if task:
        parts.append(f"Your task in this plan: {task}")
    return "\n\n".join(parts)


def _summarize(result: dict, limit: int = 300) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)[:limit]


class ToolAgent:
    name = "agent"

    def __init__(self, llm: LLMClient, system_prompt: str, tools: list[Tool], max_iterations: int, model: str | None = None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in tools}
        self.max_iterations = max_iterations
        self.model = model

    def run(self, ctx: ToolContext, message: str) -> AgentResult:
        history = [self.llm.user_message(message)]
        schemas = [t.schema() for t in self.tools.values()]
        used: list[str] = []

        for iteration in range(1, self.max_iterations + 1):
            with ctx.trace.span(f"llm:{self.name}", iteration=iteration) as span:
                resp = self.llm.chat(system=self.system_prompt, history=history, tools=schemas, model=self.model)
                span.attrs.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                                  tool_calls=[c.name for c in resp.tool_calls])
            ctx.trace.add_usage(resp.input_tokens, resp.output_tokens)

            if not resp.tool_calls:
                return AgentResult(text=resp.text.strip(), tools_used=used)

            history.append(resp.raw_content)  # keep the model's own turn (incl. thought signatures)
            results = []
            for call in resp.tool_calls:
                tool = self.tools.get(call.name)
                with ctx.trace.span(f"tool:{call.name}", agent=self.name, args=call.args) as tspan:
                    if tool is None:
                        output = {"error": f"Unknown tool '{call.name}'."}
                    else:
                        try:
                            output = tool.run(ctx, call.args)
                        except Exception as exc:  # a broken tool must not crash the request
                            output = {"error": f"Tool failed: {type(exc).__name__}"}
                            tspan.attrs["exception"] = str(exc)[:200]
                    tspan.attrs["result"] = _summarize(output)
                used.append(call.name)
                ctx.add_evidence({"tool": call.name, "args": call.args, "result": output})
                results.append((call.name, output))
            history.append(self.llm.tool_results_message(results))

        # Iteration limit reached: fail safe and let the orchestrator hand off to a human.
        ctx.escalation_requested = True
        ctx.escalation_reason = ctx.escalation_reason or f"{self.name} could not finish within {self.max_iterations} steps"
        return AgentResult(text="", tools_used=used, hit_iteration_limit=True)
