from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
import os

from app.data.customer_repository import CustomerRepository
from app.db import Database
from app.memory import ConversationStore
from app.observability import Metrics
from app.orchestrator import Orchestrator, Services
from app.rag.retriever import Retriever
from app.rag.store import Chunk, VectorStore
from tests.fakes import FakeLLM, fake_fx, fake_weather, fake_web_search, hash_embed

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 20)  # a Tuesday -> "yesterday" is Monday
STAFF_KEY = "staff-test-key"
KEY_1988 = "demo-key-cliente1988"      # demo keys from data/api_keys.json
KEY_2024 = "demo-key-cliente2024"
AUTH_1988 = {"Authorization": f"Bearer {KEY_1988}"}
STAFF = {"X-Admin-Key": STAFF_KEY}

KB = [
    ("Maquininhas Getnet", "https://site.getnet.com.br/todas-as-maquininhas",
     "Get Smart maquininha com tela touch Android aplicativos e impressora. Get Clássica maquininha com chip 4G e Wi-Fi."),
    ("Pix Getnet", "https://site.getnet.com.br/pix",
     "Receba vendas via Pix na maquininha. O dinheiro cai na hora na conta digital Getnet."),
    ("Link de Pagamento", "https://site.getnet.com.br/link-de-pagamento",
     "Venda pelo WhatsApp e redes sociais com o Link de Pagamento Getnet."),
]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        gemini_api_key="test", customers_file=ROOT / "data" / "customers.json",
        tickets_file=tmp_path / "tickets.json", index_path=tmp_path / "index.json",
        retrieval_min_score=0.05, auto_ingest=False,
        analytics_db_path=tmp_path / "analytics.db", eval_report_path=tmp_path / "eval_report.json",
        state_db_path=tmp_path / "state.db", index_snapshot_path=tmp_path / "none.json.gz",
        admin_api_key=STAFF_KEY, rate_limit_per_minute=1000, auth_enabled=True,
        staff_keys_file=tmp_path / "staff_keys.json", sla_check_interval_s=0,
    )


@pytest.fixture
def store() -> VectorStore:
    s = VectorStore()
    chunks = [Chunk(id=str(i), text=f"{t}\n{x}", url=u, title=t) for i, (t, u, x) in enumerate(KB)]
    s.add(chunks, [hash_embed(c.text) for c in chunks])
    return s


@pytest.fixture
def db(settings) -> Database:
    """SQLite by default; set TEST_DATABASE_URL to run the same tests against Postgres (CI does)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        from app.db import metadata
        database = Database(url)
        metadata.drop_all(database.engine)
        metadata.create_all(database.engine)
        return database
    return Database(f"sqlite:///{settings.state_db_path}")


@pytest.fixture
def repo(settings, db) -> CustomerRepository:
    return CustomerRepository(settings.customers_file, db, today=TODAY)


def make_orchestrator(settings, repo, store, llm=None, web_search=fake_web_search, memory=False, fx=fake_fx) -> Orchestrator:
    llm = llm or FakeLLM()
    services = Services(llm=llm, repo=repo, retriever=Retriever(store, llm, settings),
                        web_search=web_search, today=lambda: TODAY, weather=fake_weather, fx=fx)
    mem = ConversationStore(repo.db) if memory else None
    return Orchestrator(services, settings, Metrics(), memory=mem)


@pytest.fixture
def client(settings, repo, store):
    """API client authenticated as cliente1988, with memory enabled (like production)."""
    from fastapi.testclient import TestClient

    from app.analytics import AnalyticsStore
    from app.main import app
    from app.security import ApiKeyStore, RateLimiter
    app.state.orchestrator = make_orchestrator(settings, repo, store, memory=True)
    app.state.analytics = AnalyticsStore(repo.db)
    app.state.api_keys = ApiKeyStore(settings.api_keys_file)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    with TestClient(app, headers=AUTH_1988) as c:
        yield c
    for name in ("orchestrator", "analytics", "api_keys", "rate_limiter", "staff_keys"):
        setattr(app.state, name, None)


@pytest.fixture
def orchestrator(settings, repo, store) -> Orchestrator:
    return make_orchestrator(settings, repo, store)
