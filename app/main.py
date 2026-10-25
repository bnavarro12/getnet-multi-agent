"""FastAPI entry point.

Customer endpoints (API key: `Authorization: Bearer <key>`; the key decides the user_id)
  GET  /                         -> web console to try the assistant and inspect each answer's agent path and trace
  POST /chat                     -> {"message": "...", "user_id": "...", "session_id": "..."(optional)}
  GET  /sessions/{id}/messages   -> conversation of a session (the console polls it for human replies)
  POST /feedback                 -> {"trace_id": "...", "rating": "up" | "down"} from the console's 👍/👎 buttons

Staff endpoints (header `X-Admin-Key: <ADMIN_API_KEY>`)
  GET  /handoffs                 -> human hand-off queue (with each conversation)
  POST /handoffs/{ticket}/reply  -> human specialist answers; the agent continues the conversation
  GET  /analytics                -> the dashboard's data (JSON); ?days=7 limits the window
  GET  /evals                    -> latest evaluation report written by `python -m evals.run_evals`
  GET  /metrics                  -> operational counters (requests, routes, latency p50/p95/p99, tokens, cost)
  POST /admin/reindex            -> rebuild the knowledge base in the background

Public
  GET  /health                   -> liveness/readiness (index loaded? how old?)
  GET  /dashboard, /agent        -> static pages (they ask for the staff key)
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Literal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.analytics import AnalyticsStore
from app.config import get_settings
from app.data.customer_repository import CustomerRepository
from app.guardrails import mask_sensitive
from app.db import Database
from app.memory import ConversationStore
from app.observability import Metrics, setup_logging
from app.orchestrator import Orchestrator, Services
from app.rag.retriever import Retriever
from app.rag.store import VectorStore
from app.security import ApiKeyStore, DbRateLimiter, require_staff, resolve_user
from app.tools.knowledge_tools import ddg_web_search

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, examples=["What's the difference between the Get Clássica and the Get Smart?"])
    user_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$", examples=["cliente1988"])
    session_id: str | None = Field(None, max_length=64, pattern=r"^[a-f0-9]+$",
                                   description="Continue a conversation (returned by the previous /chat response)")


class FeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=64)
    rating: str = Field(..., pattern=r"^(up|down)$")


class HumanReply(BaseModel):
    reply: str = Field("", max_length=4000)
    close: bool = Field(False, description="true = case solved (closed); false = answered, may continue")
    decision: Literal["approve", "reject"] | None = Field(None, description="required for approval cases (APR-)")


def index_age_days(store: VectorStore) -> float | None:
    built = store.meta.get("built_at")
    if not built:
        return None
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(built)).total_seconds() / 86400, 2)


def load_store(settings, llm) -> VectorStore:
    """Index on the volume -> committed snapshot -> live crawl (in that order)."""
    if settings.index_path.exists():
        return VectorStore.load(settings.index_path)
    if settings.index_snapshot_path.exists():
        store = VectorStore.load(settings.index_snapshot_path)
        store.save(settings.index_path)
        logger.info(json.dumps({"event": "index_from_snapshot", "chunks": len(store)}))
        return store
    if settings.auto_ingest:
        from app.rag.ingest import build_index
        logger.info(json.dumps({"event": "index_missing_building_now"}))
        try:
            return build_index(settings, llm)
        except Exception as exc:  # start in degraded mode instead of crashing
            logger.error(json.dumps({"event": "ingest_failed", "error": str(exc)[:300]}))
    return VectorStore()


_reindex_lock = threading.Lock()


def refresh_index(orch: Orchestrator, settings) -> bool:
    """Rebuild the index and swap it in atomically. A failed/empty crawl keeps the old index."""
    if not _reindex_lock.acquire(blocking=False):
        return False  # already running
    try:
        from app.rag.ingest import build_index
        new = build_index(settings, orch.services.llm)
        if len(new):
            orch.services.retriever.replace_store(new)
            logger.info(json.dumps({"event": "index_refreshed", "chunks": len(new)}))
        return True
    except Exception as exc:
        logger.error(json.dumps({"event": "index_refresh_failed", "error": str(exc)[:300]}))
        return False
    finally:
        _reindex_lock.release()


def build_orchestrator(settings=None, llm=None, web_search=None) -> Orchestrator:
    """Wire all dependencies. Tests call this with fakes; production with the real ones."""
    settings = settings or get_settings()
    if llm is None:
        from app.llm import GeminiClient
        llm = GeminiClient(settings)

    store = load_store(settings, llm)
    logger.info(json.dumps({"event": "index_loaded", "chunks": len(store), "meta": {k: v for k, v in store.meta.items() if k != "sources"}}))

    db = Database(settings.db_url)
    db.import_legacy(settings.analytics_db_path, settings.tickets_file)
    logger.info(json.dumps({"event": "database_ready", "backend": db.backend}))
    services = Services(
        llm=llm,
        repo=CustomerRepository(settings.customers_file, db),
        retriever=Retriever(store, llm, settings),
        web_search=web_search or ddg_web_search,
    )
    memory = ConversationStore(db, settings.memory_max_turns, settings.session_ttl_hours)
    orch = Orchestrator(services, settings, Metrics(settings.price_input_per_mtok_usd, settings.price_output_per_mtok_usd),
                        memory=memory)
    orch.db = db
    return orch


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    if not getattr(app.state, "orchestrator", None):  # tests may inject one
        app.state.orchestrator = build_orchestrator(settings)
        age = index_age_days(app.state.orchestrator.services.retriever.store)
        if settings.index_max_age_days and age is not None and age > settings.index_max_age_days:
            logger.info(json.dumps({"event": "index_stale_refreshing", "age_days": age}))
            threading.Thread(target=refresh_index, args=(app.state.orchestrator, settings), daemon=True).start()
    settings = app.state.orchestrator.s
    if not getattr(app.state, "analytics", None):
        app.state.analytics = AnalyticsStore(app.state.orchestrator.db)
    if not getattr(app.state, "telegram", None):
        from app.telegram import bridge_from_settings
        bridge = bridge_from_settings(settings)
        if bridge is not None:
            orch = app.state.orchestrator
            orch.on_handoff = lambda h: bridge.notify_handoff(orch.memory, h)
            orch.on_overdue = bridge.notify_overdue
            if settings.telegram_polling:
                bridge.start(orch)
            app.state.telegram = bridge
    if not getattr(app.state, "api_keys", None):
        app.state.api_keys = ApiKeyStore(settings.api_keys_file)
    if not getattr(app.state, "rate_limiter", None):
        app.state.rate_limiter = DbRateLimiter(app.state.orchestrator.db, settings.rate_limit_per_minute,
                                               fail_open=settings.rate_limit_fail_open)
    if not getattr(app.state, "staff_keys", None):
        app.state.staff_keys = ApiKeyStore(settings.staff_keys_file)
    if settings.sla_check_interval_s > 0 and app.state.orchestrator.memory is not None:
        threading.Thread(target=sla_loop, args=(app.state.orchestrator, settings.sla_check_interval_s),
                         daemon=True, name="sla-check").start()
    yield


def sla_loop(orch: Orchestrator, interval_s: float) -> None:
    """Background check: open cases past their response-time target are re-escalated (see check_sla)."""
    while True:
        time.sleep(interval_s)
        try:
            orch.check_sla()
        except Exception:
            logger.exception("sla_check_failed")


app = FastAPI(
    title="Getnet Multi-Agent Support System",
    version="3.0.0",
    description=("Router + planner -> Knowledge (hybrid RAG + web) / Customer Support (tools) -> Supervisor synthesis "
                 "-> Human hand-off with reply loop; conversation memory, guardrails, auth and tracing."),
    lifespan=lifespan,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def console() -> FileResponse:
    """Small web console: chat + agent path, tools, sources and trace for each answer."""
    return FileResponse(STATIC_DIR / "index.html")


def _settings(request: Request):
    """Settings of the running orchestrator (tests inject their own)."""
    orch = getattr(request.app.state, "orchestrator", None)
    return orch.s if orch is not None else get_settings()


def _client_key(request: Request) -> str:
    """Client IP for the rate limiter. Behind the load balancer, X-Forwarded-For is
    "<what the client sent>, <the IP nginx saw>": only the LAST hop was written by our proxy, so the
    first entries are attacker-controlled and would let anyone bypass the per-IP limit by rotating them."""
    if _settings(request).trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> dict:
    settings = _settings(request)
    request.app.state.rate_limiter.check(f"ip:{_client_key(request)}")
    user_id = resolve_user(request, settings, request.app.state.api_keys, req.user_id)
    request.app.state.rate_limiter.check(f"user:{user_id}")
    orchestrator: Orchestrator = request.app.state.orchestrator
    # Agents are synchronous (blocking HTTP calls to the LLM); run them off the event loop.
    try:
        result = await run_in_threadpool(orchestrator.handle, req.message, user_id, req.session_id)
    except PermissionError:
        raise HTTPException(403, "This session belongs to another merchant.")
    analytics: AnalyticsStore | None = getattr(request.app.state, "analytics", None)
    if analytics is not None:
        try:
            analytics.record(mask_sensitive(req.message)[0], result)  # never store raw sensitive data
        except Exception:  # analytics must never break the chat
            logger.exception("analytics_record_failed")
    return result


@app.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, request: Request, after: float = 0.0) -> dict:
    settings = _settings(request)
    user_id = resolve_user(request, settings, request.app.state.api_keys, None) if settings.auth_enabled else None
    memory = request.app.state.orchestrator.memory
    if memory is None or memory.owner(session_id) is None:
        raise HTTPException(404, "unknown session")
    if settings.auth_enabled and memory.owner(session_id) != user_id:
        raise HTTPException(403, "This session belongs to another merchant.")
    handoff = memory.open_handoff_for(session_id)
    return {"session_id": session_id, "messages": memory.messages(session_id, after),
            "open_handoff": handoff["ticket_id"] if handoff else None, "now": time.time()}


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request) -> dict:
    settings = _settings(request)
    user_id = resolve_user(request, settings, request.app.state.api_keys, None) if settings.auth_enabled else None
    request.app.state.rate_limiter.check(f"fb:{user_id or _client_key(request)}")
    if not request.app.state.analytics.feedback(req.trace_id, req.rating == "up", user_id=user_id):
        raise HTTPException(404, "unknown trace_id")
    return {"ok": True}


# -- human specialists (staff) ----------------------------------------------------
@app.get("/handoffs")
async def list_handoffs(request: Request, status: str | None = "open") -> dict:
    require_staff(request, _settings(request), request.app.state.staff_keys)
    memory = request.app.state.orchestrator.memory
    items = memory.list_handoffs(status or None) if memory else []
    for h in items:
        h["conversation"] = memory.messages(h["session_id"])
    return {"handoffs": items}


@app.post("/handoffs/{ticket_id}/reply")
async def reply_handoff(ticket_id: str, body: HumanReply, request: Request) -> dict:
    operator = require_staff(request, _settings(request), request.app.state.staff_keys)  # audit: who answered
    orch: Orchestrator = request.app.state.orchestrator
    try:
        return await run_in_threadpool(orch.resume_after_human, ticket_id, body.reply, body.close, operator,
                                       body.decision)
    except KeyError:
        raise HTTPException(404, "unknown ticket")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/agent", include_in_schema=False)
async def agent_console() -> FileResponse:
    """Inbox for human specialists: open hand-offs, the conversation, and a reply box."""
    return FileResponse(STATIC_DIR / "agent.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/analytics")
async def analytics_summary(request: Request, days: float | None = None) -> dict:
    require_staff(request, _settings(request), request.app.state.staff_keys)
    summary = request.app.state.analytics.summary(days=days)
    m = request.app.state.orchestrator.metrics
    k = summary["kpis"]
    k["estimated_cost_usd_per_request"] = round(m.cost_usd(k.get("avg_input_tokens", 0), k.get("avg_output_tokens", 0)), 6)
    return summary


@app.get("/evals")
async def evals_report(request: Request) -> dict:
    require_staff(request, _settings(request), request.app.state.staff_keys)
    path = _settings(request).eval_report_path
    if not path.exists():
        return {"available": False}
    return {"available": True, **json.loads(path.read_text(encoding="utf-8"))}


@app.post("/admin/reindex")
async def reindex(request: Request) -> dict:
    require_staff(request, _settings(request), request.app.state.staff_keys)
    threading.Thread(target=refresh_index, args=(request.app.state.orchestrator, _settings(request)), daemon=True).start()
    return {"started": True, "note": "Watch the logs for index_refreshed; the old index keeps serving meanwhile."}


@app.get("/health")
async def health(request: Request) -> dict:
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(503, "starting")
    store = orch.services.retriever.store
    age = index_age_days(store)
    max_age = _settings(request).index_max_age_days
    breaker = getattr(orch.services.llm, "breaker", None)
    return {"status": "ok" if len(store) else "degraded", "knowledge_base_chunks": len(store),
            "index_built_at": store.meta.get("built_at"), "index_age_days": age,
            "index_stale": bool(max_age and age is not None and age > max_age),
            "llm_circuit": breaker.state if breaker else "n/a",
            "database": orch.db.backend if getattr(orch, "db", None) else "n/a",
            "replica": socket.gethostname()}


@app.get("/metrics")
async def metrics(request: Request) -> dict:
    require_staff(request, _settings(request), request.app.state.staff_keys)
    return request.app.state.orchestrator.metrics.snapshot()
