"""One database for all mutable state: conversations, hand-offs, tickets, analytics and rate limits.

Why a single database (and why SQLAlchemy Core)
  * Replicas: with several API containers behind a load balancer, every replica must see the same
    sessions, tickets and rate-limit counters. Local files (a JSON ticket file, per-process counters)
    would split that state. Pointing DATABASE_URL at Postgres makes every replica share it.
  * Same code for development and production: SQLite by default (zero setup, one file on the Docker
    volume), Postgres in production - only the URL changes. SQLAlchemy Core gives portable SQL
    without an ORM.
  * The vector index is read-only at request time, so each replica simply loads it (snapshot/volume).

Tables
  sessions, turns, handoffs      -> conversation memory and human-in-the-loop (app/memory.py)
  tickets                        -> support and hand-off tickets (app/data/customer_repository.py)
  requests                       -> analytics (app/analytics.py)
  rate_limits                    -> shared per-minute counters (app/security.py)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text, create_engine, inspect, select,
                        text)
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

metadata = MetaData()

sessions = Table(
    "sessions", metadata,
    Column("session_id", String(32), primary_key=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("created_at", Float), Column("updated_at", Float),
)

turns = Table(
    "turns", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(32), nullable=False, index=True),
    Column("ts", Float),
    Column("role", String(16), nullable=False),          # user | assistant | human_agent
    Column("text", Text, nullable=False),
    Column("trace_id", String(32)),
    Column("meta", Text),
)

handoffs = Table(
    "handoffs", metadata,
    Column("ticket_id", String(32), primary_key=True),
    Column("session_id", String(32), index=True), Column("user_id", String(64)),
    Column("agent", String(64)), Column("language", String(8)), Column("reason", Text), Column("summary", Text),
    Column("priority", String(16)), Column("category", String(32)),
    Column("status", String(16), index=True),          # open | answered | closed
    Column("created_at", Float), Column("answered_at", Float), Column("human_reply", Text),
    Column("channel_ref", String(64)),                 # e.g. the Telegram message id of the notification
)

tickets = Table(
    "tickets", metadata,
    Column("ticket_id", String(32), primary_key=True),
    Column("user_id", String(64), index=True), Column("kind", String(32)), Column("category", String(32)),
    Column("priority", String(16)), Column("summary", Text), Column("status", String(16)),
    Column("created_at", String(40)),
)

requests = Table(
    "requests", metadata,
    Column("trace_id", String(32), primary_key=True),
    Column("ts", Float, nullable=False, index=True),
    Column("user_id", String(64)), Column("message", Text), Column("question", Text),
    Column("route", String(32)), Column("agent", String(64)), Column("tools", Text),
    Column("escalated", Integer), Column("blocked", String(32)), Column("error", Integer), Column("resolved", Integer),
    Column("latency_ms", Float), Column("tokens", Integer), Column("in_tokens", Integer), Column("out_tokens", Integer),
    Column("feedback", Integer),                       # 1 = 👍, 0 = 👎, NULL = not rated
)

rate_limits = Table(
    "rate_limits", metadata,
    Column("key", String(128), primary_key=True),
    Column("window", Integer, primary_key=True),       # minute bucket
    Column("count", Integer, nullable=False),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # timeout: wait for a concurrent writer instead of failing with "database is locked"
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        metadata.create_all(self.engine)
        self._add_missing_columns()

    @property
    def backend(self) -> str:
        return self.engine.dialect.name   # sqlite | postgresql

    def _add_missing_columns(self) -> None:
        """Tables created by an older version get the new columns in place (history is kept)."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in metadata.sorted_tables:
                existing = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in existing:
                        conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" '
                                          f'{col.type.compile(self.engine.dialect)}'))

    def import_legacy(self, analytics_db: Path | None = None, tickets_file: Path | None = None) -> None:
        """One-time import from the files used by earlier versions (analytics.db, tickets.json)."""
        with self.engine.begin() as conn:
            if analytics_db and analytics_db.exists() and not conn.execute(select(requests.c.trace_id).limit(1)).first():
                try:
                    src = sqlite3.connect(str(analytics_db))
                    src.row_factory = sqlite3.Row
                    rows = [dict(r) for r in src.execute("SELECT * FROM requests")]
                    src.close()
                    names = {c.name for c in requests.columns}
                    rows = [{k: v for k, v in r.items() if k in names} for r in rows]
                    if rows:
                        conn.execute(requests.insert(), rows)
                        logger.info(json.dumps({"event": "legacy_analytics_imported", "rows": len(rows)}))
                except Exception as exc:  # never block startup on a legacy file
                    logger.warning(json.dumps({"event": "legacy_analytics_import_failed", "error": str(exc)[:200]}))
            if tickets_file and tickets_file.exists() and not conn.execute(select(tickets.c.ticket_id).limit(1)).first():
                try:
                    rows = json.loads(tickets_file.read_text(encoding="utf-8"))
                    names = {c.name for c in tickets.columns}
                    rows = [{k: v for k, v in r.items() if k in names} for r in rows]
                    if rows:
                        conn.execute(tickets.insert(), rows)
                        logger.info(json.dumps({"event": "legacy_tickets_imported", "rows": len(rows)}))
                except Exception as exc:
                    logger.warning(json.dumps({"event": "legacy_tickets_import_failed", "error": str(exc)[:200]}))
