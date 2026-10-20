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

    # --- Orchestration ------------------------------------------------------
    max_tool_iterations: int = 5          # hard cap on tool-calling loops per agent
    router_confidence_threshold: float = 0.55  # below this -> human escalation

    # --- RAG ----------------------------------------------------------------
    sources_file: Path = BASE_DIR / "data" / "sources.txt"
    index_path: Path = BASE_DIR / "storage" / "index.json"
    chunk_size: int = 1000                # characters per chunk
    chunk_overlap: int = 150
    retrieval_top_k: int = 5
    retrieval_min_score: float = 0.45     # cosine similarity floor for a "relevant" chunk
    crawl_max_pages: int = 60
    crawl_delay_s: float = 0.5            # politeness delay between requests
    auto_ingest: bool = True              # build the index on startup if it is missing

    # --- Mock customer data -------------------------------------------------
    customers_file: Path = BASE_DIR / "data" / "customers.json"
    tickets_file: Path = BASE_DIR / "storage" / "tickets.json"

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
