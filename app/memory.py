"""Conversation state: sessions, turns and human hand-offs, persisted in the shared database (app/db.py).

Why this exists
  * Memory: turn 2 ("and what about today's?") must know what turn 1 was about. The last
    MEMORY_MAX_TURNS turns of the session are given to the Router and the specialists.
  * Survives restarts and works with several replicas: SQLite on the Docker volume by default,
    Postgres when DATABASE_URL points at it (every replica then sees the same conversations).
  * Human-in-the-loop: a hand-off is linked to the session, so when the human specialist answers,
    the reply is written into the same conversation and the agent continues from there.

Security
  * A session belongs to one merchant: another user_id can't read or continue it (PermissionError).
  * Only already-masked text is stored (the guardrails hide card numbers, CPF, passwords first).
  * Blocked messages (e.g. prompt injection) are stored as a placeholder, so they can't be
    replayed into later prompts through the history.

Scaling: SQLite for one replica, Postgres (DATABASE_URL) for several - same code, see app/db.py.
"""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import select, update

from app.db import Database, handoffs, sessions, turns

BLOCKED_PLACEHOLDER = "[message blocked by the guardrails]"


class ConversationStore:
    def __init__(self, db: Database, max_turns: int = 6, ttl_hours: float = 24.0):
        self.db = db
        self.max_turns = max_turns
        self.ttl_s = ttl_hours * 3600

    # -- sessions -------------------------------------------------------------
    def resolve_session(self, user_id: str, session_id: str | None) -> str:
        """Return a usable session id for this user (creating one when needed)."""
        now = time.time()
        with self.db.engine.begin() as conn:
            if session_id:
                row = conn.execute(select(sessions.c.user_id, sessions.c.updated_at)
                                   .where(sessions.c.session_id == session_id)).first()
                if row is not None:
                    if row.user_id != user_id:
                        raise PermissionError("session belongs to another merchant")
                    if now - row.updated_at <= self.ttl_s:
                        return session_id
            new_id = uuid.uuid4().hex[:16]
            conn.execute(sessions.insert().values(session_id=new_id, user_id=user_id, created_at=now, updated_at=now))
            return new_id

    def owner(self, session_id: str) -> str | None:
        with self.db.engine.connect() as conn:
            return conn.execute(select(sessions.c.user_id).where(sessions.c.session_id == session_id)).scalar()

    # -- turns ----------------------------------------------------------------
    def add_turn(self, session_id: str, role: str, text: str, trace_id: str | None = None,
                 meta: dict | None = None) -> None:
        now = time.time()
        with self.db.engine.begin() as conn:
            conn.execute(turns.insert().values(session_id=session_id, ts=now, role=role, text=text[:4000],
                                               trace_id=trace_id, meta=json.dumps(meta or {})))
            conn.execute(update(sessions).where(sessions.c.session_id == session_id).values(updated_at=now))

    @staticmethod
    def _turn(r) -> dict:
        return {"role": r.role, "text": r.text, "ts": r.ts, "trace_id": r.trace_id, "meta": json.loads(r.meta or "{}")}

    def history(self, session_id: str, limit: int | None = None) -> list[dict]:
        """Most recent turns, oldest first."""
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(turns).where(turns.c.session_id == session_id)
                                .order_by(turns.c.id.desc()).limit(limit or self.max_turns)).all()
        return [self._turn(r) for r in reversed(rows)]

    def messages(self, session_id: str, after_ts: float = 0.0) -> list[dict]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(turns).where(turns.c.session_id == session_id, turns.c.ts > after_ts)
                                .order_by(turns.c.id)).all()
        return [{k: v for k, v in self._turn(r).items() if k != "trace_id"} for r in rows]

    # -- human hand-offs ------------------------------------------------------
    def create_handoff(self, *, ticket_id: str, session_id: str, user_id: str, agent: str, language: str,
                       reason: str, summary: str, priority: str, category: str) -> None:
        with self.db.engine.begin() as conn:
            if conn.execute(select(handoffs.c.ticket_id).where(handoffs.c.ticket_id == ticket_id)).first():
                return
            conn.execute(handoffs.insert().values(
                ticket_id=ticket_id, session_id=session_id, user_id=user_id, agent=agent, language=language,
                reason=reason, summary=summary, priority=priority, category=category, status="open",
                created_at=time.time()))

    def open_handoff_for(self, session_id: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(handoffs).where(handoffs.c.session_id == session_id, handoffs.c.status == "open")
                               .order_by(handoffs.c.created_at.desc()).limit(1)).first()
        return dict(row._mapping) if row else None

    def get_handoff(self, ticket_id: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(handoffs).where(handoffs.c.ticket_id == ticket_id)).first()
        return dict(row._mapping) if row else None

    def find_handoff_by_channel_ref(self, ref: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(handoffs).where(handoffs.c.channel_ref == ref)).first()
        return dict(row._mapping) if row else None

    def set_channel_ref(self, ticket_id: str, ref: str) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(update(handoffs).where(handoffs.c.ticket_id == ticket_id).values(channel_ref=ref))

    def list_handoffs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        q = select(handoffs).order_by(handoffs.c.created_at.desc()).limit(limit)
        if status:
            q = q.where(handoffs.c.status == status)
        with self.db.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def answer_handoff(self, ticket_id: str, reply: str, close: bool) -> bool:
        """Mark answered. Returns False if it was not open anymore (another replica/agent got there first)."""
        with self.db.engine.begin() as conn:
            res = conn.execute(update(handoffs).where(handoffs.c.ticket_id == ticket_id, handoffs.c.status == "open")
                               .values(status="closed" if close else "answered", answered_at=time.time(),
                                       human_reply=reply[:4000]))
            return res.rowcount == 1
