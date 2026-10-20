"""Deterministic test doubles: a scripted LLM and a fake web search.

The FakeLLM follows the same `LLMClient` interface as GeminiClient, so the whole
orchestration (guardrails, routing, tool loops, escalation, API) is exercised end to end
without network access or API keys. LLM *quality* is measured separately by evals/.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from app.agents.prompts import ESCALATION_SCHEMA, ROUTER_SCHEMA
from app.llm import LLMResponse, ToolCall

DIM = 64


def hash_embed(text: str) -> list[float]:
    """Bag-of-words hashing embedding: texts sharing words get similar vectors."""
    vec = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeLLM:
    """Rule-based stand-in for the model.

    * Router calls (ROUTER_SCHEMA): keyword classification, or `route_override`.
    * Escalation calls (ESCALATION_SCHEMA): fixed JSON.
    * Agent calls with tools: on the first turn, calls the tool chosen by `tool_plan`;
      once tool results are present, answers with a text that embeds them.
    """

    def __init__(self, route_override: dict | None = None, tool_plan=None, never_stop: bool = False):
        self.route_override = route_override
        self.tool_plan = tool_plan or self.default_tool_plan
        self.never_stop = never_stop
        self.calls: list[dict] = []

    # --- message builders (plain dicts are enough for the fake) -------------------
    def user_message(self, text):
        return {"role": "user", "text": text}

    def tool_results_message(self, results):
        return {"role": "tool", "results": results}

    def embed(self, texts, *, is_query=False):
        return [hash_embed(t) for t in texts]

    # --- generation ----------------------------------------------------------
    def chat(self, *, system, history, tools=None, json_schema=None, model=None):
        self.calls.append({"system": system[:40], "tools": [t["name"] for t in tools or []], "schema": bool(json_schema)})
        user_text = next(m["text"] for m in history if m.get("role") == "user")

        if json_schema is ROUTER_SCHEMA:
            data = self.route_override or self.classify(user_text)
            return LLMResponse(text=json.dumps(data), input_tokens=50, output_tokens=20)

        if json_schema is ESCALATION_SCHEMA:
            return LLMResponse(text=json.dumps({
                "summary": "Customer needs a human specialist.", "priority": "high", "category": "fraud",
                "user_message": "A specialist will contact you. Protocol: {ticket_id}.",
            }), input_tokens=80, output_tokens=40)

        tool_results = [m for m in history if m.get("role") == "tool"]
        if tools and (self.never_stop or not tool_results):
            name, args = self.tool_plan(user_text, [t["name"] for t in tools])
            if name:
                return LLMResponse(tool_calls=[ToolCall(name, args)], raw_content={"role": "model", "call": name},
                                   input_tokens=100, output_tokens=15)
        payload = json.dumps([r for m in tool_results for r in m["results"]], ensure_ascii=False, default=str)
        return LLMResponse(text=f"Answer based on tools: {payload[:1500]}", input_tokens=200, output_tokens=60)

    # --- scripted behaviour --------------------------------------------------
    @staticmethod
    def classify(text: str) -> dict:
        t = text.lower()
        if any(w in t for w in ("human", "attendant", "fraud", "atendente")):
            route = "escalation"
        elif any(w in t for w in ("my ", "yesterday", "machine won't", "decline", "minha", "meu")):
            route = "support"
        elif any(w in t for w in ("hello", "hi ", "oi", "thanks")) and len(t) < 20:
            route = "greeting"
        elif "poem" in t or "homework" in t:
            route = "out_of_scope"
        else:
            route = "knowledge"
        reply = "Hello! How can I help?" if route == "greeting" else (
            "I can only help with Getnet topics." if route == "out_of_scope" else "")
        return {"route": route, "confidence": 0.9, "language": "en", "reason": "keyword rule", "reply": reply}

    @staticmethod
    def default_tool_plan(text: str, available: list[str]):
        t = text.lower()
        if "weather" in t or "exchange" in t or "euro" in t:
            return "web_search", {"query": text}
        if "get_sales_and_settlements" in available and ("deposit" in t or "money" in t):
            return "get_sales_and_settlements", {"period": "yesterday"}
        if "get_recent_transactions" in available and "decline" in t:
            return "get_recent_transactions", {"status": "declined"}
        if "get_terminal_status" in available and ("connect" in t or "machine" in t):
            return "get_terminal_status", {}
        if "search_knowledge_base" in available:
            return "search_knowledge_base", {"query": text}
        return None, {}


def fake_web_search(query: str) -> list[dict]:
    return [{"title": "Fake result", "url": "https://example.com/result", "snippet": f"Result for {query}"}]


def failing_web_search(query: str) -> list[dict]:
    raise ConnectionError("network down")
