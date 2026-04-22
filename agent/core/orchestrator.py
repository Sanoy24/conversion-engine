"""
End-to-end orchestrator.
Wires together enrichment, classification, drafting, delivery, CRM logging,
and discovery-call booking.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agent.channels.email_handler import send_email
from agent.channels.sms_handler import send_sms
from agent.core.conversation import (
    add_message,
    create_conversation,
    get_conversation,
    get_thread_history,
    update_status,
)
from agent.core.email_drafter import draft_email
from agent.core.icp_classifier import classify_prospect
from agent.enrichment.signal_brief import generate_signal_brief
from agent.integrations.calcom import get_calcom_client
from agent.integrations.hubspot import get_hubspot_client
from agent.models import (
    ChannelType,
    ConversationState,
    ConversationStatus,
    EmailType,
    TraceRecord,
)

logger = logging.getLogger(__name__)


async def process_new_prospect(
    company_name: str | None = None,
    domain: str | None = None,
    crunchbase_id: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_title: str | None = None,
) -> dict:
    """
    Full pipeline for a new prospect:
    1. Enrich with signal brief
    2. Classify into ICP segment
    3. Draft outbound email
    4. Create HubSpot record and note
    5. Send outbound email
    6. Return the execution result
    """
    all_traces: list[TraceRecord] = []
    pipeline_start = datetime.now(UTC)

    logger.info("Starting enrichment for %s", company_name or domain)
    signal_brief, gap_brief, enrich_traces = await generate_signal_brief(
        company_name=company_name,
        domain=domain,
        crunchbase_id=crunchbase_id,
    )
    all_traces.extend(enrich_traces)

    if contact_name:
        signal_brief.prospect.contact_name = contact_name
    if contact_email:
        signal_brief.prospect.contact_email = contact_email
    if contact_title:
        signal_brief.prospect.contact_title = contact_title

    classification = classify_prospect(signal_brief)
    logger.info(
        "Classified %s as %s (confidence: %s)",
        signal_brief.prospect.company,
        classification.segment.value,
        classification.confidence.value,
    )

    conversation = create_conversation(
        prospect=signal_brief.prospect,
        channel=ChannelType.EMAIL,
    )
    conversation.signal_brief = signal_brief
    conversation.gap_brief = gap_brief
    conversation.classification = classification

    email_draft, draft_traces = await draft_email(
        signal_brief=signal_brief,
        classification=classification,
        email_type=EmailType.COLD,
        gap_brief=gap_brief,
        thread_id=conversation.thread_id,
    )
    all_traces.extend(draft_traces)

    hubspot_trace, note_trace = await _log_new_prospect_to_hubspot(
        conversation=conversation,
        email_draft=email_draft,
    )
    if hubspot_trace is not None:
        all_traces.append(hubspot_trace)
    if note_trace is not None:
        all_traces.append(note_trace)

    email_result = {"status": "skipped", "reason": "missing_contact_email"}
    if signal_brief.prospect.contact_email:
        email_result, email_trace = await send_email(
            to_email=signal_brief.prospect.contact_email,
            draft=email_draft,
        )
        all_traces.append(email_trace)

    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=email_draft.body,
        channel=ChannelType.EMAIL,
        metadata={
            "subject": email_draft.subject,
            "email_type": email_draft.email_type.value,
            "grounded_claims": [claim.model_dump() for claim in email_draft.grounded_claims],
            "delivery": email_result,
        },
    )
    update_status(conversation.thread_id, ConversationStatus.OUTBOUND_SENT)

    pipeline_latency_ms = (datetime.now(UTC) - pipeline_start).total_seconds() * 1000
    total_cost = sum(trace.cost_usd or 0 for trace in all_traces)

    logger.info(
        "Pipeline complete for %s: segment=%s, latency=%.0fms, cost=$%.4f",
        signal_brief.prospect.company,
        classification.segment.value,
        pipeline_latency_ms,
        total_cost,
    )

    return {
        "thread_id": conversation.thread_id,
        "prospect": signal_brief.prospect.model_dump(),
        "classification": classification.model_dump(),
        "signal_brief": signal_brief.model_dump(),
        "gap_brief": gap_brief.model_dump() if gap_brief else None,
        "email_draft": email_draft.model_dump(),
        "email_delivery": email_result,
        "hubspot_contact_id": conversation.hubspot_contact_id,
        "pipeline_latency_ms": pipeline_latency_ms,
        "trace_count": len(all_traces),
        "total_cost_usd": total_cost,
        "requires_human_review": signal_brief.requires_human_review or email_draft.handoff_to_human,
    }


async def handle_prospect_reply(
    thread_id: str,
    reply_content: str,
    channel: ChannelType = ChannelType.EMAIL,
) -> dict:
    """
    Handle a reply from a prospect in an existing conversation.
    This can lead to a warm email reply, SMS scheduling handoff, or direct
    Cal.com booking.
    """
    conversation = get_conversation(thread_id)
    if not conversation:
        raise ValueError(f"Conversation {thread_id} not found")

    add_message(thread_id, role="prospect", content=reply_content, channel=channel)
    await _log_reply_to_hubspot(conversation, reply_content, channel)

    if conversation.status == ConversationStatus.OPTED_OUT:
        return {
            "thread_id": thread_id,
            "action": "opted_out",
            "message": "Prospect opted out. No further messages will be sent.",
        }

    if _prospect_prefers_sms(reply_content) and conversation.prospect.contact_phone:
        return await _handoff_to_sms(conversation)

    if _has_scheduling_intent(reply_content):
        return await _book_discovery_call(conversation)

    if conversation.signal_brief and conversation.classification:
        thread_history = get_thread_history(thread_id)
        email_draft, _draft_traces = await draft_email(
            signal_brief=conversation.signal_brief,
            classification=conversation.classification,
            email_type=EmailType.WARM_REPLY,
            gap_brief=conversation.gap_brief,
            thread_history=thread_history,
            thread_id=thread_id,
        )

        email_result = {"status": "skipped", "reason": "missing_contact_email"}
        if conversation.prospect.contact_email:
            email_result, _email_trace = await send_email(
                to_email=conversation.prospect.contact_email,
                draft=email_draft,
            )

        add_message(
            thread_id=thread_id,
            role="agent",
            content=email_draft.body,
            channel=ChannelType.EMAIL,
            metadata={"email_type": "warm_reply", "delivery": email_result},
        )
        update_status(thread_id, ConversationStatus.QUALIFIED)

        if conversation.hubspot_contact_id:
            await get_hubspot_client().update_contact_status(
                conversation.hubspot_contact_id,
                "QUALIFIED",
            )

        return {
            "thread_id": thread_id,
            "action": "warm_reply",
            "email_draft": email_draft.model_dump(),
            "email_delivery": email_result,
            "handoff_to_human": email_draft.handoff_to_human,
        }

    return {
        "thread_id": thread_id,
        "action": "no_context",
        "message": "No signal brief available for this thread.",
    }


async def _log_new_prospect_to_hubspot(
    conversation: ConversationState,
    email_draft,
) -> tuple[TraceRecord | None, TraceRecord | None]:
    """Create the contact record and attach the initial outbound note."""
    if not conversation.prospect.contact_email:
        return None, None

    hubspot_client = get_hubspot_client()
    hubspot_contact, hubspot_trace = await hubspot_client.create_contact(
        prospect=conversation.prospect,
        signal_brief=conversation.signal_brief,
        classification=conversation.classification,
    )
    conversation.hubspot_contact_id = hubspot_contact.get("id")

    if not conversation.hubspot_contact_id:
        return hubspot_trace, None

    _, note_trace = await hubspot_client.add_note(
        contact_id=conversation.hubspot_contact_id,
        note_body=_build_outbound_note(conversation, email_draft),
        prospect_company=conversation.prospect.company,
    )
    return hubspot_trace, note_trace


async def _log_reply_to_hubspot(
    conversation: ConversationState,
    reply_content: str,
    channel: ChannelType,
) -> None:
    """Attach inbound replies to the HubSpot timeline when a contact exists."""
    if not conversation.hubspot_contact_id:
        return

    note_body = f"Prospect reply via {channel.value}: {reply_content}"
    await get_hubspot_client().add_note(
        contact_id=conversation.hubspot_contact_id,
        note_body=note_body,
        prospect_company=conversation.prospect.company,
    )


async def _handoff_to_sms(conversation: ConversationState) -> dict:
    """Switch warm-lead scheduling to SMS when the prospect asks for it."""
    sms_body = (
        "Happy to switch to SMS for scheduling. "
        f"You can also book directly here: {get_calcom_client().get_booking_link()}"
    )
    sms_result, _sms_trace = await send_sms(
        to_phone=conversation.prospect.contact_phone or "",
        message=sms_body,
        thread_id=conversation.thread_id,
    )
    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=sms_body,
        channel=ChannelType.SMS,
        metadata={"delivery": sms_result, "reason": "warm_lead_sms_fallback"},
    )
    update_status(conversation.thread_id, ConversationStatus.QUALIFIED)

    if conversation.hubspot_contact_id:
        await get_hubspot_client().update_contact_status(
            conversation.hubspot_contact_id,
            "QUALIFIED",
        )

    return {
        "thread_id": conversation.thread_id,
        "action": "sms_fallback",
        "sms_result": sms_result,
    }


async def _book_discovery_call(conversation: ConversationState) -> dict:
    """Book a simple default discovery slot and persist the outcome."""
    calcom_client = get_calcom_client()
    start_time, end_time = _default_booking_window()
    notes = (
        f"Thread {conversation.thread_id} | "
        f"Segment: {conversation.classification.segment.value if conversation.classification else 'unknown'}"
    )
    booking_result, booking_trace = await calcom_client.create_booking(
        prospect=conversation.prospect,
        start_time=start_time,
        end_time=end_time,
        notes=notes,
    )

    booking_id = booking_result.get("id") or booking_result.get("uid")
    conversation.calcom_booking_id = booking_id
    update_status(conversation.thread_id, ConversationStatus.CALL_BOOKED)

    confirmation = (
        "Booked a discovery call. "
        f"Start: {start_time}. Booking ID: {booking_id or 'pending_confirmation'}."
    )
    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=confirmation,
        channel=ChannelType.EMAIL,
        metadata={"booking": booking_result, "trace_id": booking_trace.trace_id},
    )

    if conversation.hubspot_contact_id:
        hubspot_client = get_hubspot_client()
        await hubspot_client.update_contact_status(conversation.hubspot_contact_id, "QUALIFIED")
        await hubspot_client.add_note(
            contact_id=conversation.hubspot_contact_id,
            note_body=confirmation,
            prospect_company=conversation.prospect.company,
        )

    return {
        "thread_id": conversation.thread_id,
        "action": "booked_call",
        "booking": booking_result,
        "calcom_booking_id": booking_id,
    }


def _build_outbound_note(conversation: ConversationState, email_draft) -> str:
    """Summarize the outbound action for CRM notes."""
    note = {
        "company": conversation.prospect.company,
        "segment": conversation.classification.segment.value if conversation.classification else None,
        "confidence": (
            conversation.classification.confidence.value if conversation.classification else None
        ),
        "subject": email_draft.subject,
        "grounded_claims": [claim.claim for claim in email_draft.grounded_claims],
        "requires_human_review": conversation.signal_brief.requires_human_review
        if conversation.signal_brief
        else False,
    }
    return json.dumps(note, ensure_ascii=True)


def _prospect_prefers_sms(reply_content: str) -> bool:
    lowered = reply_content.lower()
    return any(phrase in lowered for phrase in ("text me", "sms", "text is easier"))


def _has_scheduling_intent(reply_content: str) -> bool:
    lowered = reply_content.lower()
    return any(
        phrase in lowered
        for phrase in (
            "book",
            "schedule",
            "call",
            "meeting",
            "calendar",
            "availability",
            "available",
        )
    )


def _default_booking_window() -> tuple[str, str]:
    """Choose a simple default 30-minute booking slot one day ahead."""
    start = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=15,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = start + timedelta(minutes=30)
    return start.isoformat(), end.isoformat()
