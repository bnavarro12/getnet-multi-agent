"""Tests for: thread-safe shared context, approval before risky actions, operator audit, SLA re-escalation,
rolling memory summary, JWT / expiring API keys, fail-closed rate limiting, required Telegram allowlist,
real-time data APIs and the numeric grounding check of the evals."""

import json
import threading
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from app.security import ApiKeyStore, DbRateLimiter, hash_key, jwt_user
from app.telegram import TelegramBridge, bridge_from_settings
from app.tools.base import ToolContext
from evals.run_evals import unsupported_numbers
from tests.conftest import STAFF, make_orchestrator
from tests.test_v3 import fake_telegram

REFUND = "My customer gave up the purchase: please refund transaction TX-89951."


# --- shared context is safe across parallel specialists ------------------------------------
def test_tool_context_writes_are_thread_safe():
    ctx = ToolContext(user_id="u", trace=None, services=None)
    def work(n):
        for i in range(500):
            ctx.add_source({"url": f"https://x/{n}-{i}"})
            ctx.add_evidence({"i": i})
    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert len(ctx.sources) == 2000 and len(ctx.evidence) == 2000


# --- approval before a risky action ---------------------------------------------------------
def test_refund_is_not_executed_before_a_human_approves(settings, repo, store):
    orch = make_orchestrator(settings, repo, store, memory=True)
    r = orch.handle(REFUND, "cliente1988")
    apr = r["tickets"][-1]
    assert apr.startswith("APR-") and r["escalated"] and apr in r["response"]
    h = orch.memory.get_handoff(apr)
    assert h["kind"] == "approval" and json.loads(h["action"])["transaction_id"] == "TX-89951"
    assert not [t for t in [repo.get_ticket(x) for x in r["tickets"]] if t and t["kind"] == "refund"]  # nothing ran

    with pytest.raises(ValueError):                       # a plain reply cannot approve
        orch.resume_after_human(apr, "ok", operator="ana")
    out = orch.resume_after_human(apr, "conferido", operator="ana", decision="approve")
    assert out["action_result"]["status"] == "executed" and out["action_result"]["approved_by"] == "ana"
    assert repo.get_ticket(out["action_result"]["refund_id"])["status"] == "executed"
    assert orch.memory.get_handoff(apr)["answered_by"] == "ana" and orch.memory.get_handoff(apr)["status"] == "closed"


def test_rejected_refund_runs_nothing(settings, repo, store):
    orch = make_orchestrator(settings, repo, store, memory=True)
    apr = orch.handle(REFUND, "cliente1988")["tickets"][-1]
    out = orch.resume_after_human(apr, "", operator="bruno", decision="reject")
    assert out["action_result"] == {"status": "rejected", "rejected_by": "bruno"}


def test_api_records_the_operator_and_needs_a_decision(client, settings):
    from tests.conftest import AUTH_1988
    r = client.post("/chat", json={"message": REFUND, "user_id": "cliente1988"}, headers=AUTH_1988).json()
    apr = r["tickets"][-1]
    assert client.post(f"/handoffs/{apr}/reply", json={"reply": "ok"}, headers=STAFF).status_code == 409
    out = client.post(f"/handoffs/{apr}/reply", json={"decision": "approve"}, headers=STAFF).json()
    assert out["answered_by"] == "admin" and out["action_result"]["status"] == "executed"


def test_personal_staff_keys_identify_the_operator(client, settings):
    from tests.conftest import AUTH_1988
    settings.staff_keys_file.write_text(json.dumps({"keys": {hash_key("ana-key"): {"operator": "ana"}}}))
    client.app.state.staff_keys = ApiKeyStore(settings.staff_keys_file)
    r = client.post("/chat", json={"message": "I want to talk to a human, I think this is fraud", "user_id": "cliente1988"}, headers=AUTH_1988).json()
    out = client.post(f"/handoffs/{r['tickets'][0]}/reply", json={"reply": "Bloqueamos."},
                      headers={"X-Admin-Key": "ana-key"}).json()
    assert out["answered_by"] == "ana"


def test_telegram_approve_command_and_operator(settings, repo, store):
    orch = make_orchestrator(settings, repo, store, memory=True)
    sent = []
    bridge = TelegramBridge("T", "-1", allowed_user_ids="42", http=fake_telegram(sent))
    apr = orch.handle(REFUND, "cliente1988")["tickets"][-1]
    bridge.notify_handoff(orch.memory, orch.memory.get_handoff(apr))
    assert "/approve" in sent[0][1]["text"]
    base = {"message_id": 9, "chat": {"id": -1}, "from": {"id": 42, "username": "ana"}, "reply_to_message": {"message_id": 101}}
    assert bridge.handle_update({"message": {**base, "text": "pode fazer"}}, orch) is None   # no decision
    assert bridge.handle_update({"message": {**base, "text": "/approve conferido"}}, orch) == apr
    assert orch.memory.get_handoff(apr)["answered_by"] == "tg:ana"


def test_telegram_requires_an_allowlist(settings):
    settings.telegram_bot_token, settings.telegram_chat_id = "T", "-1"
    assert bridge_from_settings(settings) is None              # fail closed
    settings.telegram_allowed_user_ids = "42"
    assert bridge_from_settings(settings) is not None


# --- SLA ------------------------------------------------------------------------------------
def test_overdue_case_is_reescalated_once(settings, repo, store):
    orch = make_orchestrator(settings, repo, store, memory=True)
    warned = []
    orch.on_overdue = warned.append
    t = orch.handle("I want to talk to a human, I think this is fraud", "cliente1988")["tickets"][0]
    assert orch.check_sla() == []                               # not late yet
    settings.sla_high_minutes = settings.sla_normal_minutes = -1   # every open case is now late
    assert [h["ticket_id"] for h in orch.check_sla()] == [t] and warned[0]["ticket_id"] == t
    assert orch.check_sla() == []                               # only once (atomic, safe with replicas)
    assert orch.memory.get_handoff(t)["escalation_level"] == 1


# --- memory: rolling summary ----------------------------------------------------------------
def test_turn_15_still_knows_turn_1(settings, repo, store):
    orch = make_orchestrator(settings, repo, store, memory=True)
    sid = orch.handle("My terminal GS-8841-2207 won't connect to the internet", "cliente1988")["session_id"]
    for q in ["what is Pix", "and the payment link", "and crediário", "digital account?", "loans?", "checkout?"]:
        orch.handle(q, "cliente1988", session_id=sid)
    assert "GS-8841-2207" not in json.dumps(orch.memory.history(sid))    # left the 6-turn window...
    assert "GS-8841-2207" in orch.memory.summary(sid)                    # ...but is in the summary
    orch.services.llm.calls.clear()
    orch.handle("back to my first problem, what else can I try?", "cliente1988", session_id=sid)
    assert any("GS-8841-2207" in c["user"] for c in orch.services.llm.calls if c["schema"])   # the router saw it


# --- identity ---------------------------------------------------------------------------------
def test_jwt_is_verified(settings):
    settings.auth_jwt_secret, settings.auth_jwt_audience = "s3cret-for-tests-only-32-bytes-long!", "getnet-api"
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    good = jwt.encode({"sub": "cliente1988", "exp": exp, "aud": "getnet-api"}, settings.auth_jwt_secret, "HS256")
    assert jwt_user(good, settings) == "cliente1988"
    assert jwt_user(jwt.encode({"sub": "x", "exp": exp, "aud": "getnet-api"}, "wrong-key-wrong-key-wrong-key-123", "HS256"), settings) is None
    expired = jwt.encode({"sub": "x", "exp": datetime.now(timezone.utc) - timedelta(minutes=1), "aud": "getnet-api"},
                         settings.auth_jwt_secret, "HS256")
    assert jwt_user(expired, settings) is None
    assert jwt_user(jwt.encode({"sub": "x", "exp": exp, "aud": "other"}, settings.auth_jwt_secret, "HS256"), settings) is None


def test_expired_and_revoked_api_keys_are_refused(tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"keys": {hash_key("old"): {"user_id": "a", "expires_at": past},
                                         hash_key("gone"): {"user_id": "a", "revoked": True},
                                         hash_key("new"): {"user_id": "a"}}}))
    keys = ApiKeyStore(path)
    assert keys.user_for("old") is None and keys.user_for("gone") is None and keys.user_for("new") == "a"


def test_rate_limiter_fails_closed(db):
    class Broken:
        class engine:
            @staticmethod
            def begin():
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError("x", {}, Exception("down"))
    with pytest.raises(HTTPException) as exc:
        DbRateLimiter(Broken(), 5).check("user:x")
    assert exc.value.status_code == 503
    DbRateLimiter(Broken(), 5, fail_open=True).check("user:x")     # explicit opt-in lets it through


# --- real-time data and the numeric check -----------------------------------------------------
def test_weather_answer_comes_from_the_forecast_api(orchestrator):
    r = orchestrator.handle("What's the weather forecast in Porto Alegre tomorrow?", "cliente1988", include_evidence=True)
    assert r["tools_used"] == ["get_weather_forecast"] and "2026-10-21" in json.dumps(r["evidence"])


def test_numeric_grounding_flags_invented_prices():
    evidence = [{"tool": "search_knowledge_base", "result": "Get Smart 12x R$ 10,90 ou 130,80 à vista"}]
    assert unsupported_numbers("A Get Smart custa 12x de R$ 10,90 (R$ 130,80 à vista).", evidence, "q") == []
    assert unsupported_numbers("A Get Smart custa R$ 129,90.", evidence, "q") == ["129,90"]


def test_spoofed_forwarded_for_cannot_bypass_the_ip_rate_limit(client, settings):
    """With a proxy, only the last X-Forwarded-For hop is ours; the rest is whatever the client sent."""
    from app.main import _client_key
    settings.trust_proxy_headers = True

    app = client.app

    class Req:  # minimal stand-in: headers + peer address, like a request through nginx
        headers = {"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}
        client = type("peer", (), {"host": "10.0.0.2"})()
    Req.app = app
    assert _client_key(Req()) == "203.0.113.9"      # the hop nginx added, not the spoofed one


@pytest.mark.parametrize("transaction,why", [("TX-90022", "declined sale"), ("TX-00000", "not this merchant's")])
def test_refund_is_refused_before_bothering_a_human(settings, repo, store, transaction, why):
    """The money path validates first: an invalid refund opens no approval case at all."""
    orch = make_orchestrator(settings, repo, store, memory=True)
    r = orch.handle(f"My customer gave up the purchase: please refund transaction {transaction}.", "cliente1988")
    assert not [t for t in r["tickets"] if t.startswith("APR-")], why
    assert not r["escalated"]


def test_the_same_sale_is_never_refunded_twice(settings, repo, store):
    """A customer can open a second approval case for the same sale; the money still moves once."""
    first = repo.execute_refund("cliente1988", "TX-89951", "ana")
    other = repo.execute_refund("cliente1988", "TX-89952", "ana")
    assert first["status"] == other["status"] == "executed"
    assert first["refund_id"] != other["refund_id"]                       # one audit record per sale
    again = repo.execute_refund("cliente1988", "TX-89951", "bruno")
    assert again["status"] == "already_refunded" and again["refund_id"] == first["refund_id"]


def test_an_already_refunded_sale_is_answered_without_a_case(settings, repo, store):
    """Second ask, new conversation: the tool reads the system of record and answers, bothering no operator."""
    orch = make_orchestrator(settings, repo, store, memory=True)
    apr = orch.handle(REFUND, "cliente1988")["tickets"][-1]
    done = orch.resume_after_human(apr, "ok", operator="ana", decision="approve")

    again = orch.handle(REFUND, "cliente1988", session_id=None)       # no memory of the first conversation
    assert again["tools_used"] == ["request_refund"]                  # it did consult, not guess
    assert not [t for t in again["tickets"] if t.startswith("APR-")]  # no new approval case
    assert not again["escalated"]
    assert done["action_result"]["refund_id"] in again["response"]
