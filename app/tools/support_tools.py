"""Tools used by the Customer Support Agent.

All of them read the merchant identified by ctx.user_id (see tools/base.py).
"""

from __future__ import annotations

from app.tools.base import Tool, ToolContext, no_params

NOT_FOUND = {"error": "customer_not_found", "message": "No merchant account is linked to this user_id."}

PERIODS = {"today": (0, 0), "yesterday": (1, 1), "last_7_days": (7, 0), "last_30_days": (30, 0)}


def _profile(ctx: ToolContext) -> dict:
    return ctx.services.repo.profile(ctx.user_id) or NOT_FOUND


def _sales_and_settlements(ctx: ToolContext, period: str = "yesterday") -> dict:
    start, end = PERIODS.get(period, PERIODS["yesterday"])
    return ctx.services.repo.sales(ctx.user_id, start, end) or NOT_FOUND


def _recent_transactions(ctx: ToolContext, status: str | None = None, days: int = 7) -> dict:
    txs = ctx.services.repo.transactions(ctx.user_id, status=status, days=min(int(days or 7), 30))
    return NOT_FOUND if txs is None else {"transactions": txs}


def _terminal_status(ctx: ToolContext) -> dict:
    terminals = ctx.services.repo.terminals(ctx.user_id)
    return NOT_FOUND if terminals is None else {"terminals": terminals}


def _open_ticket(ctx: ToolContext, category: str, summary: str, priority: str = "normal") -> dict:
    if not ctx.services.repo.exists(ctx.user_id):
        return NOT_FOUND
    ticket = ctx.services.repo.create_ticket(ctx.user_id, category, summary, priority)
    ctx.tickets.append(ticket["ticket_id"])
    return {"ticket": ticket}


GET_CUSTOMER_PROFILE = Tool(
    name="get_customer_profile",
    description="Get the merchant's profile: name, business, plan, settlement plan (when each payment type is deposited) and deposit bank account.",
    parameters=no_params(),
    fn=_profile,
)

GET_SALES_AND_SETTLEMENTS = Tool(
    name="get_sales_and_settlements",
    description="List the merchant's approved sales in a period with the expected deposit date and amount of each one (receivables schedule). Use for 'when will I receive my money' questions.",
    parameters={
        "type": "object",
        "properties": {"period": {"type": "string", "enum": list(PERIODS), "description": "Sales period"}},
        "required": ["period"],
    },
    fn=_sales_and_settlements,
)

GET_RECENT_TRANSACTIONS = Tool(
    name="get_recent_transactions",
    description="List recent card/Pix transactions, including declined ones with the decline code, its meaning and the recommended action. Use for transaction errors and declines.",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["approved", "declined"], "description": "Optional status filter"},
            "days": {"type": "integer", "description": "How many days back (1-30). Default 7."},
        },
    },
    fn=_recent_transactions,
)

GET_TERMINAL_STATUS = Tool(
    name="get_terminal_status",
    description="Get real-time diagnostics of the merchant's card machines: online/offline, connection type (Wi-Fi/4G chip), signal strength, battery, firmware. Use for connectivity or hardware problems.",
    parameters=no_params(),
    fn=_terminal_status,
)

OPEN_SUPPORT_TICKET = Tool(
    name="open_support_ticket",
    description="Open a support ticket when the issue needs follow-up by the technical team (e.g. machine replacement, problem persists after troubleshooting). Do not open tickets for questions you already answered.",
    parameters={
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["terminal", "settlement", "transaction", "account", "other"]},
            "summary": {"type": "string", "description": "Short description of the problem and what was already tried"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["category", "summary"],
    },
    fn=_open_ticket,
)
