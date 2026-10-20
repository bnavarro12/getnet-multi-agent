"""FastAPI entry point.

Endpoints
  GET  /         -> web console to try the assistant and inspect each answer's agent path and trace
  POST /chat    -> main endpoint: {"message": "...", "user_id": "..."}
  GET  /health   -> liveness/readiness (index loaded? how many chunks?)
  GET  /metrics  -> operational counters (requests, routes, escalations, latency p50/p95, tokens)
  GET  /dashboard -> analytics dashboard: most asked questions, success rates, routes, evaluation results
  GET  /analytics -> the dashboard's data (JSON); ?days=7 limits the window
  GET  /evals     -> latest evaluation report written by `python -m evals.run_evals`
  POST /feedback  -> {"trace_id": "...", "rating": "up" | "down"} from the console's 👍/👎 buttons
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.analytics import AnalyticsStore
from app.guardrails import mask_sensitive

from app.config import get_settings
from app.data.customer_repository import CustomerRepository
from app.observability import Metrics, setup_logging
from app.orchestrator import Orchestrator, Services
from app.rag.retriever import Retriever
from app.rag.store import VectorStore
from app.tools.knowledge_tools import ddg_web_search

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, examples=["What's the difference between the Get Clássica and the Get Smart?"])
    user_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$", examples=["cliente1988"])


class FeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=64)
    rating: str = Field(..., pattern=r"^(up|down)$")


def build_orchestrator(settings=None, llm=None, web_search=None) -> Orchestrator:
    """Wire all dependencies. Tests call this with fakes; production with the real ones."""
    settings = settings or get_settings()
    if llm is None:
        from app.llm import GeminiClient
        llm = GeminiClient(settings)

    if settings.index_path.exists():
        store = VectorStore.load(settings.index_path)
    elif settings.auto_ingest:
        from app.rag.ingest import build_index
        logger.info(json.dumps({"event": "index_missing_building_now"}))
        try:
            store = build_index(settings, llm)
        except Exception as exc:  # start in degraded mode instead of crashing
            logger.error(json.dumps({"event": "ingest_failed", "error": str(exc)[:300]}))
            store = VectorStore()
    else:
        store = VectorStore()
    logger.info(json.dumps({"event": "index_loaded", "chunks": len(store), "meta": {k: v for k, v in store.meta.items() if k != "sources"}}))

    services = Services(
        llm=llm,
        repo=CustomerRepository(settings.customers_file, settings.tickets_file),
        retriever=Retriever(store, llm, settings),
        web_search=web_search or ddg_web_search,
    )
    return Orchestrator(services, settings, Metrics())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    if not getattr(app.state, "orchestrator", None):  # tests may inject one
        app.state.orchestrator = build_orchestrator(settings)
    if not getattr(app.state, "analytics", None):
        app.state.analytics = AnalyticsStore(settings.analytics_db_path)
    yield


app = FastAPI(
    title="Getnet Multi-Agent Support System",
    version="1.0.0",
    description="Router -> Knowledge (RAG + web) / Customer Support (tools) -> Human Escalation, with guardrails and tracing.",
    lifespan=lifespan,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def console() -> FileResponse:
    """Small web console: chat + agent path, tools, sources and trace for each answer."""
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> dict:
    orchestrator: Orchestrator = request.app.state.orchestrator
    # Agents are synchronous (blocking HTTP calls to the LLM); run them off the event loop.
    result = await run_in_threadpool(orchestrator.handle, req.message, req.user_id)
    analytics: AnalyticsStore | None = getattr(request.app.state, "analytics", None)
    if analytics is not None:
        try:
            analytics.record(mask_sensitive(req.message)[0], result)  # never store raw sensitive data
        except Exception:  # analytics must never break the chat
            logger.exception("analytics_record_failed")
    return result


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request) -> dict:
    if not request.app.state.analytics.feedback(req.trace_id, req.rating == "up"):
        raise HTTPException(404, "unknown trace_id")
    return {"ok": True}


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/analytics")
async def analytics_summary(request: Request, days: float | None = None) -> dict:
    return request.app.state.analytics.summary(days=days)


@app.get("/evals")
async def evals_report() -> dict:
    path = get_settings().eval_report_path
    if not path.exists():
        return {"available": False}
    return {"available": True, **json.loads(path.read_text(encoding="utf-8"))}


@app.get("/health")
async def health(request: Request) -> dict:
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(503, "starting")
    chunks = len(orch.services.retriever.store)
    return {"status": "ok" if chunks else "degraded", "knowledge_base_chunks": chunks,
            "index_built_at": orch.services.retriever.store.meta.get("built_at")}


@app.get("/metrics")
async def metrics(request: Request) -> dict:
    return request.app.state.orchestrator.metrics.snapshot()
