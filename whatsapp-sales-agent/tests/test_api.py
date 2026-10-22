"""HTTP layer (FastAPI). Skipped automatically if FastAPI is not installed."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.integrations.whatsapp_cloud import sign_body  # noqa: E402


@pytest.fixture
def client(settings, monkeypatch):
    import app.config as config
    import app.main as main

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "alive"}
    ready = client.get("/health/ready").json()
    assert ready["database"] is True and ready["whatsapp_mode"] == "mock"


def test_webhook_handshake_and_signed_post(client):
    r = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    assert client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                                   "hub.challenge": "42"}).status_code == 403
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    assert client.post("/webhook/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    ok = client.post("/webhook/whatsapp", content=body,
                     headers={"X-Hub-Signature-256": sign_body("test-app-secret", body)})
    assert ok.status_code == 200


def test_admin_requires_key(client):
    assert client.get("/admin/leads").status_code == 401
    assert client.get("/admin/leads", headers={"X-Admin-Key": "admin-key"}).status_code == 200
