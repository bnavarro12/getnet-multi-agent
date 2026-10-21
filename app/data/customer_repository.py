"""Access layer for (mock) customer data and support tickets.

In production this would call Getnet's internal APIs (merchant profile, settlement
ledger, terminal management, CRM). The agents only see this repository through tools,
so replacing the JSON file with real APIs does not touch any agent code.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Standard card-network response codes (ISO 8583) - meaning + what the merchant should do.
DECLINE_CODES = {
    "05": ("Do not honor - issuer refused without a specific reason",
           "Ask the customer to contact their card-issuing bank or use another card."),
    "14": ("Invalid card number", "Ask for another card or retry by inserting/tapping the card."),
    "51": ("Insufficient funds or credit limit", "Ask the customer for another card or payment method (e.g. Pix)."),
    "54": ("Expired card", "The card is expired - ask for another card."),
    "55": ("Incorrect PIN", "Ask the customer to retry carefully; after 3 attempts the card may be blocked."),
    "57": ("Transaction not permitted for this card", "Card does not allow this type of purchase; try another function (debit/credit) or another card."),
    "61": ("Amount limit exceeded", "Customer exceeded their card limit; suggest a lower amount, split payment or Pix."),
    "91": ("Issuer unavailable", "Temporary issue at the bank; wait a few minutes and try again."),
    "96": ("System error", "Restart the card machine and try again; if it persists, contact support."),
}


def add_business_days(start: date, days: int) -> date:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri (holidays ignored in this demo)
            days -= 1
    return current


class CustomerRepository:
    def __init__(self, customers_file: Path, tickets_file: Path, today: date | None = None):
        raw = json.loads(customers_file.read_text(encoding="utf-8"))
        self._customers: dict = raw["customers"]
        self._tickets_file = tickets_file
        self._lock = threading.Lock()
        self._today = today

    @property
    def today(self) -> date:
        return self._today or date.today()

    def other_accounts_mentioned(self, text: str, user_id: str) -> list[str]:
        """Merchant ids written in the message that are not the authenticated merchant's own."""
        lowered = (text or "").lower()
        return [uid for uid in self._customers if uid != user_id and uid.lower() in lowered]

    def exists(self, user_id: str) -> bool:
        return user_id in self._customers

    def profile(self, user_id: str) -> dict | None:
        c = self._customers.get(user_id)
        if not c:
            return None
        return {k: c[k] for k in ("name", "business_name", "segment", "city", "plan", "settlement_plan", "bank_account")}

    def _settlement_date(self, tx: dict, plan: dict, sale_day: date) -> list[dict]:
        """Expected deposit date(s) for a sale, following the customer's settlement plan."""
        amount = tx["amount"]
        if tx["type"] == "pix":
            return [{"date": sale_day.isoformat(), "amount": amount}]
        if tx["type"] == "debit":
            return [{"date": add_business_days(sale_day, 1).isoformat(), "amount": amount}]
        if plan.get("automatic_advance"):
            return [{"date": add_business_days(sale_day, 1).isoformat(), "amount": amount, "note": "automatic advance (fee applies)"}]
        n = max(1, tx.get("installments", 1))
        return [
            {"date": (sale_day + timedelta(days=30 * (i + 1))).isoformat(), "amount": round(amount / n, 2),
             "installment": f"{i + 1}/{n}"}
            for i in range(n)
        ]

    def sales(self, user_id: str, days_ago_from: int, days_ago_to: int) -> dict | None:
        c = self._customers.get(user_id)
        if not c:
            return None
        sales = []
        for tx in c["transactions"]:
            if not (days_ago_to <= tx["days_ago"] <= days_ago_from) or tx["status"] != "approved":
                continue
            sale_day = self.today - timedelta(days=tx["days_ago"])
            sales.append({
                "id": tx["id"], "sale_date": sale_day.isoformat(), "time": tx["time"], "type": tx["type"],
                "brand": tx["brand"], "amount": tx["amount"], "installments": tx["installments"],
                "expected_deposits": self._settlement_date(tx, c["settlement_plan"], sale_day),
            })
        total = round(sum(s["amount"] for s in sales), 2)
        return {"today": self.today.isoformat(), "sales": sales, "total_approved": total,
                "settlement_plan": c["settlement_plan"], "deposit_account": c["bank_account"]}

    def transactions(self, user_id: str, status: str | None = None, days: int = 7) -> list[dict] | None:
        c = self._customers.get(user_id)
        if not c:
            return None
        out = []
        for tx in c["transactions"]:
            if tx["days_ago"] > days or (status and tx["status"] != status):
                continue
            item = {**{k: v for k, v in tx.items() if k != "days_ago"},
                    "date": (self.today - timedelta(days=tx["days_ago"])).isoformat()}
            if tx.get("decline_code"):
                meaning, action = DECLINE_CODES.get(tx["decline_code"], ("Unknown code", "Contact support."))
                item["decline_meaning"] = meaning
                item["recommended_action"] = action
            out.append(item)
        return out

    def terminals(self, user_id: str) -> list[dict] | None:
        c = self._customers.get(user_id)
        return None if not c else c["terminals"]

    DEDUPE_WINDOW = timedelta(hours=24)

    def create_ticket(self, user_id: str, category: str, summary: str, priority: str = "normal",
                      kind: str = "support") -> dict:
        """Open a ticket - idempotently.

        Business key: one OPEN ticket per merchant + kind + category in a 24-hour window. A repeated
        request (the model calling the tool twice, the user asking again, a client retry) returns the
        existing ticket with deduplicated=True instead of creating a duplicate.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            for t in self._read_tickets():
                if (t["user_id"] == user_id and t["kind"] == kind and t["category"] == category
                        and t["status"] == "open"
                        and now - datetime.fromisoformat(t["created_at"]) < self.DEDUPE_WINDOW):
                    return {**t, "deduplicated": True}
        ticket = {
            "ticket_id": f"{'HUM' if kind == 'human_handoff' else 'SUP'}-{uuid.uuid4().hex[:8].upper()}",
            "user_id": user_id, "kind": kind, "category": category, "priority": priority,
            "summary": summary[:500], "status": "open",
            "created_at": now.isoformat(timespec="seconds"),
        }
        with self._lock:
            self._tickets_file.parent.mkdir(parents=True, exist_ok=True)
            tickets = self._read_tickets()
            tickets.append(ticket)
            self._tickets_file.write_text(json.dumps(tickets, indent=2, ensure_ascii=False))
        return ticket

    def _read_tickets(self) -> list[dict]:
        return json.loads(self._tickets_file.read_text()) if self._tickets_file.exists() else []

    def update_ticket(self, ticket_id: str, **fields) -> None:
        with self._lock:
            tickets = self._read_tickets()
            for t in tickets:
                if t["ticket_id"] == ticket_id:
                    t.update(fields)
            self._tickets_file.write_text(json.dumps(tickets, indent=2, ensure_ascii=False))
