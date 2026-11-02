"""API key and webhook authentication, exercised through the real app."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent.config import settings
from agent.core.suppression import get_suppression_list
from agent.main import app

RESEND_KEY = b"resend-signing-key"
RESEND_SECRET = "whsec_" + base64.b64encode(RESEND_KEY).decode()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")


def _svix_headers(body: bytes, timestamp: int | None = None) -> dict:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"msg_1.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(RESEND_KEY, signed, hashlib.sha256).digest()).decode()
    return {
        "svix-id": "msg_1",
        "svix-timestamp": str(timestamp),
        "svix-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


# ── API key ───────────────────────────────────────────────────────────


def test_api_open_in_development_without_key(client):
    assert client.get("/api/suppressions").status_code == 200


def test_api_closed_in_production_without_key(client, production):
    assert client.get("/api/suppressions").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{"X-API-Key": "k-123"}, {"Authorization": "Bearer k-123"}],
)
def test_api_accepts_key_in_either_header(client, monkeypatch, headers):
    monkeypatch.setattr(settings, "api_key", "k-123")
    assert client.get("/api/suppressions", headers=headers).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer x"}])
def test_api_rejects_missing_or_wrong_key(client, monkeypatch, headers):
    monkeypatch.setattr(settings, "api_key", "k-123")
    assert client.get("/api/suppressions", headers=headers).status_code == 401
    assert (
        client.post("/api/suppressions", headers=headers, json={"address": "a@b.c"}).status_code
        == 401
    )
    assert get_suppression_list().all() == []


def test_public_routes_need_no_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "k-123")
    assert client.get("/health").status_code == 200
    assert client.get("/unsubscribe", params={"t": "x"}).status_code == 400  # reached the route


# ── Resend (Svix) ─────────────────────────────────────────────────────


BOUNCE = json.dumps({"type": "email.bounced", "data": {"to": ["jane@acme.io"]}}).encode()


def test_resend_valid_signature_is_processed(client, monkeypatch):
    monkeypatch.setattr(settings, "resend_webhook_secret", RESEND_SECRET)
    response = client.post("/webhooks/email/reply", content=BOUNCE, headers=_svix_headers(BOUNCE))
    assert response.status_code == 200
    assert get_suppression_list().is_suppressed("jane@acme.io")


def test_resend_tampered_body_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "resend_webhook_secret", RESEND_SECRET)
    tampered = BOUNCE.replace(b"jane", b"bob")
    response = client.post("/webhooks/email/reply", content=tampered, headers=_svix_headers(BOUNCE))
    assert response.status_code == 401
    assert get_suppression_list().all() == []


def test_resend_replayed_old_message_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "resend_webhook_secret", RESEND_SECRET)
    stale = _svix_headers(BOUNCE, timestamp=int(time.time()) - 3600)
    assert client.post("/webhooks/email/reply", content=BOUNCE, headers=stale).status_code == 401


def test_resend_unsigned_rejected_in_production(client, production):
    response = client.post(
        "/webhooks/email/reply", content=BOUNCE, headers={"content-type": "application/json"}
    )
    assert response.status_code == 401


# ── Cal.com ───────────────────────────────────────────────────────────


CALCOM = json.dumps({"triggerEvent": "BOOKING_CREATED", "payload": {"uid": "bk_1"}}).encode()


def test_calcom_valid_signature_is_processed(client, monkeypatch):
    monkeypatch.setattr(settings, "calcom_webhook_secret", "cal-secret")
    signature = hmac.new(b"cal-secret", CALCOM, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/calcom",
        content=CALCOM,
        headers={"X-Cal-Signature-256": signature, "content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["event"] == "unmatched_booking"


def test_calcom_bad_signature_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "calcom_webhook_secret", "cal-secret")
    response = client.post(
        "/webhooks/calcom", content=CALCOM, headers={"X-Cal-Signature-256": "deadbeef"}
    )
    assert response.status_code == 401


# ── Africa's Talking ──────────────────────────────────────────────────


def test_sms_with_right_token_is_processed(client, monkeypatch):
    monkeypatch.setattr(settings, "at_webhook_token", "at-token")
    response = client.post(
        "/webhooks/sms/inbound",
        params={"token": "at-token"},
        data={"from": "+254700000005", "text": "STOP"},
    )
    assert response.status_code == 200
    assert get_suppression_list().is_suppressed("+254700000005")


@pytest.mark.parametrize("params", [{}, {"token": "wrong"}])
def test_sms_with_missing_or_wrong_token_is_rejected(client, monkeypatch, params):
    monkeypatch.setattr(settings, "at_webhook_token", "at-token")
    response = client.post(
        "/webhooks/sms/inbound", params=params, data={"from": "+254700000005", "text": "STOP"}
    )
    assert response.status_code == 401
    assert get_suppression_list().all() == []
