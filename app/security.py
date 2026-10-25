"""Identity boundary, staff access and rate limiting for the HTTP API.

Identity (two ways in, same result: a user_id the LLM never chooses)
  * OIDC / JWT: `Authorization: Bearer <token>` from the company's login (e.g. Keycloak, Cognito, Entra).
    The signature is verified (JWKS URL or a shared secret), plus expiry, issuer and audience;
    the token's `sub` claim is the user_id. Expiry and rotation are handled by the identity provider.
  * API keys (demo / machine clients): stored hashed (sha256) in data/api_keys.json, each with an
    optional `expires_at` and `revoked` flag, so a key can be rotated (issue a new one, revoke the old).
    `python -m app.security new-key <user_id> --days 90` prints a new key and its file entry.
  The `user_id` in the body must match the identity, otherwise 403.

Staff
  Operational and human-specialist endpoints require a staff key: ADMIN_API_KEY, or a personal key per
  operator in data/staff_keys.json. The key identifies WHO acted, and that name is written on the case
  (audit). Compared in constant time; with no key configured those endpoints are disabled.

Rate limiting
  Per-minute counters per merchant and per client IP, stored in the shared database (DbRateLimiter), so
  every replica enforces the same limit. RateLimiter (in-memory sliding window) is kept for tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Request


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyStore:
    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self._entries: dict[str, dict] = data.get("keys", {})

    def user_for(self, key: str | None) -> str | None:
        entry = self._entries.get(hash_key(key)) if key else None
        if not entry or entry.get("revoked"):
            return None
        expires = entry.get("expires_at")
        if expires and datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            return None  # expired: the merchant must use the rotated key
        return entry.get("user_id") or entry.get("operator")


def jwt_user(token: str, settings) -> str | None:
    """Verify an OIDC/JWT access token; return its subject (the user_id) or None."""
    if not (settings.auth_jwt_jwks_url or settings.auth_jwt_secret) or token.count(".") != 2:
        return None
    import jwt
    try:
        if settings.auth_jwt_jwks_url:
            key = jwt.PyJWKClient(settings.auth_jwt_jwks_url).get_signing_key_from_jwt(token).key
            algorithms = ["RS256", "ES256"]
        else:
            key, algorithms = settings.auth_jwt_secret, ["HS256"]
        claims = jwt.decode(token, key, algorithms=algorithms, audience=settings.auth_jwt_audience or None,
                            issuer=settings.auth_jwt_issuer or None, options={"require": ["exp", "sub"]})
        return str(claims["sub"])
    except jwt.PyJWTError:
        return None


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
    token = bearer_token(request) or ""
    user_id = jwt_user(token, settings) or keys.user_for(token)
    if user_id is None:
        raise HTTPException(401, "Missing, invalid or expired credentials (send 'Authorization: Bearer <token or key>').",
                            headers={"WWW-Authenticate": "Bearer"})
    if claimed_user_id and claimed_user_id != user_id:
        raise HTTPException(403, "user_id does not match the authenticated merchant.")
    return user_id


def require_staff(request: Request, settings, staff: ApiKeyStore | None = None) -> str:
    """Check the staff key and return WHO is acting (written on the case for auditing)."""
    given = request.headers.get("X-Admin-Key") or ""
    expected = settings.admin_api_key
    if expected and hmac.compare_digest(given.encode(), expected.encode()):
        return "admin"
    operator = staff.user_for(given) if staff and given else None
    if operator:
        return operator
    if not expected and not (staff and staff._entries):
        raise HTTPException(403, "Staff endpoints are disabled: set ADMIN_API_KEY in .env.")
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


class DbRateLimiter:
    """Fixed one-minute windows counted in the shared database (works across replicas)."""

    def __init__(self, db, per_minute: int, fail_open: bool = False):
        self.db = db
        self.limit = per_minute
        self.fail_open = fail_open   # default: if the counter can't be updated, refuse (503) instead of letting it through

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        from sqlalchemy import delete, select, update
        from sqlalchemy.exc import IntegrityError

        from app.db import rate_limits as rl
        now = time.time()
        window = int(now // 60)
        key = key[:128]
        for _ in range(3):  # a concurrent first insert in another replica -> retry as an update
            try:
                with self.db.engine.begin() as conn:
                    res = conn.execute(update(rl).where(rl.c.key == key, rl.c.window == window)
                                       .values(count=rl.c.count + 1))
                    if res.rowcount == 0:
                        conn.execute(rl.insert().values(key=key, window=window, count=1))
                        conn.execute(delete(rl).where(rl.c.window < window - 2))  # housekeeping
                    count = conn.execute(select(rl.c.count).where(rl.c.key == key, rl.c.window == window)).scalar()
                break
            except IntegrityError:
                continue
        else:
            if self.fail_open:
                return
            raise HTTPException(503, "Rate limiter unavailable, try again.", headers={"Retry-After": "2"})
        if count > self.limit:
            retry = max(1, int(60 - (now % 60)) + 1)
            raise HTTPException(429, "Too many requests, please slow down.", headers={"Retry-After": str(retry)})


if __name__ == "__main__":  # python -m app.security new-key cliente1988 --days 90
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["new-key"])
    ap.add_argument("user_id")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    key = "gk_" + secrets.token_urlsafe(24)
    entry = {"user_id": args.user_id,
             "expires_at": (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat(timespec="seconds")}
    print(f"New key (give it to the client, it is shown only once): {key}")
    print("Add to data/api_keys.json under \"keys\" (then set \"revoked\": true on the old one):")
    print(json.dumps({hash_key(key): entry}, indent=2))
