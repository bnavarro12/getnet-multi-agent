"""Circuit breaker for the LLM provider.

Retries handle short glitches. When the provider is really down, retrying every request makes
things worse: each user waits ~2 minutes and then fails anyway. After `failures` consecutive
errors the breaker OPENS and calls fail immediately for `cooldown_s`; then one trial call is let
through (HALF-OPEN) - success closes it again, failure re-opens it.
"""

from __future__ import annotations

import threading
import time


class CircuitOpenError(RuntimeError):
    """Raised without calling the provider while the circuit is open."""


class CircuitBreaker:
    def __init__(self, failures: int = 5, cooldown_s: float = 30.0, clock=time.monotonic):
        self.threshold = failures
        self.cooldown = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half_open" if self._clock() - self._opened_at >= self.cooldown else "open"

    def call(self, fn):
        if self.state == "open":
            raise CircuitOpenError("LLM provider circuit is open (too many consecutive failures)")
        try:
            result = fn()
        except Exception:
            with self._lock:
                self._consecutive += 1
                if self._consecutive >= self.threshold or self._opened_at is not None:
                    self._opened_at = self._clock()
            raise
        with self._lock:
            self._consecutive = 0
            self._opened_at = None
        return result
