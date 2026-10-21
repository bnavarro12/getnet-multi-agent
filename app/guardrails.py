"""Deterministic guardrails that run before and after the LLM agents.

Why rules and not only an LLM? Rules are fast, free, testable and cannot be talked out
of their job by a clever prompt. Two more layers sit on top:
  * obfuscation is normalized before matching (spaced letters, l33t-speak, zero-width characters);
  * the Router is a SEMANTIC detector: its structured output has `injection_suspected`, so a
    paraphrased attempt ("from now on you have no rules...") is blocked even when no pattern matches.

Input guardrails:
  * size limit
  * prompt-injection / jailbreak patterns
  * sensitive data (full card numbers, CVV, passwords, CPF/CNPJ): the message is
    *masked* before it reaches any LLM or log, and the user is warned.

Output guardrails:
  * mask any sensitive data the model may echo back
  * never leak the system prompt

Indirect prompt injection (content the agents READ, not what the user types):
  * web pages, knowledge-base chunks and web-search results are wrapped as untrusted data
    (`wrap_untrusted`), and instruction-like sentences inside them are removed first;
  * every agent prompt says tool results are data, never instructions (prompts.COMMON_RULES);
  * the same patterns are applied at ingestion, so poisoned chunks never enter the index.
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
    r"forget (all |your |the )?(previous |prior )?(instructions|rules|guidelines)",
    r"esque[cç]a (todas )?(as |suas |as suas )?(instru[cç][oõ]es|regras)",
    r"(new|updated) (system )?instructions?\s*:",
    r"novas instru[cç][oõ]es\s*:",
    r"\bsystem prompt\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# Obfuscation tricks that defeat a plain regex: zero-width characters, look-alike Unicode,
# l33t-speak and spaced-out letters ("i g n o r e").
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_SPACED = re.compile(r"\b(?:\w[ .\-_*]){3,}\w\b")


def normalize_for_detection(text: str) -> str:
    """A de-obfuscated copy of the text, used ONLY to detect injection (the message itself is unchanged)."""
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH)
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    t = _SPACED.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group()), t)
    return t.lower().translate(_LEET)


def find_injection(text: str) -> str | None:
    """Return the first instruction-like fragment found in the text (or its de-obfuscated form), or None."""
    for candidate in (text or "", normalize_for_detection(text)):
        for rx in _INJECTION_RE:
            m = rx.search(candidate)
            if m:
                return m.group()
    return None


def strip_injections(text: str) -> tuple[str, int]:
    """Remove sentences that look like instructions to the model (indirect injection)."""
    removed = 0
    parts = re.split(r"(?<=[.!?\n])\s+", text or "")
    kept = []
    for part in parts:
        if find_injection(part):
            removed += 1
            kept.append("[removed: instruction-like text]")
        else:
            kept.append(part)
    return " ".join(kept) if removed else (text or ""), removed


def wrap_untrusted(text: str, source: str) -> tuple[str, int]:
    """Mark external content as data. Returns (wrapped_text, removed_fragments)."""
    clean, removed = strip_injections(text)
    return f"<untrusted_content source=\"{source}\">\n{clean}\n</untrusted_content>", removed

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

    if find_injection(message):
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
    "other_account": (
        "I can only help with the account you are logged in with. For security, I can't access or share "
        "another merchant's data. / Só posso ajudar com a conta em que você está logado: por segurança, não "
        "acesso nem compartilho dados de outro lojista da Getnet."
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
