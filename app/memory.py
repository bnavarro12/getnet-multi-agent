"""Conversation state: sessions, turns and human hand-offs, persisted in SQLite.

Why this exists
  * Memory: turn 2 ("and what about today's?") must know what turn 1 was about. The last
    MEMORY_MAX_TURNS turns of the session are given to the Router and the specialists.
  * Survives restarts: the database lives on the Docker volume (storage/state.db), so a
    `docker compose restart` does not lose conversations.
  * Human-in-the-loop: a hand-off is linked to the session, so when the human specialist answers,
    the reply is written into the same conversation and the agent continues from there.

Security
  * A session belongs to one merchant: another user_id can't read or continue it (PermissionError).
  * Only already-masked text is stored (the guardrails hide card numbers, CPF, passwords first).
  * Blocked messages (e.g. prompt injection) are stored as a placeholder, so they can't be
    replayed into later prompts through the history.

Scaling note: SQLite is right for one replica. With several replicas this moves to Postgres or
Redis - the interface (methods below) stays the same.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BLOCKED_PLACEHOLDER = "[message blocked by the guardrails]"


class ConversationStore:
    def __init__(self, db_path: Path, max_turns: int = 6, ttl_hours: float = 24.0):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns
        self.ttl_s = ttl_hours * 3600
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL, updated_at REAL);
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts REAL,
                role TEXT NOT NULL,              -- user | assistant | human_agent
                text TEXT NOT NULL, trace_id TEXT, meta TEXT);
            CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, id);
            CREATE TABLE IF NOT EXISTS handoffs (
                ticket_id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, agent TEXT, language TEXT,
                reason TEXT, summary TEXT, priority TEXT, category TEXT,
                status TEXT,                     -- open | answered | closed
                created_at REAL, answered_at REAL, human_reply TEXT);
        """)
        self._db.commit()

    # -- sessions -------------------------------------------------------------
    def resolve_session(self, user_id: str, session_id: str | None) -> str:
        """Return a usable session id for this user (creating one when needed)."""
        now = time.time()
        with self._lock:
            if session_id:
                row = self._db.execute("SELECT user_id, updated_at FROM sessions WHERE session_id = ?",
                                       (session_id,)).fetchone()
                if row is not None:
                    if row["user_id"] != user_id:
                        raise PermissionError("session belongs to another merchant")
                    if now - row["updated_at"] <= self.ttl_s:
                        return session_id
            new_id = uuid.uuid4().hex[:16]
            self._db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (new_id, user_id, now, now))
            self._db.commit()
            return new_id

    def owner(self, session_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row["user_id"] if row else None

    # -- turns ----------------------------------------------------------------
    def add_turn(self, session_id: str, role: str, text: str, trace_id: str | None = None,
                 meta: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO turns (session_id, ts, role, text, trace_id, meta) VALUES (?,?,?,?,?,?)",
                             (session_id, now, role, text[:4000], trace_id, json.dumps(meta or {})))
            self._db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
            self._db.commit()

    def history(self, session_id: str, limit: int | None = None) -> list[dict]:
        """Most recent turns, oldest first."""
        limit = limit or self.max_turns
        with self._lock:
            rows = self._db.execute(
                "SELECT role, text, ts, trace_id, meta FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        return [{"role": r["role"], "text": r["text"], "ts": r["ts"], "trace_id": r["trace_id"],
                 "meta": json.loads(r["meta"] or "{}")} for r in reversed(rows)]

    def messages(self, session_id: str, after_ts: float = 0.0) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, text, ts, meta FROM turns WHERE session_id = ? AND ts > ? ORDER BY id",
                (session_id, after_ts)).fetchall()
        return [{"role": r["role"], "text": r["text"], "ts": r["ts"], "meta": json.loads(r["meta"] or "{}")}
                for r in rows]

    # -- human hand-offs ------------------------------------------------------
    def create_handoff(self, *, ticket_id: str, session_id: str, user_id: str, agent: str, language: str,
                       reason: str, summary: str, priority: str, category: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, session_id, user_id, agent, language, reason, summary, priority, category,
                 "open", time.time(), None, None))
            self._db.commit()

    def open_handoff_for(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM handoffs WHERE session_id = ? AND status = 'open' "
                                   "ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row) if row else None

    def get_handoff(self, ticket_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM handoffs WHERE ticket_id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def list_handoffs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            if status:
                rows = self._db.execute("SELECT * FROM handoffs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                                        (status, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM handoffs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def answer_handoff(self, ticket_id: str, reply: str, close: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE handoffs SET status = ?, answered_at = ?, human_reply = ? WHERE ticket_id = ?",
                             ("closed" if close else "answered", time.time(), reply[:4000], ticket_id))
            self._db.commit()
