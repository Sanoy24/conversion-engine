"""Integration-style tests for the orchestrator workflow wiring."""

from __future__ import annotations

import pytest

from agent.core import conversation as conversation_module
from agent.core.orchestrator import handle_calcom_event, handle_prospect_reply, process_new_prospect
from agent.models import (
    ChannelType,
    CompetitorGapBrief,
    Confidence,
    EmailDraft,
    EmailType,
    GroundedClaim,
    ICPClassification,
    ICPSegment,
    ProspectInfo,
    ProposedTime,
    TraceRecord,
)
from tests.helpers import make_brief


@pytest.fixture(autouse=True)
def clear_conversation_state():
    conversation_module._conversations.clear()
    conversation_module._company_threads.clear()
    yield
    conversation_module._conversations.clear()
    conversation_module._company_threads.clear()


class _FakeHubSpotClient:
    def __init__(self):
        self.created = []
        self.notes = []
        self.status_updates = []

    async def create_contact(self, prospect, signal_brief=None, classification=None):
        self.created.append((prospect.company, classification.segment.value if classification else None))
        return {"id": "hs_123"}, TraceRecord(trace_id="tr_hs_create", event_type="hubspot_contact_created")

    async def add_note(self, contact_id, note_body, prospect_company=None):
        self.notes.append((contact_id, note_body, prospect_company))
        return {"id": "note_123"}, TraceRecord(trace_id="tr_hs_note", event_type="hubspot_note_added")

    async def update_contact_status(self, contact_id, status, properties=None):
        self.status_updates.append((contact_id, status, properties))
        return {"id": contact_id, "status": status}

    async def search_contact(self, email):
        return None


class _FakeCalComClient:
    def get_booking_link(self):
        return "https://cal.fake/book/demo"

    async def create_booking(
        self, prospect, start_time, end_time=None, notes=None, thread_id=None, sdr_email=None
    ):
        return {
            "id": "booking_123",
            "start": start_time,
            "end": end_time,
            "notes": notes,
            "thread_id": thread_id,
            "sdr_guest": sdr_email,
        }, TraceRecord(trace_id="tr_booking", event_type="calcom_booking_created")


@pytest.fixture
def fake_signal_brief(sample_funded_prospect, sample_funding, sample_hiring, no_layoff):
    return make_brief(
        prospect=sample_funded_prospect,
        funding=sample_funding,
        hiring=sample_hiring,
        layoffs=no_layoff,
    )


@pytest.fixture
def fake_email_draft():
    return EmailDraft(
        thread_id="thread_test",
        email_type=EmailType.COLD,
        subject="A grounded subject",
        body="A grounded body",
        proposed_times=[
            ProposedTime(
                prospect_local="2026-04-22 10:00 CET",
                utc="2026-04-22 09:00 UTC",
            )
        ],
        calcom_link="https://cal.fake/book/demo",
        grounded_claims=[
            GroundedClaim(
                claim="Series A, $10,000,000",
                source_field="funding.event",
                confidence="high",
            )
        ],
    )


@pytest.mark.asyncio
async def test_process_new_prospect_wires_hubspot_and_email(
    monkeypatch,
    fake_signal_brief,
    fake_email_draft,
):
    fake_hubspot = _FakeHubSpotClient()

    async def fake_generate_signal_brief(**_kwargs):
        return fake_signal_brief, CompetitorGapBrief(prospect=fake_signal_brief.prospect), []

    async def fake_draft_email(**_kwargs):
        return fake_email_draft, [TraceRecord(trace_id="tr_draft", event_type="email_draft")]

    async def fake_send_email(to_email, draft, reply_to=None):
        return {"status": "sink", "id": "email_123"}, TraceRecord(
            trace_id="tr_email",
            event_type="email_sent_sink",
            thread_id=draft.thread_id,
        )

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_generate_signal_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft_email)
    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send_email)
    monkeypatch.setattr("agent.core.orchestrator.get_hubspot_client", lambda: fake_hubspot)

    result = await process_new_prospect(
        company_name=fake_signal_brief.prospect.company,
        contact_email=fake_signal_brief.prospect.contact_email,
    )

    assert result["hubspot_contact_id"] == "hs_123"
    assert result["email_delivery"]["status"] == "sink"
    assert fake_hubspot.created == [("FreshFund Inc", "segment_1_recently_funded")]
    assert len(fake_hubspot.notes) == 1


@pytest.mark.asyncio
async def test_handle_prospect_reply_books_call_on_scheduling_intent(
    monkeypatch,
    fake_signal_brief,
    fake_email_draft,
):
    fake_hubspot = _FakeHubSpotClient()
    fake_calcom = _FakeCalComClient()

    async def fake_generate_signal_brief(**_kwargs):
        return fake_signal_brief, CompetitorGapBrief(prospect=fake_signal_brief.prospect), []

    async def fake_draft_email(**_kwargs):
        return fake_email_draft, []

    async def fake_send_email(to_email, draft, reply_to=None):
        return {"status": "sink"}, TraceRecord(trace_id="tr_email", event_type="email_sent_sink")

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_generate_signal_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft_email)
    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send_email)
    monkeypatch.setattr("agent.core.orchestrator.get_hubspot_client", lambda: fake_hubspot)
    monkeypatch.setattr("agent.core.orchestrator.get_calcom_client", lambda: fake_calcom)

    created = await process_new_prospect(
        company_name=fake_signal_brief.prospect.company,
        contact_email=fake_signal_brief.prospect.contact_email,
    )

    reply = await handle_prospect_reply(
        thread_id=created["thread_id"],
        reply_content="Sounds good. Can we schedule a call tomorrow?",
        channel=ChannelType.EMAIL,
    )

    assert reply["action"] == "booked_call"
    assert reply["calcom_booking_id"] == "booking_123"
    conversation = conversation_module.get_conversation(created["thread_id"])
    assert conversation is not None
    assert conversation.status.value == "call_booked"


@pytest.mark.asyncio
async def test_handle_prospect_reply_uses_sms_fallback_when_requested(
    monkeypatch,
    fake_signal_brief,
    fake_email_draft,
):
    fake_signal_brief.prospect.contact_phone = "+15551234567"
    fake_hubspot = _FakeHubSpotClient()

    async def fake_generate_signal_brief(**_kwargs):
        return fake_signal_brief, CompetitorGapBrief(prospect=fake_signal_brief.prospect), []

    async def fake_draft_email(**_kwargs):
        return fake_email_draft, []

    async def fake_send_email(to_email, draft, reply_to=None):
        return {"status": "sink"}, TraceRecord(trace_id="tr_email", event_type="email_sent_sink")

    async def fake_send_sms(to_phone, message, thread_id=None, warm_lead=True):
        return {"status": "sink", "to": to_phone}, TraceRecord(
            trace_id="tr_sms",
            event_type="sms_sent_sink",
            thread_id=thread_id,
        )

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_generate_signal_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft_email)
    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send_email)
    monkeypatch.setattr("agent.core.orchestrator.send_sms", fake_send_sms)
    monkeypatch.setattr("agent.core.orchestrator.get_hubspot_client", lambda: fake_hubspot)
    monkeypatch.setattr("agent.core.orchestrator.get_calcom_client", lambda: _FakeCalComClient())

    created = await process_new_prospect(
        company_name=fake_signal_brief.prospect.company,
        contact_email=fake_signal_brief.prospect.contact_email,
    )

    reply = await handle_prospect_reply(
        thread_id=created["thread_id"],
        reply_content="Text me to coordinate the meeting.",
        channel=ChannelType.EMAIL,
    )

    assert reply["action"] == "sms_fallback"
    conversation = conversation_module.get_conversation(created["thread_id"])
    assert conversation is not None
    assert conversation.status.value == "qualified"


@pytest.mark.asyncio
async def test_handle_calcom_event_updates_matched_conversation(monkeypatch):
    fake_hubspot = _FakeHubSpotClient()
    monkeypatch.setattr("agent.core.orchestrator.get_hubspot_client", lambda: fake_hubspot)

    prospect = ProspectInfo(company="CalEvent Co", contact_email="cal@example.com")
    conversation = conversation_module.create_conversation(prospect=prospect, channel=ChannelType.EMAIL)
    conversation.hubspot_contact_id = "hs_123"

    result = await handle_calcom_event(
        trigger="BOOKING_CREATED",
        booking_payload={
            "uid": "booking_456",
            "metadata": {"thread_id": conversation.thread_id},
        },
    )

    assert result["event"] == "booking_created"
    assert result["thread_id"] == conversation.thread_id
    updated = conversation_module.get_conversation(conversation.thread_id)
    assert updated is not None
    assert updated.status.value == "call_booked"
    assert updated.calcom_booking_id == "booking_456"


@pytest.mark.asyncio
async def test_qualification_gate_blocks_booking_on_first_reply_medium_confidence(
    monkeypatch,
    fake_signal_brief,
    fake_email_draft,
):
    """
    Regression test for the qualification gate (challenge doc: "qualifies in 3-5
    turns, then books").

    A MEDIUM-confidence ICP prospect who sends a scheduling-intent reply on their
    very first response (status=OUTBOUND_SENT, no prior warm exchanges) MUST NOT
    trigger a direct Cal.com booking.  The gate should fall through to a warm reply
    so the prospect gets at least one qualifying exchange first.

    Gate logic being tested:
      Rule 1: status == QUALIFIED | CALL_BOOKED  → False (status is OUTBOUND_SENT)
      Rule 2: confidence == HIGH and segment != ABSTAIN  → False (MEDIUM)
      Rule 3: ≥ 1 warm_reply agent message in thread  → False (none yet)
    Expected: action == "warm_reply", NOT "booked_call".
    """
    fake_hubspot = _FakeHubSpotClient()

    async def fake_generate_signal_brief(**_kwargs):
        return fake_signal_brief, CompetitorGapBrief(prospect=fake_signal_brief.prospect), []

    async def fake_draft_email(**_kwargs):
        return fake_email_draft, []

    async def fake_send_email(to_email, draft, reply_to=None):
        return {"status": "sink"}, TraceRecord(trace_id="tr_email", event_type="email_sent_sink")

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_generate_signal_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft_email)
    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send_email)
    monkeypatch.setattr("agent.core.orchestrator.get_hubspot_client", lambda: fake_hubspot)
    # Cal.com client should never be reached; bind it anyway so an accidental
    # call surfaces a clear AttributeError rather than a config read.
    monkeypatch.setattr("agent.core.orchestrator.get_calcom_client", lambda: _FakeCalComClient())

    created = await process_new_prospect(
        company_name=fake_signal_brief.prospect.company,
        contact_email=fake_signal_brief.prospect.contact_email,
    )

    # Override the classification on the live conversation object to MEDIUM
    # confidence so Rule 2 of the gate cannot fire.
    conv = conversation_module.get_conversation(created["thread_id"])
    assert conv is not None
    conv.classification = ICPClassification(
        prospect=fake_signal_brief.prospect,
        segment=ICPSegment.MID_MARKET_RESTRUCTURING,
        confidence=Confidence.MEDIUM,
    )

    # First reply from prospect — clear scheduling intent, no prior warm exchange
    reply = await handle_prospect_reply(
        thread_id=created["thread_id"],
        reply_content="This sounds great, let's set up a call for next week!",
        channel=ChannelType.EMAIL,
    )

    # Gate must route to warm reply, NOT direct booking
    assert reply["action"] == "warm_reply", (
        f"Expected warm_reply but got {reply['action']!r}. "
        "The qualification gate should block direct booking for MEDIUM-confidence "
        "prospects on their first reply."
    )

    # Conversation status advances to QUALIFIED (warm reply sent), not CALL_BOOKED
    updated = conversation_module.get_conversation(created["thread_id"])
    assert updated is not None
    assert updated.status.value == "qualified", (
        f"Expected status 'qualified' after warm reply, got {updated.status.value!r}"
    )
    # No booking was created
    assert updated.calcom_booking_id is None
