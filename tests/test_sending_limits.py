"""Sending guardrails: daily cap, warm-up ramp, auto-pause on bounces/complaints."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agent.channels.email_handler import process_reply_webhook, send_email
from agent.config import settings
from agent.core.orchestrator import ReviewError, approve_review
from agent.core.review import get_review_queue, new_review_item
from agent.core.sending import SendingGuard, get_sending_guard
from agent.main import app
from agent.models import EmailDraft, EmailType


@pytest.fixture
def live(monkeypatch):
    """Live mode with a fake provider that records deliveries."""
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://engine.example")
    monkeypatch.setattr(settings, "unsubscribe_secret", "s")
    delivered: list[str] = []

    async def fake_deliver(message):
        delivered.append(message["to"][0])
        return f"msg-{len(delivered)}"

    monkeypatch.setattr("agent.channels.email_handler.deliver", fake_deliver)
    return delivered


def _draft() -> EmailDraft:
    return EmailDraft(thread_id="t1", email_type=EmailType.COLD, subject="Hi", body="Body.")


@pytest.mark.asyncio
async def test_daily_limit_holds_further_sends(live, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_limit", 2)
    results = [(await send_email(f"p{i}@acme.io", _draft()))[0] for i in range(3)]

    assert [r["status"] for r in results] == ["sent", "sent", "held"]
    assert "daily limit of 2" in results[2]["reason"]
    assert live == ["p0@acme.io", "p1@acme.io"]


@pytest.mark.asyncio
async def test_sink_mode_is_never_limited(monkeypatch):
    monkeypatch.setattr(settings, "daily_send_limit", 0)
    result, _ = await send_email("p@acme.io", _draft())
    assert result["status"] == "sink"


@pytest.mark.parametrize(
    ("days_in", "expected"), [(0, 10), (6, 10), (7, 20), (14, 40), (21, 50), (60, 50)]
)
def test_warmup_ramp(monkeypatch, days_in, expected):
    monkeypatch.setattr(settings, "daily_send_limit", 50)
    monkeypatch.setattr(settings, "email_warmup_start", "2026-11-01")
    today = date(2026, 11, 1) + timedelta(days=days_in)
    assert get_sending_guard().daily_limit(today) == expected


def test_bounce_rate_pauses_only_with_enough_volume():
    guard = get_sending_guard()
    for i in range(10):
        guard.record("sent", f"p{i}@acme.io")
    guard.record("bounce", "p0@acme.io")
    assert not guard.paused_reason  # 10% but only 10 sends: too little volume to judge

    for i in range(10, 20):
        guard.record("sent", f"p{i}@acme.io")
    guard.record("bounce", "p1@acme.io")
    assert "bounce rate 10.0%" in guard.paused_reason
    assert guard.check() == (False, f"paused: {guard.paused_reason}")


def test_one_complaint_at_low_volume_pauses():
    guard = get_sending_guard()
    for i in range(50):
        guard.record("sent", f"p{i}@acme.io")
    guard.record("complaint", "p3@acme.io")
    assert "spam complaint rate" in guard.paused_reason


def test_resume_measures_health_afresh():
    guard = get_sending_guard()
    for i in range(20):
        guard.record("sent", f"p{i}@acme.io")
    guard.record("complaint", "p0@acme.io")
    assert guard.paused_reason

    guard.resume()
    assert guard.paused_reason is None
    assert guard.health()["complaints"] == 0
    assert guard.check() == (True, None)


def test_old_events_fall_out_of_the_window():
    guard = get_sending_guard()
    old = datetime.now(UTC) - timedelta(days=8)
    for i in range(20):
        guard.record("sent", f"p{i}@acme.io", at=old)
    guard.record("bounce", "p0@acme.io", at=old)
    assert guard.health()["sent"] == 0


def test_webhook_bounces_and_complaints_feed_the_guard():
    process_reply_webhook({"type": "email.complained", "data": {"to": ["p@acme.io"]}})
    assert get_sending_guard().health()["complaints"] == 1
    assert get_sending_guard().paused_reason  # complaint with no sends recorded


def test_state_survives_restart(tmp_path):
    db = tmp_path / "engine.db"
    guard = SendingGuard(db)
    guard.record("sent", "p@acme.io")
    guard.pause("manual")
    guard.close()

    reopened = SendingGuard(db)
    assert reopened.paused_reason == "manual"
    assert reopened.sent_today() == 1


@pytest.mark.asyncio
async def test_approval_over_the_limit_keeps_the_draft_pending(live, monkeypatch):
    monkeypatch.setattr(settings, "daily_send_limit", 0)
    item = new_review_item("t1", "p@acme.io", _draft(), prospect_company="Acme")
    get_review_queue().save(item)

    with pytest.raises(ReviewError, match="still pending: daily limit"):
        await approve_review(item.id, body="Edited body.")

    kept = get_review_queue().get(item.id)
    assert kept.status == "pending"
    assert live == []


def test_sending_api(monkeypatch):
    client = TestClient(app)
    status = client.get("/api/sending").json()
    assert status["paused"] is False
    assert status["daily_limit"] == settings.daily_send_limit

    paused = client.post("/api/sending/pause", json={"reason": "holiday"}).json()
    assert paused["paused_reason"] == "holiday"
    assert client.post("/api/sending/resume").json()["paused"] is False

    monkeypatch.setattr(settings, "api_key", "k")
    assert client.post("/api/sending/resume").status_code == 401
