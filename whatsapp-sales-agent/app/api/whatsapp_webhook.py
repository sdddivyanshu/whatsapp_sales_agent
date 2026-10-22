"""WhatsApp webhook: Meta verification handshake + signed event intake.

The framework-independent ``WebhookService`` holds the logic (and is what the
tests exercise); the FastAPI router is a thin adapter. The POST handler only
verifies, parses and persists, then returns 200 - all LLM work happens in
queued jobs, so Meta never times out waiting for us.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.integrations.whatsapp_cloud import parse_webhook, verify_signature
from app.services.conversation_manager import ConversationManager

log = logging.getLogger(__name__)


class WebhookService:
    def __init__(self, settings: Settings, conversations: ConversationManager) -> None:
        self.settings = settings
        self.conversations = conversations

    def verify_handshake(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        expected = self.settings.whatsapp_verify_token
        if mode == "subscribe" and expected and token == expected and challenge is not None:
            return challenge
        return None

    def receive(self, body: bytes, signature: str | None) -> tuple[int, dict[str, Any]]:
        secret = self.settings.whatsapp_app_secret
        if secret:
            if not verify_signature(secret, body, signature):
                log.warning("Rejected webhook with invalid signature")
                return 401, {"error": "invalid signature"}
        elif self.settings.whatsapp_live:
            log.error("WHATSAPP_APP_SECRET is not set; refusing unsigned webhook in live mode")
            return 503, {"error": "webhook signature verification not configured"}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid JSON"}
        parsed = parse_webhook(payload, self.settings.whatsapp_phone_number_id or None)
        stats = self.conversations.handle_webhook(parsed)
        return 200, {"status": "ok", **stats, "ignored": parsed.ignored}


def build_router(service_getter):  # pragma: no cover - requires FastAPI at runtime
    from fastapi import APIRouter, Request, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter()

    @router.get("/webhook/whatsapp")
    def verify(request: Request) -> Response:
        params = request.query_params
        challenge = service_getter().verify_handshake(
            params.get("hub.mode"), params.get("hub.verify_token"), params.get("hub.challenge"))
        if challenge is None:
            return Response("forbidden", status_code=403)
        return Response(challenge, media_type="text/plain")

    @router.post("/webhook/whatsapp")
    async def receive(request: Request) -> Response:
        body = await request.body()
        status, content = await run_in_threadpool(
            service_getter().receive, body, request.headers.get("x-hub-signature-256"))
        return Response(json.dumps(content), status_code=status, media_type="application/json")

    return router
