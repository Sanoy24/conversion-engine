"""Email providers: send_email delivers through Resend, SendGrid or SMTP."""

from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest

from agent.channels import email_providers
from agent.channels.email_handler import send_email
from agent.config import settings
from agent.models import EmailDraft, EmailType


@pytest.fixture(autouse=True)
def live_and_configured(monkeypatch):
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://engine.example")
    monkeypatch.setattr(settings, "unsubscribe_secret", "test-secret")
    monkeypatch.setattr(settings, "email_from", "Sam at Acme <sam@acme.example>")


def _draft() -> EmailDraft:
    return EmailDraft(
        thread_id="thread_1", email_type=EmailType.COLD, subject="Hello", body="Body text."
    )


# ── SendGrid ──────────────────────────────────────────────────────────


@pytest.fixture
def sendgrid(monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "sendgrid")
    monkeypatch.setattr(settings, "sendgrid_api_key", "SG.key")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, headers={"X-Message-Id": "sg-msg-1"})

    monkeypatch.setattr(
        email_providers,
        "_sendgrid_http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return requests


@pytest.mark.asyncio
async def test_sendgrid_request_shape(sendgrid):
    result, trace = await send_email("jane@acme.io", _draft(), reply_to="replies@acme.example")

    assert result == {"status": "sent", "provider": "sendgrid", "id": "sg-msg-1"}
    assert trace.output_data["provider"] == "sendgrid"
    request = sendgrid[0]
    assert str(request.url) == "https://api.sendgrid.com/v3/mail/send"
    assert request.headers["authorization"] == "Bearer SG.key"
    body = json.loads(request.content)
    assert body["personalizations"] == [{"to": [{"email": "jane@acme.io"}]}]
    assert body["from"] == {"email": "sam@acme.example", "name": "Sam at Acme"}
    assert body["reply_to"] == {"email": "replies@acme.example"}
    assert body["subject"] == "Hello"
    assert body["content"][0]["type"] == "text/plain"
    assert body["content"][0]["value"].startswith("Body text.")
    assert "Unsubscribe: https://engine.example/unsubscribe" in body["content"][0]["value"]
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert body["headers"]["X-Thread-ID"] == "thread_1"


@pytest.mark.asyncio
async def test_sendgrid_error_is_reported(monkeypatch, sendgrid):
    monkeypatch.setattr(
        email_providers,
        "_sendgrid_http",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, json={"errors": [{"message": "bad key"}]})
            )
        ),
    )
    result, trace = await send_email("jane@acme.io", _draft())
    assert result["status"] == "error"
    assert "401" in result["error"]
    assert not trace.success


# ── SMTP ──────────────────────────────────────────────────────────────


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what happened."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message):
        self.calls.append("send")
        self.sent = message


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "email-smtp.us-east-1.amazonaws.com")
    monkeypatch.setattr(settings, "smtp_username", "AKIA")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(email_providers.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_providers.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


@pytest.mark.asyncio
async def test_smtp_starttls_login_and_message(smtp):
    result, _ = await send_email("jane@acme.io", _draft())

    server = smtp[0]
    assert (server.host, server.port) == ("email-smtp.us-east-1.amazonaws.com", 587)
    assert server.calls == ["starttls", "login:AKIA:secret", "send", "quit"]
    message = server.sent
    assert message["From"] == "Sam at Acme <sam@acme.example>"
    assert message["To"] == "jane@acme.io"
    assert message["Subject"] == "Hello"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert message["List-Unsubscribe"].startswith("<https://engine.example/unsubscribe?t=")
    assert message["X-Thread-ID"] == "thread_1"
    assert message["Message-ID"].endswith("@acme.example>")
    assert "Body text." in message.get_content()
    # On the wire the URL must stay a plain header, not RFC 2047-encoded.
    raw = message.as_bytes()
    assert b"List-Unsubscribe: <https://engine.example/unsubscribe?t=" in raw
    assert b"=?utf-8?" not in raw
    assert result == {"status": "sent", "provider": "smtp", "id": message["Message-ID"]}


@pytest.mark.asyncio
async def test_smtp_implicit_tls_skips_starttls(monkeypatch, smtp):
    monkeypatch.setattr(settings, "smtp_use_ssl", True)
    monkeypatch.setattr(settings, "smtp_port", 465)
    await send_email("jane@acme.io", _draft())
    assert smtp[0].port == 465
    assert "starttls" not in smtp[0].calls
    assert "context" in smtp[0].kwargs


@pytest.mark.asyncio
async def test_smtp_without_host_fails_softly(monkeypatch, smtp):
    monkeypatch.setattr(settings, "smtp_host", "")
    result, trace = await send_email("jane@acme.io", _draft())
    assert result["status"] == "error"
    assert "SMTP_HOST" in result["error"]
    assert not trace.success
    assert smtp == []


# ── Shared behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sender_falls_back_to_resend_from_email(monkeypatch, sendgrid):
    monkeypatch.setattr(settings, "email_from", "")
    monkeypatch.setattr(settings, "resend_from_email", "outbound@acme.example")
    await send_email("jane@acme.io", _draft())
    assert json.loads(sendgrid[0].content)["from"] == {"email": "outbound@acme.example"}


@pytest.mark.asyncio
async def test_kill_switch_never_reaches_a_provider(monkeypatch, sendgrid):
    monkeypatch.setattr(settings, "live_outbound_enabled", False)
    result, _ = await send_email("jane@acme.io", _draft())
    assert result["status"] == "sink"
    assert sendgrid == []
