"""Follow-ups: sent on schedule after silence, stopped by replies, review, suppression and limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agent.config import settings
from agent.core import conversation as conversation_module
from agent.core.follow_ups import run_follow_ups
from agent.core.orchestrator import approve_review, handle_prospect_reply, process_new_prospect
from agent.core.review import get_review_queue
from agent.core.sending import get_sending_guard
from agent.core.suppression import get_suppression_list
from agent.main import app
from agent.models import (
    CompetitorGapBrief,
    ConversationStatus,
    EmailDraft,
    EmailType,
    TraceRecord,
)
from tests.helpers import make_brief

T0 = datetime.now(UTC)


class FakeCRM:
    async def create_contact(self, prospect, signal_brief=None, classification=None):
        return {"id": "crm_1"}, TraceRecord(trace_id="t1", event_type="crm_contact_created")

    async def add_note(self, contact_id, note_body, prospect_company=None):
        return {"id": "n1"}, TraceRecord(trace_id="t2", event_type="crm_note_added")

    async def update_contact_status(self, contact_id, status, properties=None):
        return {"id": contact_id}


@pytest.fixture
def engine(monkeypatch, sample_funded_prospect, sample_funding, sample_hiring):
    """Pipeline with fake enrichment/drafting/sending; records drafted email types and sends."""
    brief = make_brief(
        prospect=sample_funded_prospect, funding=sample_funding, hiring=sample_hiring
    )
    log = {"drafted": [], "sent": []}

    async def fake_brief(**_kwargs):
        return brief, CompetitorGapBrief(prospect=brief.prospect), []

    async def fake_draft(**kwargs):
        email_type = kwargs.get("email_type", EmailType.COLD)
        log["drafted"].append(email_type.value)
        return EmailDraft(
            thread_id=kwargs["thread_id"],
            email_type=email_type,
            subject=f"{email_type.value} subject",
            body=f"{email_type.value} body",
        ), []

    async def fake_send(to_email, draft, reply_to=None):
        log["sent"].append(draft.email_type.value)
        return {"status": "sink", "id": "e"}, TraceRecord(
            trace_id="t3", event_type="email_sent_sink"
        )

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft)
    monkeypatch.setattr("agent.core.follow_ups.draft_email", fake_draft)
    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send)
    monkeypatch.setattr("agent.core.orchestrator.get_crm_client", FakeCRM)
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "follow_up_days", "3,7")
    log["brief"] = brief
    return log


async def _cold_thread(engine) -> str:
    result = await process_new_prospect(
        company_name=engine["brief"].prospect.company,
        contact_email=engine["brief"].prospect.contact_email,
    )
    return result["thread_id"]


def _status(thread_id):
    return conversation_module.get_conversation(thread_id).status


@pytest.mark.asyncio
async def test_follow_ups_follow_the_schedule_then_stall(engine):
    thread = await _cold_thread(engine)

    assert (await run_follow_ups(T0 + timedelta(days=2)))["follow_ups"] == []
    first = await run_follow_ups(T0 + timedelta(days=3, minutes=1))
    assert [f["number"] for f in first["follow_ups"]] == [1]

    # The second is due 7 days after the first was sent (at T0+3d).
    assert (await run_follow_ups(T0 + timedelta(days=9)))["follow_ups"] == []
    second = await run_follow_ups(T0 + timedelta(days=10, minutes=2))
    assert [f["number"] for f in second["follow_ups"]] == [2]

    # No third follow-up; after the last wait the thread is stalled.
    assert (await run_follow_ups(T0 + timedelta(days=12)))["follow_ups"] == []
    final = await run_follow_ups(T0 + timedelta(days=17, minutes=3))
    assert final["stalled"] == [thread]
    assert _status(thread) == ConversationStatus.STALLED
    assert engine["sent"] == ["cold", "re_engagement", "re_engagement"]


@pytest.mark.asyncio
async def test_a_reply_stops_the_sequence(engine):
    thread = await _cold_thread(engine)
    await handle_prospect_reply(thread, "Thanks, not right now.")

    summary = await run_follow_ups(T0 + timedelta(days=30))
    assert summary["follow_ups"] == [] and summary["checked"] == 0
    assert "re_engagement" not in engine["drafted"]


@pytest.mark.asyncio
async def test_suppressed_contacts_get_no_follow_up_or_draft(engine):
    await _cold_thread(engine)
    get_suppression_list().add(
        engine["brief"].prospect.contact_email, channel="email", reason="bounce"
    )

    summary = await run_follow_ups(T0 + timedelta(days=4))
    assert summary["skipped"] == {"suppressed": 1}
    assert "re_engagement" not in engine["drafted"]


@pytest.mark.asyncio
async def test_timing_counts_from_approval_not_from_drafting(engine, monkeypatch):
    monkeypatch.setattr(settings, "require_approval", True)
    thread = await _cold_thread(engine)

    # The first email is still in review (thread is pending_review), so there is
    # nothing to follow up on, however long it waits.
    waiting = await run_follow_ups(T0 + timedelta(days=10))
    assert waiting["checked"] == 0 and waiting["follow_ups"] == []

    review_id = conversation_module.get_conversation(thread).messages[-1].metadata["review_id"]
    await approve_review(review_id)
    # Approved "now", so nothing is due 2 days later even though the draft is older.
    assert (await run_follow_ups(T0 + timedelta(days=2)))["follow_ups"] == []

    queued = await run_follow_ups(T0 + timedelta(days=3, minutes=1))
    assert queued["follow_ups"][0]["delivery"] == "pending_review"
    assert [i.email_type for i in get_review_queue().list("pending")] == ["re_engagement"]
    # A queued follow-up blocks the next one until it is reviewed.
    assert (await run_follow_ups(T0 + timedelta(days=20)))["skipped"] == {"awaiting_review": 1}


@pytest.mark.asyncio
async def test_sending_limits_hold_the_run(engine, monkeypatch):
    await _cold_thread(engine)
    monkeypatch.setattr(settings, "live_outbound_enabled", True)
    get_sending_guard().pause("testing")

    summary = await run_follow_ups(T0 + timedelta(days=4))
    assert summary["held"] == "paused: testing"
    assert "re_engagement" not in engine["drafted"]


@pytest.mark.asyncio
async def test_empty_schedule_disables_follow_ups(engine, monkeypatch):
    await _cold_thread(engine)
    monkeypatch.setattr(settings, "follow_up_days", "")
    assert (await run_follow_ups(T0 + timedelta(days=30)))["checked"] == 0


@pytest.mark.asyncio
async def test_run_endpoint(engine):
    await _cold_thread(engine)
    response = TestClient(app).post("/api/follow-ups/run")
    assert response.status_code == 200
    assert response.json()["skipped"] == {"not_due": 1}
