"""Webhook verification, parsing, idempotency and lead matching."""

from __future__ import annotations

import json

from conftest import inbound, outbound_bodies, start_lead

from app.api.whatsapp_webhook import WebhookService
from app.integrations.whatsapp_cloud import parse_webhook, sign_body


def payload(messages=None, statuses=None, phone_number_id="123456"):
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "919000000000", "phone_number_id": phone_number_id}}
    if messages:
        value["contacts"] = [{"profile": {"name": "Test"}, "wa_id": m["from"]} for m in messages]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(wamid, sender, body):
    return {"from": sender, "id": wamid, "timestamp": "1700000000", "type": "text", "text": {"body": body}}


def service(app) -> WebhookService:
    return WebhookService(app.settings, app.conversations)


def post(app, data: dict, secret: str | None = "test-app-secret"):
    body = json.dumps(data).encode()
    sig = sign_body(secret, body) if secret else None
    return service(app).receive(body, sig)


def test_verification_handshake(app):
    svc = service(app)
    assert svc.verify_handshake("subscribe", "verify-me", "12345") == "12345"
    assert svc.verify_handshake("subscribe", "wrong", "12345") is None
    assert svc.verify_handshake(None, "verify-me", "12345") is None


def test_signature_is_required_and_checked(app):
    data = payload(messages=[text_msg("wamid.1", "919876500001", "hi")])
    assert post(app, data, secret="not-the-secret")[0] == 401
    assert service(app).receive(json.dumps(data).encode(), None)[0] == 401
    assert post(app, data)[0] == 200


def test_duplicate_webhook_does_not_generate_duplicate_replies(app):
    start_lead(app, lead_id="W1", phone="+919876500001")
    data = payload(messages=[text_msg("wamid.DUP1", "919876500001", "Hi, what is the price of WC-400?")])
    for _ in range(3):                                # Meta retries deliveries
        status, _ = post(app, data)
        assert status == 200
    app.worker.drain()
    replies = [m for m in app.provider.sent if m["kind"] == "text"]
    assert len(replies) == 1
    assert app.db.query_one("SELECT COUNT(*) AS n FROM messages WHERE wa_message_id='wamid.DUP1'")["n"] == 1


def test_reply_is_matched_to_the_correct_lead(app):
    start_lead(app, lead_id="A1", phone="+919876500001", name="Rahul")
    start_lead(app, lead_id="B1", phone="+919876500002", name="Priya", requirement="EDM Drill machine")
    post(app, payload(messages=[text_msg("wamid.B", "919876500002", "Tell me about DR-20")]))
    app.worker.drain()
    b_msgs = app.db.query("SELECT * FROM messages WHERE wa_message_id='wamid.B'")
    assert b_msgs[0]["lead_id"] == "B1"
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='A1'")["status"] == "MESSAGE_SENT"
    texts = [m for m in app.provider.sent if m["kind"] == "text"]
    assert len(texts) == 1 and texts[0]["to"] == "+919876500002"


def test_status_updates_are_not_customer_messages(app):
    start_lead(app, lead_id="S1", phone="+919876500001")
    intro = app.db.query_one("SELECT wa_message_id FROM messages WHERE lead_id='S1'")["wa_message_id"]
    statuses = [{"id": intro, "status": "delivered", "timestamp": "1", "recipient_id": "919876500001"},
                {"id": intro, "status": "read", "timestamp": "2", "recipient_id": "919876500001"}]
    post(app, payload(statuses=statuses))
    post(app, payload(statuses=statuses))             # duplicate status delivery
    app.worker.drain()
    assert app.db.query_one("SELECT COUNT(*) AS n FROM messages WHERE direction='inbound'")["n"] == 0
    assert [m for m in app.provider.sent if m["kind"] == "text"] == []
    lead = app.db.query_one("SELECT status, delivery_status FROM leads WHERE lead_id='S1'")
    assert lead["status"] == "DELIVERED" and lead["delivery_status"] == "read"


def test_own_number_and_foreign_phone_number_id_are_ignored(app):
    start_lead(app, lead_id="O1", phone="+919876500001")
    parsed = parse_webhook(payload(messages=[text_msg("wamid.own", "919000000000", "echo")]), "123456")
    assert parsed.messages == [] and parsed.ignored == 1
    parsed = parse_webhook(payload(messages=[text_msg("wamid.x", "919876500001", "hi")],
                                   phone_number_id="999"), "123456")
    assert parsed.messages == []


def test_media_and_unsupported_messages_are_handled(app):
    start_lead(app, lead_id="M1", phone="+919876500001")
    msg = {"from": "919876500001", "id": "wamid.IMG", "type": "image",
           "image": {"id": "media1", "caption": "drawing of our die", "mime_type": "image/jpeg"}}
    unsupported = {"from": "919876500001", "id": "wamid.UNS", "type": "unsupported",
                   "errors": [{"code": 131051}]}
    post(app, payload(messages=[msg, unsupported]))
    app.worker.drain()
    stored = app.db.query("SELECT body FROM messages WHERE direction='inbound' ORDER BY id")
    assert stored[0]["body"].startswith("[image attachment] drawing")
    assert stored[1]["body"] == "[unsupported attachment without text]"
    assert len([m for m in app.provider.sent if m["kind"] == "text"]) == 1  # one combined reply


def test_unknown_sender_is_stored_and_notified_without_auto_reply(app):
    inbound(app, "+919999999999", "Hello, do you sell lathes?")
    assert [m for m in app.provider.sent if m["kind"] == "text"] == []
    assert any(n["title"] == "Message from unknown WhatsApp number" for n in app.notifier.sent)


def test_agent_never_replies_to_its_own_outbound_messages(app):
    start_lead(app, lead_id="E1", phone="+919876500001")
    inbound(app, "+919876500001", "Hi")
    before = len(outbound_bodies(app, "E1"))
    app.worker.drain()
    app.conversations.check_inactivity()
    app.worker.drain()
    assert len(outbound_bodies(app, "E1")) == before  # nothing new without a new inbound message
