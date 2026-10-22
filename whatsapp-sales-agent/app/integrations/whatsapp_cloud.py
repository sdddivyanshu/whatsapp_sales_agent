"""Meta WhatsApp Business Platform (Cloud API) client, webhook parsing and a mock provider.

Outbound
    POST {base}/{version}/{phone_number_id}/messages with a bearer token.
    * ``send_template`` for business-initiated messages (approved template required).
    * ``send_text`` for replies inside the 24-hour customer service window.
Inbound
    * ``verify_signature`` checks ``X-Hub-Signature-256`` (HMAC-SHA256 with the app secret).
    * ``parse_webhook`` turns a webhook payload into customer messages and status updates.

Errors are classified so the job queue knows whether retrying can help.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
import uuid
from typing import Any, Protocol

from app.models.message import InboundMessage, ParsedWebhook, SendResult, StatusUpdate
from app.repositories.database import Database, dumps, now_iso

log = logging.getLogger(__name__)

# Meta error codes (https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes)
WINDOW_EXPIRED_CODES = {131047}               # >24h since last customer message
RATE_LIMIT_CODES = {4, 80007, 130429, 131048, 131056}
PERMANENT_CODES = {
    100,      # invalid parameter
    131008,   # required parameter missing
    131009,   # parameter value invalid
    131026,   # message undeliverable (not on WhatsApp / old app / not accepted terms)
    131050,   # user stopped marketing messages
    132000, 132001, 132005, 132007, 132012, 132015, 132016,  # template problems
    133010,   # phone number not registered
}


class WhatsAppError(Exception):
    def __init__(self, message: str, *, code: int | None = None, retryable: bool = False,
                 window_expired: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.window_expired = window_expired
        self.status_code = status_code


class MessagingProvider(Protocol):
    def send_template(self, to: str, template_name: str, language: str,
                      body_params: list[str]) -> SendResult: ...

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult: ...

    def mark_read(self, wa_message_id: str) -> None: ...


class _RateLimiter:
    """Simple token bucket shared by all sends of a client."""

    def __init__(self, rate_per_second: float) -> None:
        self.interval = 1.0 / rate_per_second if rate_per_second > 0 else 0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def classify_error(status_code: int, body: dict[str, Any]) -> WhatsAppError:
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    message = err.get("message") or f"HTTP {status_code}"
    details = (err.get("error_data") or {}).get("details")
    text = f"WhatsApp API error {code}: {message}" + (f" ({details})" if details else "")
    if code in WINDOW_EXPIRED_CODES:
        return WhatsAppError(text, code=code, window_expired=True, status_code=status_code)
    if code in RATE_LIMIT_CODES or status_code == 429 or status_code >= 500:
        return WhatsAppError(text, code=code, retryable=True, status_code=status_code)
    if code in PERMANENT_CODES or 400 <= status_code < 500:
        return WhatsAppError(text, code=code, retryable=False, status_code=status_code)
    return WhatsAppError(text, code=code, retryable=True, status_code=status_code)


class WhatsAppCloudClient:
    def __init__(self, *, access_token: str, phone_number_id: str, api_version: str,
                 base_url: str = "https://graph.facebook.com", timeout: float = 15.0,
                 rate_per_second: float = 5.0, http_client: Any = None) -> None:
        if not access_token or not phone_number_id:
            raise ValueError("WhatsApp access token and phone number id are required")
        self.url = f"{base_url.rstrip('/')}/{api_version}/{phone_number_id}/messages"
        self._token = access_token
        self._limiter = _RateLimiter(rate_per_second)
        if http_client is None:
            import httpx

            http_client = httpx.Client(timeout=timeout)
        self._http = http_client

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._limiter.wait()
        try:
            response = self._http.post(
                self.url, json=payload,
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        except Exception as exc:  # network errors, timeouts
            raise WhatsAppError(f"network error: {type(exc).__name__}: {exc}", retryable=True) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise classify_error(response.status_code, body)
        return body

    @staticmethod
    def _result(body: dict[str, Any]) -> SendResult:
        messages = body.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise WhatsAppError(f"unexpected response: {body}", retryable=False)
        return SendResult(wa_message_id=messages[0]["id"], raw=body)

    def send_template(self, to: str, template_name: str, language: str,
                      body_params: list[str]) -> SendResult:
        template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
        if body_params:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body_params],
            }]
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
                   "to": to.lstrip("+"), "type": "template", "template": template}
        return self._result(self._post(payload))

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual",
                                   "to": to.lstrip("+"), "type": "text",
                                   "text": {"preview_url": False, "body": body[:4096]}}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return self._result(self._post(payload))

    def mark_read(self, wa_message_id: str) -> None:
        try:
            self._post({"messaging_product": "whatsapp", "status": "read",
                        "message_id": wa_message_id})
        except WhatsAppError as exc:  # read receipts are best effort
            log.debug("mark_read failed: %s", exc)


class MockWhatsAppProvider:
    """Records messages in the ``outbox_mock`` table instead of sending them.

    ``fail_next`` lets tests simulate provider errors.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.fail_next: list[WhatsAppError] = []
        self.sent: list[dict[str, Any]] = []

    def _record(self, to: str, kind: str, payload: dict[str, Any]) -> SendResult:
        if self.fail_next:
            raise self.fail_next.pop(0)
        wamid = f"wamid.MOCK{uuid.uuid4().hex[:20].upper()}"
        self.db.execute(
            "INSERT INTO outbox_mock(wa_message_id, to_phone, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (wamid, to, kind, dumps(payload), now_iso()))
        self.sent.append({"wa_message_id": wamid, "to": to, "kind": kind, **payload})
        log.info("[MOCK WhatsApp] %s to %s: %s", kind, to, payload.get("body") or payload)
        return SendResult(wa_message_id=wamid, raw={"mock": True})

    def send_template(self, to: str, template_name: str, language: str,
                      body_params: list[str]) -> SendResult:
        return self._record(to, "template", {"template": template_name, "language": language,
                                             "params": body_params})

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        return self._record(to, "text", {"body": body, "reply_to": reply_to})

    def mark_read(self, wa_message_id: str) -> None:
        return None


# ---------------------------------------------------------------------------
# webhook helpers
# ---------------------------------------------------------------------------

def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


def sign_body(app_secret: str, body: bytes) -> str:
    """Helper for tests / local simulation."""
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def _e164(wa_id: str | None) -> str:
    digits = "".join(ch for ch in (wa_id or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def parse_webhook(payload: dict[str, Any], own_phone_number_id: str | None = None) -> ParsedWebhook:
    """Extract customer messages and delivery statuses from a Cloud API webhook payload.

    Statuses are returned separately and never treated as customer messages.
    Messages addressed to a different phone number id, or echoes of our own
    number, are ignored.
    """
    result = ParsedWebhook()
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        result.ignored += 1
        return result
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                result.ignored += 1
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")
            if own_phone_number_id and phone_number_id and phone_number_id != own_phone_number_id:
                result.ignored += 1
                continue
            own_display = "".join(ch for ch in metadata.get("display_phone_number", "") if ch.isdigit())
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                sender = msg.get("from", "")
                if not msg.get("id") or not sender or (own_display and sender == own_display):
                    result.ignored += 1
                    continue
                mtype = msg.get("type", "unknown")
                text = caption = media_id = None
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body")
                elif mtype == "button":
                    text = (msg.get("button") or {}).get("text")
                elif mtype == "interactive":
                    inter = msg.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title")
                elif mtype in {"image", "document", "video", "audio", "sticker"}:
                    media = msg.get(mtype) or {}
                    media_id = media.get("id")
                    caption = media.get("caption") or media.get("filename")
                elif mtype == "location":
                    loc = msg.get("location") or {}
                    text = f"[location] {loc.get('name') or ''} {loc.get('address') or ''}".strip()
                result.messages.append(InboundMessage(
                    wa_message_id=msg["id"], from_phone=_e164(sender), timestamp=msg.get("timestamp"),
                    msg_type=mtype, text=text, caption=caption, media_id=media_id,
                    profile_name=names.get(sender), to_phone_number_id=phone_number_id, raw=msg))
            for status in value.get("statuses") or []:
                if not status.get("id") or not status.get("status"):
                    result.ignored += 1
                    continue
                result.statuses.append(StatusUpdate(
                    wa_message_id=status["id"], status=status["status"],
                    recipient=_e164(status.get("recipient_id")), timestamp=status.get("timestamp"),
                    errors=status.get("errors") or [], raw=status))
    return result
