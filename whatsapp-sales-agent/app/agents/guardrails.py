"""Post-generation guardrails applied to every outgoing agent message.

The model is instructed to follow the pricing and privacy rules; this module
*enforces* them:

* every monetary amount in the reply must equal a verified price-list amount
  (or an amount the customer themselves mentioned, e.g. their budget);
* no discount / offer language;
* no phone numbers or emails other than the configured company contacts;
* no secrets, prompt text or internal tags;
* bounded length.

If a check fails the reply is replaced with a safe fallback and the incident is
recorded so it shows up in the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_REPLY_CHARS = 1500

_AMOUNT_RE = re.compile(
    r"(?:(?P<prefix>₹|\brs\.?|\binr\b|\brupees?\b)\s*)?"
    r"(?<!\w)(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suffix>lakhs?\b|lacs?\b|crores?\b|cr\b|/-|k\b|inr\b|rupees?\b)?",
    re.IGNORECASE,
)
_DISCOUNT_PROMISE_RE = re.compile(
    r"\b(\d+\s*%\s*(off|discount)|discount\s+of|special\s+(price|offer|discount)|we\s+can\s+offer"
    r"|reduced\s+price|best\s+price\s+(is|of)|festive\s+offer)\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"\+?\d[\d\s-]{8,14}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_INTERNAL_RE = re.compile(
    r"<\s*/?\s*(customer_message|reference_documents|product_facts|verified_price_checks|"
    r"conversation_history|memory|system)\b|NON-NEGOTIABLE PRICING RULES|respond_to_customer",
    re.IGNORECASE)


def extract_amounts(text: str) -> list[float]:
    """Monetary-looking amounts in ``text`` (currency marked, or large comma-grouped numbers)."""
    amounts: list[float] = []
    for m in _AMOUNT_RE.finditer(text):
        num, prefix, suffix = m.group("num"), m.group("prefix"), (m.group("suffix") or "").lower()
        value = float(num.replace(",", ""))
        has_currency = bool(prefix) or suffix in {"/-", "inr", "rupee", "rupees"} or \
            suffix.startswith(("lakh", "lac", "crore", "cr"))
        if suffix.startswith(("lakh", "lac")):
            value *= 100_000
        elif suffix.startswith(("crore", "cr")):
            value *= 10_000_000
        elif suffix == "k":
            value *= 1_000
        # Plain numbers are only treated as money when large and comma-grouped
        # (avoids flagging "400 mm", "18%", "12 months", model codes...).
        if has_currency or ("," in num and value >= 1000):
            amounts.append(round(value, 2))
    return amounts


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


@dataclass
class GuardResult:
    text: str
    passed: bool
    violations: list[str] = field(default_factory=list)


def check_reply(text: str, *, allowed_amounts: set[float], allowed_phones: list[str],
                allowed_emails: list[str], secrets: list[str], fallback: str) -> GuardResult:
    violations: list[str] = []
    reply = (text or "").strip()
    if not reply:
        violations.append("empty reply")

    allowed = {round(a, 2) for a in allowed_amounts}
    for amount in extract_amounts(reply):
        if amount not in allowed:
            violations.append(f"unverified amount {amount:,.2f}")

    if _DISCOUNT_PROMISE_RE.search(reply):
        violations.append("discount/offer language")

    allowed_digits = {_digits(p)[-10:] for p in allowed_phones if p}
    for m in _PHONE_RE.finditer(reply):
        digits = _digits(m.group(0))
        if len(digits) >= 10 and digits[-10:] not in allowed_digits:
            violations.append("unapproved phone number")

    allowed_mail = {e.lower() for e in allowed_emails if e}
    for m in _EMAIL_RE.finditer(reply):
        if m.group(0).lower().rstrip(".") not in allowed_mail:
            violations.append("unapproved email address")

    for secret in secrets:
        if secret and secret in reply:
            violations.append("secret value in reply")
    if _INTERNAL_RE.search(reply):
        violations.append("internal prompt/tag content in reply")

    if violations:
        return GuardResult(fallback, False, violations)
    if len(reply) > MAX_REPLY_CHARS:
        cut = reply[:MAX_REPLY_CHARS]
        reply = cut[:cut.rfind(" ")] + "..." if " " in cut else cut
    return GuardResult(reply, True, [])
