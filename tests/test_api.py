"""API contract tests (FastAPI TestClient, fake LLM injected)."""

import pytest

from tests.conftest import STAFF


def test_chat_returns_meaningful_json(client):
    r = client.post("/chat", json={"message": "What's the difference between the Get Clássica and the Get Smart?",
                                   "user_id": "cliente1988"})
    assert r.status_code == 200
    body = r.json()
    for key in ("trace_id", "response", "agent", "route", "sources", "tools_used", "escalated", "trace"):
        assert key in body
    assert body["agent"] == "knowledge"


@pytest.mark.parametrize("payload", [
    {"message": "hi"},                                   # missing user_id
    {"user_id": "cliente1988"},                          # missing message
    {"message": "", "user_id": "cliente1988"},           # empty message
    {"message": "hi", "user_id": "bad id; drop table"},  # invalid user_id characters
])
def test_invalid_payloads_are_rejected(client, payload):
    assert client.post("/chat", json=payload).status_code == 422


def test_web_console_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "fetch(\"/chat\"" in r.text


def test_health_and_metrics(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["knowledge_base_chunks"] == 3
    client.post("/chat", json={"message": "Can I sell through WhatsApp?", "user_id": "cliente1988"})
    m = client.get("/metrics", headers=STAFF).json()
    assert m["requests_total"] == 1
    assert "p99" in m["latency_ms"]["knowledge"] and m["estimated_cost_usd"]["total"] > 0
