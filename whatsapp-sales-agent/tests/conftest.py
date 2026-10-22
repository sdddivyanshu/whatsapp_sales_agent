"""Shared fixtures. Everything runs offline: mock WhatsApp provider + mock LLM."""

from __future__ import annotations

import csv
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

# Hard guarantee that the test suite can never reach real services.
os.environ["WHATSAPP_MODE"] = "mock"
os.environ["LLM_MODE"] = "mock"

from app.config import PROJECT_ROOT, Settings  # noqa: E402
from app.integrations.whatsapp_cloud import MockWhatsAppProvider  # noqa: E402
from app.models.message import InboundMessage, ParsedWebhook  # noqa: E402
from app.services.container import AppContainer  # noqa: E402

HEADER = ["lead_id", "customer_name", "company_name", "mobile_number", "requirement", "consent_status"]


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header or HEADER)
        writer.writerows(rows)


def append_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    kb = tmp_path / "kb"
    shutil.copytree(PROJECT_ROOT / "data" / "knowledge_base", kb)
    return Settings(
        app_env="test",
        database_path=tmp_path / "agent.db",
        leads_csv_path=tmp_path / "leads.csv",
        knowledge_base_dir=kb,
        business_profile_path=PROJECT_ROOT / "config" / "business_profile.demo.yaml",
        qualification_rules_path=PROJECT_ROOT / "config" / "qualification_rules.example.yaml",
        csv_settle_seconds=0,
        csv_use_watchdog=False,
        whatsapp_mode="mock",
        llm_mode="mock",
        whatsapp_app_secret="test-app-secret",
        whatsapp_verify_token="verify-me",
        whatsapp_phone_number_id="123456",
        admin_api_key="admin-key",
        job_backoff_base_seconds=0,
        output_sinks=["csv"],
    )


@pytest.fixture
def make_app(settings: Settings) -> Callable[..., AppContainer]:
    created: list[AppContainer] = []

    def factory(**kwargs: Any) -> AppContainer:
        s = kwargs.pop("settings", settings)
        app = AppContainer(s, **kwargs)
        app.refresh_knowledge_base()
        created.append(app)
        return app

    return factory


@pytest.fixture
def app(make_app: Callable[..., AppContainer]) -> AppContainer:
    return make_app()


def inbound(app: AppContainer, phone: str, text: str, wamid: str | None = None,
            msg_type: str = "text", drain: bool = True) -> str:
    wamid = wamid or f"wamid.T{uuid.uuid4().hex[:16]}"
    msg = InboundMessage(wa_message_id=wamid, from_phone=phone, msg_type=msg_type,
                         text=text if msg_type == "text" else None,
                         caption=None if msg_type == "text" else text)
    app.conversations.handle_webhook(ParsedWebhook(messages=[msg]))
    if drain:
        app.worker.drain()
    return wamid


def start_lead(app: AppContainer, lead_id: str = "L100", phone: str = "+919876500001",
               name: str = "Rahul Sharma", requirement: str = "EDM Wirecut machine",
               consent: str = "OPTED_IN") -> None:
    """Baseline an empty CSV, append one lead, detect it and send the intro."""
    path = app.settings.leads_csv_path
    if not path.exists():
        write_csv(path, [])
        app.csv_monitor.initialize()
    append_csv(path, [[lead_id, name, "ABC Tools", phone, requirement, consent]])
    app.csv_monitor.scan(force=True)
    app.worker.drain()


def outbound_bodies(app: AppContainer, lead_id: str) -> list[str]:
    return [r["body"] for r in app.db.query(
        "SELECT body FROM messages WHERE lead_id=? AND direction='outbound' ORDER BY id", (lead_id,))]


def mock_sent(app: AppContainer) -> list[dict[str, Any]]:
    assert isinstance(app.provider, MockWhatsAppProvider)
    return app.provider.sent
