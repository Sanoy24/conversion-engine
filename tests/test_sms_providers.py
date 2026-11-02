"""SMS providers: Twilio outbound, Twilio inbound webhook, and provider selection."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.channels import sms_providers
from agent.channels.sms_handler import send_sms
from agent.channels.sms_providers import normalize_twilio_inbound, twilio_signature
from agent.config import settings
from agent.core.suppression import get_suppression_list
from agent.main import app


@pytest.fixture
def twilio(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(settings, "twilio_auth_token", "tw-token")
    monkeypatch.setattr(settings, "twilio_from_number", "+15550001111")
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    monkeypatch.setattr(
        sms_providers,
        "_twilio_http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return requests


# ── Outbound ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_send_request_shape(twilio):
    result, trace = await send_sms("+254700000001", "Does Tuesday work?", thread_id="t1")

    assert result == {"status": "sent", "provider": "twilio", "id": "SM1"}
    assert trace.success
    request = twilio[0]
    assert str(request.url) == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert request.headers["authorization"].startswith("Basic ")
    assert dict(httpx.QueryParams(request.content.decode())) == {
        "To": "+254700000001",
        "Body": "Does Tuesday work?",
        "From": "+15550001111",
    }


@pytest.mark.asyncio
async def test_twilio_messaging_service_takes_precedence(monkeypatch, twilio):
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", "MG9")
    await send_sms("+254700000001", "Hi")
    form = dict(httpx.QueryParams(twilio[0].content.decode()))
    assert form["MessagingServiceSid"] == "MG9"
    assert "From" not in form


@pytest.mark.asyncio
async def test_twilio_error_is_reported(monkeypatch, twilio):
    monkeypatch.setattr(
        sms_providers,
        "_twilio_http",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(400, json={"code": 21610, "message": "unsubscribed"})
            )
        ),
    )
    result, trace = await send_sms("+254700000001", "Hi")
    assert result["status"] == "error"
    assert "21610" in result["error"]
    assert not trace.success


@pytest.mark.asyncio
async def test_suppressed_number_never_reaches_twilio(twilio):
    get_suppression_list().add("+254700000001", channel="sms", reason="sms_stop")
    result, _ = await send_sms("+254700000001", "Hi")
    assert result["status"] == "suppressed"
    assert twilio == []


@pytest.mark.asyncio
async def test_africastalking_is_default(monkeypatch):
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    sent = []

    class FakeAT:
        def send(self, message, recipients, sender_id=None):
            sent.append((message, recipients))
            return {"SMSMessageData": {"Message": "Sent to 1/1"}}

    monkeypatch.setattr(sms_providers, "_get_at_client", FakeAT)
    result, _ = await send_sms("+254700000001", "Hi")
    assert result["status"] == "sent"
    assert result["provider"] == "africastalking"
    assert sent == [("Hi", ["+254700000001"])]


# ── Inbound webhook ───────────────────────────────────────────────────


def test_twilio_signature_matches_official_library():
    # Expected value computed with twilio.request_validator.RequestValidator.
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert twilio_signature("12345", url, params) == "vNe7KK2kJwCsxc9K3OLkkKB3qqI="


def test_normalize_twilio_inbound():
    assert normalize_twilio_inbound({"From": "+1", "Body": "STOP", "MessageSid": "SM1"}) == {
        "from": "+1",
        "to": "",
        "text": "STOP",
        "id": "SM1",
    }


@pytest.fixture
def signed_client(monkeypatch, twilio):
    monkeypatch.setattr(settings, "public_base_url", "https://engine.example")
    return TestClient(app)


def _post_twilio(client, form: dict, signature: str | None = None):
    signature = signature or twilio_signature(
        "tw-token", "https://engine.example/webhooks/sms/twilio", form
    )
    return client.post("/webhooks/sms/twilio", data=form, headers={"X-Twilio-Signature": signature})


def test_twilio_stop_is_signed_routed_and_answered_with_twiml(signed_client):
    response = _post_twilio(
        signed_client, {"From": "+254700000007", "Body": "STOP", "MessageSid": "SM2"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.text == "<Response/>"
    assert get_suppression_list().get("+254700000007")["reason"] == "sms_stop"


def test_twilio_bad_signature_is_rejected(signed_client):
    response = _post_twilio(
        signed_client, {"From": "+254700000007", "Body": "STOP"}, signature="forged"
    )
    assert response.status_code == 401
    assert get_suppression_list().all() == []
