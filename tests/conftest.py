from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.data.customer_repository import CustomerRepository
from app.observability import Metrics
from app.orchestrator import Orchestrator, Services
from app.rag.retriever import Retriever
from app.rag.store import Chunk, VectorStore
from tests.fakes import FakeLLM, fake_web_search, hash_embed

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 20)  # a Tuesday -> "yesterday" is Monday

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
    )


@pytest.fixture
def store() -> VectorStore:
    s = VectorStore()
    chunks = [Chunk(id=str(i), text=f"{t}\n{x}", url=u, title=t) for i, (t, u, x) in enumerate(KB)]
    s.add(chunks, [hash_embed(c.text) for c in chunks])
    return s


@pytest.fixture
def repo(settings) -> CustomerRepository:
    return CustomerRepository(settings.customers_file, settings.tickets_file, today=TODAY)


def make_orchestrator(settings, repo, store, llm=None, web_search=fake_web_search) -> Orchestrator:
    llm = llm or FakeLLM()
    services = Services(llm=llm, repo=repo, retriever=Retriever(store, llm, settings),
                        web_search=web_search, today=lambda: TODAY)
    return Orchestrator(services, settings, Metrics())


@pytest.fixture
def orchestrator(settings, repo, store) -> Orchestrator:
    return make_orchestrator(settings, repo, store)
