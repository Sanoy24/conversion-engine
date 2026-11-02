"""Review queue: drafts wait for approval, approval sends (once), rejection sends nothing."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.config import settings
from agent.core import conversation as conversation_module
from agent.core.orchestrator import (
    ReviewError,
    approve_review,
    handle_prospect_reply,
    process_new_prospect,
    reject_review,
)
from agent.core.review import ReviewQueue, get_review_queue, new_review_item
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


class FakeCRM:
    async def create_contact(self, prospect, signal_brief=None, classification=None):
        return {"id": "crm_1"}, TraceRecord(trace_id="t1", event_type="crm_contact_created")

    async def add_note(self, contact_id, note_body, prospect_company=None):
        return {"id": "n1"}, TraceRecord(trace_id="t2", event_type="crm_note_added")

    async def update_contact_status(self, contact_id, status, properties=None):
        return {"id": contact_id, "status": status}


@pytest.fixture
def sent(monkeypatch):
    """Record every email the orchestrator sends instead of sending it."""
    calls: list[tuple[str, EmailDraft]] = []

    async def fake_send_email(to_email, draft, reply_to=None):
        calls.append((to_email, draft))
        return {"status": "sink", "id": "e1"}, TraceRecord(
            trace_id="t3", event_type="email_sent_sink"
        )

    monkeypatch.setattr("agent.core.orchestrator.send_email", fake_send_email)
    return calls


@pytest.fixture
def pipeline(monkeypatch, sample_funded_prospect, sample_funding, sample_hiring):
    brief = make_brief(
        prospect=sample_funded_prospect, funding=sample_funding, hiring=sample_hiring
    )

    async def fake_brief(**_kwargs):
        return brief, CompetitorGapBrief(prospect=brief.prospect), []

    async def fake_draft(**kwargs):
        draft = EmailDraft(
            thread_id=kwargs["thread_id"],
            email_type=kwargs.get("email_type", EmailType.COLD),
            subject="Congrats on the Series A",
            body="Original body",
        )
        return draft, []

    monkeypatch.setattr("agent.core.orchestrator.generate_signal_brief", fake_brief)
    monkeypatch.setattr("agent.core.orchestrator.draft_email", fake_draft)
    monkeypatch.setattr("agent.core.orchestrator.get_crm_client", FakeCRM)
    return brief


async def _new_prospect(brief) -> dict:
    return await process_new_prospect(
        company_name=brief.prospect.company, contact_email=brief.prospect.contact_email
    )


@pytest.mark.asyncio
async def test_cold_email_waits_for_review(pipeline, sent):
    result = await _new_prospect(pipeline)

    assert result["email_delivery"]["status"] == "pending_review"
    assert sent == []
    item = get_review_queue().get(result["email_delivery"]["review_id"])
    assert item.status == "pending"
    assert item.to == pipeline.prospect.contact_email
    conversation = conversation_module.get_conversation(result["thread_id"])
    assert conversation.status == ConversationStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_approve_sends_once_and_updates_thread(pipeline, sent):
    result = await _new_prospect(pipeline)
    review_id = result["email_delivery"]["review_id"]

    item = await approve_review(review_id)

    assert item.status == "sent" and not item.edited
    assert [(to, d.body) for to, d in sent] == [(pipeline.prospect.contact_email, "Original body")]
    conversation = conversation_module.get_conversation(result["thread_id"])
    assert conversation.status == ConversationStatus.OUTBOUND_SENT
    assert conversation.messages[-1].metadata["delivery"]["status"] == "sink"

    with pytest.raises(ReviewError):
        await approve_review(review_id)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_approve_with_edits_keeps_the_original(pipeline, sent):
    result = await _new_prospect(pipeline)
    item = await approve_review(
        result["email_delivery"]["review_id"], subject="Edited subject", body="Edited body"
    )

    assert item.edited
    assert sent[0][1].subject == "Edited subject"
    assert sent[0][1].body == "Edited body"
    assert item.draft.body == "Original body"  # kept for learning from edits
    message = conversation_module.get_conversation(result["thread_id"]).messages[-1]
    assert message.content == "Edited body"
    assert message.metadata["edited"] is True


@pytest.mark.asyncio
async def test_reject_sends_nothing(pipeline, sent):
    result = await _new_prospect(pipeline)
    item = reject_review(result["email_delivery"]["review_id"], reason="Too pushy")

    assert item.status == "rejected" and item.reject_reason == "Too pushy"
    assert sent == []
    conversation = conversation_module.get_conversation(result["thread_id"])
    assert conversation.status == ConversationStatus.NEW
    with pytest.raises(ReviewError):
        await approve_review(item.id)


@pytest.mark.asyncio
async def test_warm_reply_is_queued_too(pipeline, sent, monkeypatch):
    monkeypatch.setattr(settings, "require_approval", False)
    result = await _new_prospect(pipeline)
    monkeypatch.setattr(settings, "require_approval", True)

    reply = await handle_prospect_reply(result["thread_id"], "Interesting, tell me more.")

    assert reply["email_delivery"]["status"] == "pending_review"
    assert len(sent) == 1  # only the cold email, sent before approval was switched on


@pytest.mark.asyncio
async def test_approval_off_sends_immediately(pipeline, sent, monkeypatch):
    monkeypatch.setattr(settings, "require_approval", False)
    result = await _new_prospect(pipeline)
    assert result["email_delivery"]["status"] == "sink"
    assert get_review_queue().list() == []


@pytest.mark.asyncio
async def test_approving_a_suppressed_address_does_not_send(pipeline):
    result = await _new_prospect(pipeline)
    get_suppression_list().add(pipeline.prospect.contact_email, channel="email", reason="bounce")

    item = await approve_review(result["email_delivery"]["review_id"])

    assert item.status == "failed"
    assert item.delivery["status"] == "suppressed"


def test_queue_survives_restart(tmp_path):
    db = tmp_path / "engine.db"
    draft = EmailDraft(thread_id="t", email_type=EmailType.COLD, subject="s", body="b")
    queue = ReviewQueue(db)
    queue.save(new_review_item("t", "a@b.io", draft, prospect_company="Acme"))
    queue.close()
    items = ReviewQueue(db).list("pending")
    assert [(i.to, i.prospect_company) for i in items] == [("a@b.io", "Acme")]


# ── API and page ──────────────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.asyncio
async def test_review_api_flow(pipeline, sent, client):
    result = await _new_prospect(pipeline)
    review_id = result["email_delivery"]["review_id"]

    listed = client.get("/api/review").json()
    assert listed["count"] == 1
    assert "draft" not in listed["items"][0]

    response = client.post(f"/api/review/{review_id}/approve", json={"body": "Edited via API"})
    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert sent[0][1].body == "Edited via API"
    assert client.post(f"/api/review/{review_id}/reject", json={}).status_code == 409
    assert client.get("/api/review").json()["count"] == 0
    assert client.get("/api/review", params={"status": "all"}).json()["count"] == 1


def test_review_api_needs_the_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "k")
    assert client.get("/api/review").status_code == 401
    assert client.get("/api/review", headers={"X-API-Key": "k"}).status_code == 200


def test_review_page_is_served(client):
    response = client.get("/review")
    assert response.status_code == 200
    assert "Review queue" in response.text
    assert "/api/review" in response.text
