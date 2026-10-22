"""FastAPI application entry point.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000

One process runs the API (webhook, health, admin), the CSV monitor thread and
the job worker thread. All unfinished work lives in SQLite, so a restart simply
resumes it. Run a single replica (SQLite + file monitor are single-node).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import admin, health, whatsapp_webhook
from app.config import get_settings
from app.logging_utils import configure_logging
from app.services.container import AppContainer

log = logging.getLogger(__name__)

_state: dict[str, object] = {}


def container() -> AppContainer:
    return _state["container"]  # type: ignore[return-value]


def webhook_service() -> whatsapp_webhook.WebhookService:
    return _state["webhook"]  # type: ignore[return-value]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.secret_values())
    c = AppContainer(settings)
    _state["container"] = c
    _state["webhook"] = whatsapp_webhook.WebhookService(settings, c.conversations)
    c.start()
    try:
        yield
    finally:
        log.info("Shutting down: stopping monitor and worker (in-flight job finishes first)")
        c.stop()


app = FastAPI(title="WhatsApp Sales Agent", version="1.0.0", lifespan=lifespan)
app.include_router(health.build_router(container))
app.include_router(whatsapp_webhook.build_router(webhook_service))
app.include_router(admin.build_router(container))
