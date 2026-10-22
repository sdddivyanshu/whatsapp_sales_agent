"""Cloud API client request format and error classification (HTTP mocked, nothing is sent)."""

from __future__ import annotations

import json

import pytest

from app.integrations.whatsapp_cloud import WhatsAppCloudClient, WhatsAppError, classify_error

httpx = pytest.importorskip("httpx")


def client_with(handler) -> WhatsAppCloudClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WhatsAppCloudClient(access_token="TEST_TOKEN", phone_number_id="PNID", api_version="v26.0",
                               rate_per_second=0, http_client=http)


def test_template_request_format():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    result = client_with(handler).send_template("+919876543210", "lead_introduction", "en",
                                                ["Rahul", "EDM Wirecut machine"])
    assert result.wa_message_id == "wamid.ABC"
    assert seen["url"] == "https://graph.facebook.com/v26.0/PNID/messages"
    assert seen["auth"] == "Bearer TEST_TOKEN"
    body = seen["body"]
    assert body["to"] == "919876543210" and body["type"] == "template"
    assert body["template"]["name"] == "lead_introduction"
    assert body["template"]["components"][0]["parameters"][1]["text"] == "EDM Wirecut machine"


def test_text_request_format():
    def handler(request):
        body = json.loads(request.content)
        assert body["type"] == "text" and body["text"]["body"] == "Hello"
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    assert client_with(handler).send_text("+919876543210", "Hello").wa_message_id == "wamid.T"


@pytest.mark.parametrize("status,code,retryable,window", [
    (400, 131047, False, True),
    (429, 130429, True, False),
    (500, None, True, False),
    (400, 132001, False, False),
    (400, 131026, False, False),
])
def test_error_classification(status, code, retryable, window):
    err = classify_error(status, {"error": {"code": code, "message": "x"}})
    assert err.retryable == retryable and err.window_expired == window


def test_http_error_raises_classified_exception():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})

    with pytest.raises(WhatsAppError) as info:
        client_with(handler).send_text("+919876543210", "late reply")
    assert info.value.window_expired


def test_network_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(WhatsAppError) as info:
        client_with(handler).send_text("+919876543210", "x")
    assert info.value.retryable
