"""Human-in-the-loop over Telegram: the specialist gets the case on the phone and answers there.

    hand-off created ──► bot posts the case to the team's Telegram chat (ticket, priority, summary, last turns)
    specialist REPLIES to that message ──► poller reads it ──► orchestrator.resume_after_human()
        ──► the agent writes the next message to the customer ──► bot confirms in the chat ("✅ sent")

    "/close <text>" as the reply closes the case; any other reply answers it and keeps it open.

Setup (README §5): create a bot with @BotFather, add it to a group (or talk to it directly), then set
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env. Optional TELEGRAM_ALLOWED_USER_IDS restricts who may answer.

Security: only messages from the configured chat (and allowed users, when set) are accepted, and only as a
reply to a hand-off notification - a random message in the chat cannot answer anyone.

Replicas: Telegram allows one poller per bot. With one container the poller runs inside the API; with
several replicas run it once as a worker (docker-compose.replicas.yml: `python -m app.telegram`).
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
ROLE = {"user": "👤 Cliente", "assistant": "🤖 Assistente", "human_agent": "🧑‍💼 Especialista"}


class TelegramBridge:
    def __init__(self, token: str, chat_id: str, allowed_user_ids: str = "", http: httpx.Client | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.allowed = {x.strip() for x in allowed_user_ids.split(",") if x.strip()}
        self.http = http or httpx.Client(timeout=40)
        self.offset = 0
        self._stop = threading.Event()

    def _call(self, method: str, **payload) -> dict:
        r = self.http.post(API.format(token=self.token, method=method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {str(data)[:200]}")
        return data["result"]

    # -- outbound: new hand-off -------------------------------------------------------
    def notify_handoff(self, memory, handoff: dict) -> None:
        """Post the case; the message id is stored so the specialist's reply finds its ticket."""
        turns = memory.history(handoff["session_id"], limit=6) if handoff.get("session_id") else []
        convo = "\n".join(f"{ROLE.get(t['role'], t['role'])}: {t['text'][:300]}" for t in turns)
        text = (f"🎫 {handoff['ticket_id']} · prioridade {handoff['priority']} · {handoff['category']}\n"
                f"Lojista: {handoff['user_id']}\nMotivo: {handoff['reason']}\n\nResumo: {handoff['summary']}\n\n"
                f"{convo}\n\n↩️ RESPONDA a esta mensagem para enviar a resposta ao cliente "
                f"(comece com /close para encerrar o caso).")
        try:
            msg = self._call("sendMessage", chat_id=self.chat_id, text=text[:4000])
            memory.set_channel_ref(handoff["ticket_id"], f"tg:{self.chat_id}:{msg['message_id']}")
            logger.info(json.dumps({"event": "telegram_case_sent", "ticket_id": handoff["ticket_id"],
                                    "message_id": msg["message_id"]}))
        except Exception as exc:  # the web inbox still works if Telegram is down
            logger.warning(json.dumps({"event": "telegram_notify_failed", "error": str(exc)[:200]}))

    # -- inbound: specialist replies ----------------------------------------------------
    def _ignore(self, reason: str, **info) -> None:
        logger.info(json.dumps({"event": "telegram_update_ignored", "reason": reason, **info}, default=str))

    def handle_update(self, update: dict, orchestrator) -> str | None:
        """Process one Telegram update. Returns the ticket id answered, or None if ignored."""
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id"))
        original = msg.get("reply_to_message")
        logger.info(json.dumps({"event": "telegram_update", "chat_id": chat, "is_reply": bool(original),
                                "reply_to": (original or {}).get("message_id")}))
        if chat != self.chat_id:
            self._ignore("other_chat", chat_id=chat, expected=self.chat_id)
            return None
        sender = str((msg.get("from") or {}).get("id", ""))
        if self.allowed and sender not in self.allowed:
            self._ignore("user_not_allowed", user_id=sender)
            return None
        text = (msg.get("text") or "").strip()
        if not original or not text:
            self._ignore("not_a_reply_to_a_case" if not original else "empty_text")
            if not original and text:
                self._reply(msg, "Para responder a um caso, use \"Responder\" na mensagem do caso (🎫 HUM-...).")
            return None
        ref = f"tg:{self.chat_id}:{original['message_id']}"
        handoff = orchestrator.memory.find_handoff_by_channel_ref(ref)
        if not handoff:
            self._ignore("no_case_for_this_message", ref=ref)
            self._reply(msg, "Não encontrei o caso desta mensagem. Responda à mensagem 🎫 HUM-... mais recente.")
            return None
        close = text.lower().startswith("/close")
        reply = text[6:].strip() if close else text
        if not reply:
            self._reply(msg, "Escreva a resposta depois do /close.")
            return None
        try:
            out = orchestrator.resume_after_human(handoff["ticket_id"], reply, close=close)
            self._reply(msg, f"✅ {handoff['ticket_id']} {'encerrado' if close else 'respondido'}. "
                             f"Enviado ao cliente:\n\n{out['message_to_customer'][:3500]}")
            logger.info(json.dumps({"event": "telegram_reply_delivered", "ticket_id": handoff["ticket_id"]}))
        except ValueError as exc:
            self._reply(msg, f"⚠️ {handoff['ticket_id']}: {exc}")
            return None
        return handoff["ticket_id"]

    def _reply(self, msg: dict, text: str) -> None:
        try:
            self._call("sendMessage", chat_id=self.chat_id, text=text, reply_to_message_id=msg["message_id"])
        except Exception as exc:
            logger.warning(json.dumps({"event": "telegram_reply_failed", "error": str(exc)[:200]}))

    def poll_once(self, orchestrator, timeout: int = 25) -> int:
        updates = self._call("getUpdates", offset=self.offset, timeout=timeout, allowed_updates=["message"])
        if updates:
            logger.info(json.dumps({"event": "telegram_updates_received", "count": len(updates)}))
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
            try:
                self.handle_update(u, orchestrator)
            except Exception:
                logger.exception("telegram_update_failed")
        return len(updates)

    def run_forever(self, orchestrator) -> None:
        logger.info(json.dumps({"event": "telegram_poller_started", "chat_id": self.chat_id}))
        while not self._stop.is_set():
            try:
                self.poll_once(orchestrator)
            except Exception as exc:
                logger.warning(json.dumps({"event": "telegram_poll_error", "error": str(exc)[:200]}))
                self._stop.wait(5)

    def start(self, orchestrator) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, args=(orchestrator,), daemon=True, name="telegram-poller")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def bridge_from_settings(settings) -> TelegramBridge | None:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramBridge(settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_allowed_user_ids)
    return None


if __name__ == "__main__":  # dedicated worker (one per bot) for multi-replica deployments
    from app.config import get_settings
    from app.main import build_orchestrator
    from app.observability import setup_logging

    s = get_settings()
    setup_logging(s.log_level)
    bridge = bridge_from_settings(s)
    if bridge is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to run the Telegram worker.")
    orch = build_orchestrator(s)
    orch.on_handoff = lambda h: bridge.notify_handoff(orch.memory, h)
    while True:
        bridge.run_forever(orch)
        time.sleep(5)
