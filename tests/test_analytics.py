"""Analytics: storage, grouping of questions, success rate and the dashboard endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.analytics import AnalyticsStore, auto_resolved, normalize_question
from app.main import app
from tests.conftest import make_orchestrator


def test_questions_are_grouped_ignoring_case_accents_and_punctuation():
    assert normalize_question("Como funciona o Pix?") == normalize_question("como FUNCIONA o pix")
    assert normalize_question("Antecipação!") == "antecipacao"


def test_auto_resolution_rules():
    base = {"agent": "knowledge", "escalated": False, "guardrails": {}, "route": {"route": "knowledge"}}
    assert auto_resolved({**base, "sources": [{"url": "x"}], "tools_used": ["search_knowledge_base"]})
    assert not auto_resolved({**base, "sources": [], "tools_used": ["search_knowledge_base"]})  # not grounded
    assert auto_resolved({**base, "agent": "support", "sources": [], "tools_used": ["get_terminal_status"]})
    assert not auto_resolved({**base, "escalated": True, "sources": [{"url": "x"}]})
    assert not auto_resolved({**base, "agent": "error"})
    assert not auto_resolved({**base, "guardrails": {"blocked": "prompt_injection"}, "route": {}})


@pytest.fixture
def client(settings, repo, store):
    app.state.orchestrator = make_orchestrator(settings, repo, store)
    app.state.analytics = AnalyticsStore(settings.analytics_db_path)
    with TestClient(app) as c:
        yield c
    app.state.orchestrator = None
    app.state.analytics = None


def ask(client, message):
    return client.post("/chat", json={"message": message, "user_id": "cliente1988"}).json()


def test_dashboard_aggregates_questions_and_feedback(client):
    a = ask(client, "Can I sell through WhatsApp?")
    ask(client, "can i sell through whatsapp")
    b = ask(client, "Write me a poem about the sea")          # refused -> not a success
    assert client.post("/feedback", json={"trace_id": a["trace_id"], "rating": "down"}).json() == {"ok": True}

    data = client.get("/analytics").json()
    assert data["kpis"]["requests"] == 3 and data["kpis"]["ratings"] == 1
    top = data["most_asked"][0]
    assert top["asks"] == 2 and top["down"] == 1 and top["success_rate"] == 0.5  # 👎 overrides auto-resolution
    worst = data["lowest_success"][0]
    assert worst["success_rate"] == 0.0 and "poem" in worst["question"].lower()
    assert {r["route"] for r in data["routes"]} == {"knowledge", "out_of_scope"}
    assert b["trace_id"]


def test_sensitive_data_is_never_stored(client):
    ask(client, "My card 4111 1111 1111 1111 was declined at my machine")
    data = client.get("/analytics").json()
    assert "4111 1111 1111 1111" not in str(data)


def test_feedback_validation(client):
    assert client.post("/feedback", json={"trace_id": "nope", "rating": "up"}).status_code == 404
    assert client.post("/feedback", json={"trace_id": "x", "rating": "maybe"}).status_code == 422


def test_dashboard_page_and_evals_endpoint(client, settings, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    r = client.get("/dashboard")
    assert r.status_code == 200 and "Most asked questions" in r.text
    assert client.get("/evals").json() == {"available": False}
    settings.eval_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.eval_report_path.write_text('{"summary": {"cases": 1}, "cases": []}')
    assert client.get("/evals").json()["available"] is True
