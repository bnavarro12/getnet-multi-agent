"""Thin abstraction over the LLM provider.

Every agent talks to an `LLMClient`, never to the Gemini SDK directly. That keeps the
agents provider-agnostic and lets the test suite swap in a scripted fake (see
tests/fakes.py) so tests run offline, fast and deterministically.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.resilience import CircuitBreaker

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: Any = None          # provider message, re-sent as-is to keep context
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def chat(
        self,
        *,
        system: str,
        history: list[Any],
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse: ...

    def user_message(self, text: str) -> Any: ...

    def tool_results_message(self, results: list[tuple[str, dict]]) -> Any: ...

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...


class GeminiClient:
    """LLMClient implementation backed by the Google Gen AI SDK."""

    def __init__(self, settings: Settings):
        from google import genai
        from google.genai import types

        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        self._types = types
        self._s = settings
        self._no_thinking_models: set[str] = set()
        self.breaker = CircuitBreaker(settings.circuit_breaker_failures, settings.circuit_breaker_cooldown_s)
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.llm_timeout_s * 1000),
        )

    # -- message builders ---------------------------------------------------
    def user_message(self, text: str):
        t = self._types
        return t.Content(role="user", parts=[t.Part.from_text(text=text)])

    def tool_results_message(self, results: list[tuple[str, dict]]):
        t = self._types
        parts = [t.Part.from_function_response(name=name, response=payload) for name, payload in results]
        return t.Content(role="user", parts=parts)

    # -- generation -----------------------------------------------------------
    def chat(self, *, system, history, tools=None, json_schema=None, model=None) -> LLMResponse:
        return self.breaker.call(lambda: self._chat(system, history, tools, json_schema, model))

    def _chat(self, system, history, tools, json_schema, model) -> LLMResponse:
        t = self._types
        config = t.GenerateContentConfig(
            system_instruction=system,
            temperature=self._s.llm_temperature,
            # We run the tool loop ourselves (app/agents/base.py) so every call is traced.
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        if tools:
            config.tools = [t.Tool(function_declarations=[
                t.FunctionDeclaration(name=x["name"], description=x["description"],
                                      # tools without arguments are declared without a schema
                                      parameters_json_schema=x["parameters"] if x["parameters"].get("properties") else None)
                for x in tools
            ])]
        if json_schema:
            config.response_mime_type = "application/json"
            config.response_json_schema = json_schema
        target = model or self._s.llm_model
        if self._s.llm_thinking_level and target not in self._no_thinking_models:
            config.thinking_config = t.ThinkingConfig(thinking_level=self._s.llm_thinking_level.upper())

        try:
            response = self._with_retries(
                lambda: self._client.models.generate_content(model=target, contents=history, config=config)
            )
        except Exception as exc:
            # Some models don't accept a thinking level: remember that and retry without it.
            if config.thinking_config is not None and "400" in str(exc) and "think" in str(exc).lower():
                self._no_thinking_models.add(target)
                config.thinking_config = None
                response = self._with_retries(
                    lambda: self._client.models.generate_content(model=target, contents=history, config=config)
                )
            else:
                raise

        content = response.candidates[0].content if response.candidates else None
        calls = [ToolCall(name=fc.name, args=dict(fc.args or {})) for fc in (response.function_calls or [])]
        usage = response.usage_metadata
        return LLMResponse(
            text=(response.text or "") if not calls else "",
            tool_calls=calls,
            raw_content=content,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        )

    # -- embeddings -----------------------------------------------------------
    def embed(self, texts, *, is_query=False):
        """Embed texts in small, paced batches.

        Free-tier quotas are per minute (requests AND tokens), so bulk ingestion sends small
        batches with a pause between them, and waits out 429s instead of failing.
        """
        t = self._types
        size = self._s.embed_batch_size
        out: list[list[float]] = []
        for i in range(0, len(texts), size):
            batch = texts[i:i + size]
            result = self._with_retries(
                lambda b=batch: self._client.models.embed_content(
                    model=self._s.embedding_model,
                    contents=b,
                    config=t.EmbedContentConfig(
                        task_type="RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT",
                        output_dimensionality=self._s.embedding_dim,
                    ),
                ),
                retries=1 if is_query else self._s.embed_max_retries,
            )
            out.extend(e.values for e in result.embeddings)
            if not is_query and len(texts) > size:
                logger.info(json.dumps({"event": "embedding_progress", "done": len(out), "total": len(texts)}))
                if len(out) < len(texts):
                    time.sleep(self._s.embed_batch_delay_s)
        return out

    # -- reliability ----------------------------------------------------------
    def _with_retries(self, fn, retries: int | None = None):
        """Retry transient errors (rate limits, 5xx, timeouts) with exponential backoff.

        Rate limits (429) are per minute on the free tier, so they get a longer wait.
        """
        retries = retries or self._s.llm_max_retries
        delay = 2.0
        for attempt in range(1, retries + 1):
            try:
                return fn()
            except Exception as exc:  # SDK raises several error types
                msg = f"{type(exc).__name__}: {exc}"  # the type name catches ReadTimeout & co.
                rate_limited = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                transient = rate_limited or any(c in msg for c in ("500", "502", "503", "504", "timeout", "Timeout", "timed out"))
                if not transient or attempt == retries or "PerDay" in msg:  # a daily quota won't recover by waiting
                    raise
                wait = min(max(delay, 20.0), 65.0) if rate_limited else delay
                logger.warning(json.dumps({"event": "llm_retry", "attempt": attempt, "wait_s": wait, "error": msg[:160]}))
                time.sleep(wait)
                delay = wait * 2
