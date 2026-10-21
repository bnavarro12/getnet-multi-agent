"""Application settings, loaded from environment variables (or a .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM provider -------------------------------------------------------
    gemini_api_key: str = ""
    # Model names change often; "-latest" aliases always point to the current release.
    # Override them in .env if you want to pin a specific version.
    llm_model: str = "gemini-flash-lite-latest"     # specialist agents: fast and reliable on the free tier
    router_model: str = "gemini-flash-lite-latest"  # router/classifier (cheap and fast)
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    embed_batch_size: int = 16            # small batches keep each request under per-minute token quotas
    embed_batch_delay_s: float = 8.0      # pause between batches during ingestion (free tier)
    embed_max_retries: int = 6
    llm_temperature: float = 0.2
    # Reasoning effort for "thinking" models (minimal/low/medium/high, empty = model default).
    # Support answers need little deliberation; "low" cuts latency a lot.
    llm_thinking_level: str = "low"
    llm_timeout_s: int = 120
    llm_max_retries: int = 3
    # Evaluation judge: a different (larger) model than the agents, so the system is not grading
    # itself with the same model. Set JUDGE_MODEL to another provider's model to remove all overlap.
    judge_model: str = "gemini-flash-latest"
    # Circuit breaker: after N consecutive LLM failures, fail fast for a cooldown period instead of
    # piling up requests on a provider that is down.
    circuit_breaker_failures: int = 5
    circuit_breaker_cooldown_s: float = 30.0
    # Cost estimate (USD per 1M tokens). Defaults are Flash-Lite list prices; check current pricing.
    price_input_per_mtok_usd: float = 0.10
    price_output_per_mtok_usd: float = 0.40

    # --- Orchestration ------------------------------------------------------
    max_tool_iterations: int = 5          # hard cap on tool-calling loops per agent
    router_confidence_threshold: float = 0.55  # below this -> human escalation
    max_plan_steps: int = 2               # supervisor: at most N specialists per message

    # --- RAG ----------------------------------------------------------------
    sources_file: Path = BASE_DIR / "data" / "sources.txt"
    index_path: Path = BASE_DIR / "storage" / "index.json"
    chunk_size: int = 1000                # characters per chunk
    chunk_overlap: int = 150
    retrieval_top_k: int = 5
    retrieval_min_score: float = 0.45     # cosine similarity floor for a "relevant" chunk
    # dense | hybrid (dense + BM25, fused with RRF). Default chosen by measurement: on the retrieval eval
    # (evals/retrieval_report.md) hybrid did not beat dense on this corpus, so dense stays the default.
    retrieval_mode: str = "dense"
    crawl_max_pages: int = 60
    crawl_delay_s: float = 0.5            # politeness delay between requests
    auto_ingest: bool = True              # build the index on startup if it is missing
    # Optional committed snapshot of the index: makes the first start deterministic (no live crawl).
    index_snapshot_path: Path = BASE_DIR / "data" / "index_snapshot.json.gz"
    index_max_age_days: float = 7.0       # older index -> refreshed in the background (0 = never)

    # --- Security -----------------------------------------------------------
    # Identity comes from the API key, not from the request body. The body's user_id must match it.
    # AUTH_ENABLED=false restores the challenge's original contract (user_id trusted from the body).
    auth_enabled: bool = True
    api_keys_file: Path = BASE_DIR / "data" / "api_keys.json"   # sha256(key) -> user_id
    # Staff key (operations + human specialists): /analytics, /metrics, /evals, /handoffs.
    # Empty = those endpoints are disabled.
    admin_api_key: str = ""
    rate_limit_per_minute: int = 20       # per merchant (or per IP when unauthenticated)

    # --- Conversation memory ------------------------------------------------
    state_db_path: Path = BASE_DIR / "storage" / "state.db"    # sessions, turns, human hand-offs
    memory_max_turns: int = 6             # previous turns given to the agents
    session_ttl_hours: float = 24.0       # an idle session older than this starts fresh

    # --- Mock customer data -------------------------------------------------
    customers_file: Path = BASE_DIR / "data" / "customers.json"
    tickets_file: Path = BASE_DIR / "storage" / "tickets.json"

    # --- Analytics & evaluation ---------------------------------------------
    analytics_db_path: Path = BASE_DIR / "storage" / "analytics.db"
    eval_report_path: Path = BASE_DIR / "storage" / "eval_report.json"

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
