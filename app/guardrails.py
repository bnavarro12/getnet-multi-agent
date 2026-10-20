"""Deterministic guardrails that run before and after the LLM agents.

Why rules and not only an LLM? Rules are fast, free, testable and cannot be talked out
of their job by a clever prompt. The Router adds a second, semantic layer on top
(it can label a message `out_of_scope` or `unsafe`).

Input guardrails:
  * size limit
  * prompt-injection / jailbreak patterns
  * sensitive data (full card numbers, CVV, passwords, CPF/CNPJ): the message is
    *masked* before it reaches any LLM or log, and the user is warned.

Output guardrails:
  * mask any sensitive data the model may echo back
  * never leak the system prompt
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_MESSAGE_CHARS = 2000

INJECTION_PATTERNS = [
    r"ignore (all |the |any )?(previous|prior|above) (instructions|prompts?|rules)",
    r"ignor[ea] (todas )?as instru[cç][oõ]es (anteriores|acima)",
    r"disregard (your|all|the) (instructions|rules|guidelines)",
    r"(reveal|show|print|repeat) (me )?(your|the) (system )?(prompt|instructions)",
    r"(mostre|revele|repita) (o seu |seu |o )?(prompt|instru[cç][oõ]es)",
    r"you are now (DAN|in developer mode|jailbroken)",
    r"\bjailbreak\b",
    r"act as (an? )?(unfiltered|unrestricted)",
]

# Card numbers: 13-19 digits, optionally separated by spaces/dashes; validated with Luhn.
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
CVV_RE = re.compile(r"\b(cvv|cvc|c[oó]digo de seguran[cç]a)\D{0,10}\d{3,4}\b", re.IGNORECASE)
PASSWORD_RE = re.compile(r"\b(password|senha|pin)\s*(?:[:=]|\bis\b|\bé\b)\s*\S+", re.IGNORECASE)


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_sensitive(text: str) -> tuple[str, list[str]]:
    """Return (masked_text, list_of_detected_types)."""
    found: list[str] = []

    def card_sub(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card_number")
            return f"[CARD ****{digits[-4:]}]"
        return m.group()

    text = CARD_RE.sub(card_sub, text)
    if CVV_RE.search(text):
        found.append("cvv")
        text = CVV_RE.sub("[CVV REDACTED]", text)
    if PASSWORD_RE.search(text):
        found.append("password")
        text = PASSWORD_RE.sub("[CREDENTIAL REDACTED]", text)
    if CNPJ_RE.search(text):
        found.append("cnpj")
        text = CNPJ_RE.sub("[CNPJ REDACTED]", text)
    if CPF_RE.search(text):
        found.append("cpf")
        text = CPF_RE.sub("[CPF REDACTED]", text)
    return text, found


@dataclass
class GuardrailResult:
    allowed: bool
    text: str                     # message to forward (masked)
    reason: str | None = None     # why it was blocked, if blocked
    warnings: list[str] = field(default_factory=list)


def check_input(message: str) -> GuardrailResult:
    message = (message or "").strip()
    if not message:
        return GuardrailResult(False, message, reason="empty_message")
    if len(message) > MAX_MESSAGE_CHARS:
        return GuardrailResult(False, message[:MAX_MESSAGE_CHARS], reason="message_too_long")

    lowered = message.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            return GuardrailResult(False, message, reason="prompt_injection")

    masked, found = mask_sensitive(message)
    warnings = [f"sensitive_data_masked:{kind}" for kind in found]
    return GuardrailResult(True, masked, warnings=warnings)


def check_output(text: str) -> tuple[str, list[str]]:
    masked, found = mask_sensitive(text or "")
    return masked, [f"output_masked:{k}" for k in found]


BLOCK_MESSAGES = {
    "empty_message": "Please send a message so I can help you.",
    "message_too_long": f"Your message is too long. Please keep it under {MAX_MESSAGE_CHARS} characters.",
    "prompt_injection": (
        "I can't help with that request. I'm Getnet's virtual assistant and can help with "
        "Getnet products, your sales, receivables and card machines."
    ),
    "unsafe": (
        "I can't help with that. If you have a question about Getnet products or your account, "
        "I'm happy to help."
    ),
}

SENSITIVE_DATA_NOTICE = (
    "For your security, never share full card numbers, CVV, passwords or documents in chat. "
    "I've hidden that information."
)
