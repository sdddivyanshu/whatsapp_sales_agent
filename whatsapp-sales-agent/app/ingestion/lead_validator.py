"""Lead validation: phone normalisation (E.164), consent and required fields."""

from __future__ import annotations

import hashlib
import re

from app.models.lead import LeadInput, LeadState, ValidationOutcome

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
INDIAN_MOBILE_RE = re.compile(r"^\+91[6-9]\d{9}$")


def normalize_phone(raw: str | None, default_country_code: str = "91") -> str | None:
    """Return an E.164 number (``+<country><number>``) or None if it cannot be trusted.

    Accepts common Indian formats: ``98765 43210``, ``098765-43210``,
    ``91 9876543210``, ``+91-98765-43210`` and international ``00`` prefixes.
    Numbers are never "guessed": anything ambiguous is rejected.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Spreadsheet artefacts: 9.87654321E+09 or trailing ".0"
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if re.search(r"[eE]\+?\d", text) or re.search(r"[A-Za-z]", text):
        return None
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if has_plus:
        candidate = "+" + digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
    elif default_country_code == "91" and len(digits) == 10:
        candidate = "+91" + digits
    elif default_country_code == "91" and len(digits) == 11 and digits.startswith("0"):
        candidate = "+91" + digits[1:]
    elif default_country_code and digits.startswith(default_country_code) and len(digits) >= 11:
        candidate = "+" + digits
    else:
        return None
    if not E164_RE.match(candidate):
        return None
    if candidate.startswith("+91") and not INDIAN_MOBILE_RE.match(candidate):
        return None  # Indian numbers must be 10-digit mobiles starting 6-9
    return candidate


def identity_hash(mobile: str | None, requirement: str | None) -> str:
    """Key used for duplicate detection: same phone + same (normalised) requirement."""
    phone = re.sub(r"\D", "", mobile or "")
    req = re.sub(r"\s+", " ", (requirement or "").strip().lower())
    return hashlib.sha256(f"{phone}|{req}".encode()).hexdigest()


def validate_lead(lead: LeadInput, *, default_country_code: str, consent_required: bool,
                  consent_accepted_values: list[str], is_dnc: bool = False) -> ValidationOutcome:
    reasons: list[str] = []
    e164 = normalize_phone(lead.mobile_number, default_country_code)
    if not lead.mobile_number.strip():
        reasons.append("mobile_number is missing")
    elif e164 is None:
        reasons.append("mobile_number is not a valid E.164 / Indian mobile number")
    if not lead.requirement.strip():
        reasons.append("requirement is missing")
    if reasons:
        return ValidationOutcome(valid=False, mobile_e164=e164, state=LeadState.INVALID_LEAD,
                                 reasons=reasons)
    if is_dnc:
        return ValidationOutcome(valid=False, mobile_e164=e164, state=LeadState.DO_NOT_CONTACT,
                                 reasons=["number is on the do-not-contact list"])
    if consent_required:
        status = lead.consent_status.strip().upper()
        if status not in {v.upper() for v in consent_accepted_values}:
            return ValidationOutcome(
                valid=False, mobile_e164=e164, state=LeadState.CONSENT_REQUIRED,
                reasons=[f"consent_status '{lead.consent_status or 'blank'}' is not an accepted opt-in"],
            )
    return ValidationOutcome(valid=True, mobile_e164=e164, state=LeadState.READY_TO_CONTACT)
