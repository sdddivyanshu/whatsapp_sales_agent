"""Salesperson notifications (handover, unknown senders, opted-out customers writing in).

Always logs. If ``NOTIFY_WEBHOOK_URL`` is set, also POSTs JSON compatible with
Slack / Microsoft Teams / Google Chat incoming webhooks (``{"text": ...}``).
Delivery runs inside a queued job, so failures are retried with backoff.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from app.logging_utils import mask_phones_in_text

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str = "", timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.sent: list[dict[str, Any]] = []

    def notify(self, title: str, details: dict[str, Any]) -> None:
        lines = [f"*{title}*"] + [f"{k}: {v}" for k, v in details.items() if v not in (None, "")]
        text = "\n".join(lines)
        self.sent.append({"title": title, **details})
        log.warning("NOTIFY %s", mask_phones_in_text(text.replace("\n", " | ")))
        if not self.webhook_url:
            return
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(self.webhook_url, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - configured URL
            if resp.status >= 300:
                raise RuntimeError(f"notification webhook returned HTTP {resp.status}")
