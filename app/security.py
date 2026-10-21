"""Identity boundary, staff access and rate limiting for the HTTP API.

Identity
  The merchant is identified by an API key sent as `Authorization: Bearer <key>` (or `X-API-Key`).
  The server maps the key to a user_id; the `user_id` in the body must match it, otherwise 403.
  So swapping the user_id in the body no longer reads another merchant's data.
  Keys are stored hashed (sha256) in data/api_keys.json - the file never holds a usable key.
  In production this is where an OAuth2/OIDC token from Getnet's login would be validated
  (the token's subject claim becomes the user_id); the rest of the system does not change.

Staff
  Operational and human-specialist endpoints require the staff key (ADMIN_API_KEY), compared in
  constant time. With no key configured those endpoints are disabled.

Rate limiting
  Sliding window per merchant (or client IP). In-memory, so it is per process: with several
  replicas it would move to Redis or to the API gateway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyStore:
    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self._by_hash: dict[str, str] = {h: v["user_id"] for h, v in data.get("keys", {}).items()}

    def user_for(self, key: str | None) -> str | None:
        if not key:
            return None
        return self._by_hash.get(hash_key(key))


def bearer_token(request: Request, header: str = "X-API-Key") -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get(header)


def resolve_user(request: Request, settings, keys: ApiKeyStore, claimed_user_id: str | None) -> str:
    """Return the user_id this request may act as, or raise 401/403."""
    if not settings.auth_enabled:
        if not claimed_user_id:
            raise HTTPException(422, "user_id is required")
        return claimed_user_id
    user_id = keys.user_for(bearer_token(request))
    if user_id is None:
        raise HTTPException(401, "Missing or invalid API key (send 'Authorization: Bearer <key>').",
                            headers={"WWW-Authenticate": "Bearer"})
    if claimed_user_id and claimed_user_id != user_id:
        raise HTTPException(403, "user_id does not match the authenticated merchant.")
    return user_id


def require_staff(request: Request, settings) -> None:
    expected = settings.admin_api_key
    if not expected:
        raise HTTPException(403, "Staff endpoints are disabled: set ADMIN_API_KEY in .env.")
    given = request.headers.get("X-Admin-Key") or ""
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid staff key (X-Admin-Key header).")


class RateLimiter:
    def __init__(self, per_minute: int, window_s: float = 60.0):
        self.limit = per_minute
        self.window = window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, int(self.window - (now - q[0])) + 1)
                raise HTTPException(429, "Too many requests, please slow down.", headers={"Retry-After": str(retry)})
            q.append(now)
