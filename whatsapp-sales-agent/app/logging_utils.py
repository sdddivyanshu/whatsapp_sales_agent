"""Logging with phone-number masking and secret redaction."""

from __future__ import annotations

import logging
import re
from typing import Iterable

# Phone-like sequences: optional +, 10-15 digits, possibly separated by spaces/dashes.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s-]{8,16}\d(?![\w.])")


def mask_phone(value: str | None) -> str:
    """+919876543210 -> +9198******10"""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 6:
        return "***"
    prefix = "+" if value.strip().startswith("+") else ""
    return f"{prefix}{digits[:4]}{'*' * (len(digits) - 6)}{digits[-2:]}"


def mask_phones_in_text(text: str) -> str:
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


class RedactingFilter(logging.Filter):
    """Masks phone numbers and removes configured secrets from log records."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = mask_phones_in_text(message)
        record.msg = message
        record.args = None
        return True


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(getattr(h, "_wa_agent", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler._wa_agent = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    for handler in root.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, RedactingFilter)]
        handler.addFilter(RedactingFilter(secrets))
