"""Suppression list, unsubscribe links, and their enforcement on every send path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.channels import email_providers
from agent.channels.email_handler import process_reply_webhook, send_email
from agent.channels.sms_handler import send_sms
from agent.config import settings
from agent.core import conversation as conversation_module
from agent.core.orchestrator import (
    handle_prospect_reply,
    handle_sms_opt_out,
    process_new_prospect,
)
from agent.core.suppression import (
    BOUNCE,
    COMPLAINT,
    REPLY_OPT_OUT,
    SMS_STOP,
    UNSUBSCRIBE_LINK,
    SuppressionList,
    get_suppression_list,
    make_unsubscribe_token,
    normalize_address,
    unsubscribe_url,
    verify_unsubscribe_token,
)
from agent.main import app
from agent.models import ChannelType, ConversationStatus, EmailDraft, EmailType


@pytest.fixture
def unsubscribe_configured(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://engine.example")
    monkeypatch.setattr(settings, "unsubscribe_secret", "test-secret")


def _draft(thread_id: str = "thread_1") -> EmailDraft:
    return EmailDraft(
        thread_id=thread_id, email_type=EmailType.COLD, subject="Hello", body="Body text."
    )


# ── The list itself ───────────────────────────────────────────────────


def test_normalize_address():
    assert normalize_address("  Jane@Acme.IO ") == "jane@acme.io"
    assert normalize_address("+254 700-000 001") == "+254700000001"


def test_add_is_idempotent_and_first_reason_wins():
    suppressions = SuppressionList()
    assert suppressions.add("jane@acme.io", channel="email", reason=BOUNCE)
    assert not suppressions.add("JANE@acme.io", channel="email", reason=UNSUBSCRIBE_LINK)
    assert suppressions.get("jane@acme.io")["reason"] == BOUNCE
    assert len(suppressions.all()) == 1


def test_list_survives_restart(tmp_path):
    db_path = tmp_path / "engine.db"
    suppressions = SuppressionList(db_path)
    suppressions.add("jane@acme.io", channel="email", reason=UNSUBSCRIBE_LINK)
    suppressions.close()

    assert SuppressionList(db_path).is_suppressed("jane@acme.io")


# ── Unsubscribe tokens ────────────────────────────────────────────────


def test_token_round_trip(unsubscribe_configured):
    token = make_unsubscribe_token("Jane@Acme.io")
    assert verify_unsubscribe_token(token) == "jane@acme.io"


def test_forged_or_malformed_token_is_rejected(unsubscribe_configured):
    token = make_unsubscribe_token("jane@acme.io")
    other = make_unsubscribe_token("bob@acme.io")
    forged = token.split(".")[0] + "." + other.split(".")[1]
    assert verify_unsubscribe_token(forged) is None
    assert verify_unsubscribe_token("garbage") is None
    assert verify_unsubscribe_token("") is None


def test_no_links_without_configuration():
    assert unsubscribe_url("jane@acme.io") is None
    assert verify_unsubscribe_token("anything.at-all") is None


# ── Email sending ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppressed_email_is_never_sent():
    get_suppression_list().add("jane@acme.io", channel="email", reason=BOUNCE)
    result, trace = await send_email("Jane@acme.io", _draft())
    assert result == {"status": "suppressed", "reason": BOUNCE}
    assert trace.event_type == "email_suppressed"


@pytest.mark.asyncio
async def test_email_carries_footer_and_one_click_headers(unsubscribe_configured):
    _result, trace = await send_email("jane@acme.io", _draft())
    sent = trace.input_data
    url = unsubscribe_url("jane@acme.io")
    assert sent["headers"]["List-Unsubscribe"] == f"<{url}>"
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sent["text"].startswith("Body text.")
    assert f"Unsubscribe: {url}" in sent["text"]
    assert "100 Example Street" in sent["text"]  # postal address from the example playbook


@pytest.mark.asyncio
async def test_live_email_refused_without_unsubscribe_config(monkeypatch):
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    calls = []
    monkeypatch.setattr(email_providers.resend.Emails, "send", calls.append)

    result, trace = await send_email("jane@acme.io", _draft())

    assert result["status"] == "error"
    assert "unsubscribe_not_configured" in result["error"]
    assert not trace.success
    assert calls == []


@pytest.mark.asyncio
async def test_live_email_sends_when_configured(monkeypatch, unsubscribe_configured):
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    calls = []

    def fake_send(data):
        calls.append(data)
        return {"id": "re_1"}

    monkeypatch.setattr(settings, "resend_api_key", "re_key")
    monkeypatch.setattr(email_providers.resend.Emails, "send", fake_send)

    result, _trace = await send_email("jane@acme.io", _draft())

    assert result == {"status": "sent", "provider": "resend", "id": "re_1"}
    assert "List-Unsubscribe" in calls[0]["headers"]


# ── SMS sending ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppressed_phone_is_never_texted():
    get_suppression_list().add("+254700000001", channel="sms", reason=SMS_STOP)
    result, trace = await send_sms("+254 700 000 001", "Hi")
    assert result["status"] == "suppressed"
    assert trace.event_type == "sms_suppressed"


# ── Sources of suppression ────────────────────────────────────────────


def test_bounce_suppresses_recipient_not_sender():
    parsed = process_reply_webhook(
        {
            "type": "email.bounced",
            "data": {"from": "outbound@sender.example", "to": ["jane@acme.io"], "id": "e1"},
        }
    )
    assert parsed["recipients"] == ["jane@acme.io"]
    assert get_suppression_list().get("jane@acme.io")["reason"] == BOUNCE
    assert not get_suppression_list().is_suppressed("outbound@sender.example")


def test_complaint_is_recorded_as_complaint():
    process_reply_webhook({"type": "email.complained", "data": {"to": "jane@acme.io"}})
    assert get_suppression_list().get("jane@acme.io")["reason"] == COMPLAINT


@pytest.mark.asyncio
async def test_sms_stop_suppresses_phone_even_without_thread():
    await handle_sms_opt_out("+254700000009")
    assert get_suppression_list().get("+254700000009")["reason"] == SMS_STOP


@pytest.mark.asyncio
async def test_email_opt_out_reply_suppresses_address(sample_prospect):
    state = conversation_module.create_conversation(sample_prospect)
    conversation_module.update_status(state.thread_id, ConversationStatus.OUTBOUND_SENT)

    result = await handle_prospect_reply(
        state.thread_id, "Please unsubscribe me", channel=ChannelType.EMAIL
    )

    assert result["action"] == "opted_out"
    entry = get_suppression_list().get(sample_prospect.contact_email)
    assert entry["reason"] == REPLY_OPT_OUT
    assert entry["thread_id"] == state.thread_id


@pytest.mark.asyncio
async def test_new_prospect_skipped_before_enrichment(monkeypatch):
    get_suppression_list().add("jane@acme.io", channel="email", reason=UNSUBSCRIBE_LINK)

    async def must_not_run(**_kwargs):
        raise AssertionError("enrichment ran for a suppressed contact")

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", must_not_run)

    result = await process_new_prospect(company_name="Acme", contact_email="Jane@acme.io")

    assert result["status"] == "suppressed"
    assert result["suppression"]["reason"] == UNSUBSCRIBE_LINK


# ── Unsubscribe endpoints ─────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


def test_get_shows_confirmation_without_unsubscribing(client, unsubscribe_configured):
    token = make_unsubscribe_token("jane@acme.io")
    response = client.get("/unsubscribe", params={"t": token})
    assert response.status_code == 200
    assert "jane@acme.io" in response.text
    assert "<form method='post'" in response.text
    assert not get_suppression_list().is_suppressed("jane@acme.io")


def test_post_unsubscribes_and_closes_threads(client, unsubscribe_configured, sample_prospect):
    state = conversation_module.create_conversation(sample_prospect)
    token = make_unsubscribe_token(sample_prospect.contact_email)

    # Mail clients' one-click unsubscribe posts this form body (RFC 8058).
    response = client.post(
        "/unsubscribe", params={"t": token}, content="List-Unsubscribe=One-Click"
    )

    assert response.status_code == 200
    assert get_suppression_list().get(sample_prospect.contact_email)["reason"] == UNSUBSCRIBE_LINK
    assert conversation_module.get_conversation(state.thread_id).status == (
        ConversationStatus.OPTED_OUT
    )


def test_invalid_token_is_rejected(client, unsubscribe_configured):
    assert client.get("/unsubscribe", params={"t": "bad.token"}).status_code == 400
    assert client.post("/unsubscribe", params={"t": "bad.token"}).status_code == 400
    assert get_suppression_list().all() == []


def test_manual_suppression_api(client):
    response = client.post("/api/suppressions", json={"address": "Bob@Acme.io"})
    assert response.json() == {"status": "suppressed", "newly_added": True}
    listed = client.get("/api/suppressions").json()
    assert listed["count"] == 1
    assert listed["suppressions"][0]["address"] == "bob@acme.io"
