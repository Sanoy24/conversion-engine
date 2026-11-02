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
from agent.channels.handoff import HandoffAction, decide_handoff_action
from agent.channels.sms_handler import send_sms
from agent.config import settings
from agent.core.conversation import (
    add_message,
    create_conversation,
    get_conversation,
    get_conversation_by_booking_id,
    get_thread_history,
    save_conversation,
    update_status,
)
from agent.core.email_drafter import draft_email
from agent.core.icp_classifier import classify_prospect
from agent.core.review import ReviewItem, get_review_queue, new_review_item
from agent.core.suppression import (
    REPLY_OPT_OUT,
    SMS_STOP,
    UNSUBSCRIBE_LINK,
    get_suppression_list,
)
from agent.enrichment.signal_brief import generate_signal_brief
from agent.integrations.calendar import get_calendar_client
from agent.integrations.crm import get_crm_client
from agent.models import (
    ChannelType,
    Confidence,
    ConversationState,
    ConversationStatus,
    EmailType,
    TraceRecord,
)
from agent.playbook import ABSTAIN, get_playbook

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
    4. Create CRM record and note
    5. Send outbound email
    6. Return the execution result
    """
    if contact_email and get_suppression_list().is_suppressed(contact_email):
        # Checked before enrichment so a suppressed person costs no LLM calls
        # and never gets a new CRM record.
        logger.info("Skipping prospect %s: contact is suppressed", company_name or domain)
        return {
            "status": "suppressed",
            "contact_email": contact_email,
            "suppression": get_suppression_list().get(contact_email),
        }

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
        classification.segment,
        classification.confidence.value,
    )

    conversation = create_conversation(
        prospect=signal_brief.prospect,
        channel=ChannelType.EMAIL,
    )
    conversation.signal_brief = signal_brief
    conversation.gap_brief = gap_brief
    conversation.classification = classification
    save_conversation(conversation)

    email_draft, draft_traces = await draft_email(
        signal_brief=signal_brief,
        classification=classification,
        email_type=EmailType.COLD,
        gap_brief=gap_brief,
        thread_id=conversation.thread_id,
    )
    all_traces.extend(draft_traces)

    crm_trace, note_trace = await _log_new_prospect_to_crm(
        conversation=conversation,
        email_draft=email_draft,
    )
    if crm_trace is not None:
        all_traces.append(crm_trace)
    if note_trace is not None:
        all_traces.append(note_trace)

    email_result, email_trace = await send_or_queue(conversation, email_draft)
    if email_trace is not None:
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
            "review_id": email_result.get("review_id"),
            **_sent_at(email_result),
        },
    )
    update_status(
        conversation.thread_id,
        ConversationStatus.PENDING_REVIEW
        if email_result["status"] == "pending_review"
        else ConversationStatus.OUTBOUND_SENT,
    )

    pipeline_latency_ms = (datetime.now(UTC) - pipeline_start).total_seconds() * 1000
    total_cost = sum(trace.cost_usd or 0 for trace in all_traces)

    logger.info(
        "Pipeline complete for %s: segment=%s, latency=%.0fms, cost=$%.4f",
        signal_brief.prospect.company,
        classification.segment,
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
        "crm_contact_id": conversation.crm_contact_id,
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
    booking (Cal.com or Calendly).
    """
    conversation = get_conversation(thread_id)
    if not conversation:
        raise ValueError(f"Conversation {thread_id} not found")

    add_message(thread_id, role="prospect", content=reply_content, channel=channel)
    await _log_reply_to_crm(conversation, reply_content, channel)

    if conversation.status == ConversationStatus.OPTED_OUT:
        # Opting out in one thread opts the address out everywhere.
        address = (
            conversation.prospect.contact_phone
            if channel == ChannelType.SMS
            else conversation.prospect.contact_email
        )
        if address:
            get_suppression_list().add(
                address, channel=channel.value, reason=REPLY_OPT_OUT, thread_id=thread_id
            )
        return {
            "thread_id": thread_id,
            "action": "opted_out",
            "message": "Prospect opted out. No further messages will be sent.",
        }

    handoff_action = decide_handoff_action(
        reply_content,
        channel=channel,
        has_phone=bool(conversation.prospect.contact_phone),
    )
    if handoff_action == HandoffAction.SMS_FALLBACK:
        return await _handoff_to_sms(conversation)

    if handoff_action == HandoffAction.BOOK_CALL:
        # Qualification gate (qualify over a few turns, then book).
        # Only proceed to booking if the prospect has been through at least one
        # warm qualifying exchange. If not, fall through to a warm reply — the
        # LLM will acknowledge the scheduling interest and propose times, giving
        # us the qualifying turn before the next booking attempt.
        if _is_qualified_for_booking(conversation):
            return await _book_discovery_call(conversation, reply_content=reply_content)
        logger.info(
            "Booking intent detected for %s but qualification gate not met — "
            "routing to warm reply first.",
            conversation.prospect.company,
        )
        # Fall through to warm-reply path below

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

        email_result, _email_trace = await send_or_queue(conversation, email_draft)

        add_message(
            thread_id=thread_id,
            role="agent",
            content=email_draft.body,
            channel=ChannelType.EMAIL,
            metadata={
                "subject": email_draft.subject,
                "email_type": "warm_reply",
                "delivery": email_result,
                "review_id": email_result.get("review_id"),
                **_sent_at(email_result),
            },
        )
        update_status(thread_id, ConversationStatus.QUALIFIED)

        if conversation.crm_contact_id:
            await get_crm_client().update_contact_status(
                conversation.crm_contact_id,
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


def delivered(result: dict) -> bool:
    """Whether a send_email result means the email went out (or to the sink)."""
    return result.get("status") in ("sent", "sink")


def _sent_at(result: dict) -> dict:
    """`sent_at` metadata for a delivered email; follow-up timing counts from it."""
    return {"sent_at": datetime.now(UTC).isoformat()} if delivered(result) else {}


async def send_or_queue(conversation: ConversationState, draft) -> tuple[dict, TraceRecord | None]:
    """Send a drafted email now, or queue it for review when REQUIRE_APPROVAL is on."""
    to_email = conversation.prospect.contact_email
    if not to_email:
        return {"status": "skipped", "reason": "missing_contact_email"}, None
    if settings.require_approval:
        item = new_review_item(
            thread_id=conversation.thread_id,
            to=to_email,
            draft=draft,
            prospect_company=conversation.prospect.company,
            needs_attention=draft.handoff_reason if draft.handoff_to_human else None,
        )
        get_review_queue().save(item)
        logger.info("Draft for %s queued for review (%s)", conversation.prospect.company, item.id)
        return {"status": "pending_review", "review_id": item.id}, None
    return await send_email(to_email=to_email, draft=draft)


class ReviewError(ValueError):
    """The review item is missing or no longer pending."""


def _claim_pending(review_id: str) -> ReviewItem:
    queue = get_review_queue()
    item = queue.get(review_id)
    if item is None:
        raise ReviewError(f"Review item {review_id} not found")
    if item.status != "pending":
        raise ReviewError(f"Review item {review_id} is already {item.status}")
    return item


def _record_review_outcome(item: ReviewItem, sent: bool) -> None:
    """Reflect an approval or rejection on the conversation thread."""
    conversation = get_conversation(item.thread_id)
    if conversation is None:
        return
    for message in conversation.messages:
        if message.metadata.get("review_id") == item.id:
            message.content = item.body
            message.metadata["subject"] = item.subject
            message.metadata["delivery"] = item.delivery or {"status": item.status}
            message.metadata["edited"] = item.edited
            if sent:
                message.metadata["sent_at"] = item.decided_at
    save_conversation(conversation)
    if conversation.status == ConversationStatus.PENDING_REVIEW:
        update_status(
            item.thread_id, ConversationStatus.OUTBOUND_SENT if sent else ConversationStatus.NEW
        )


async def approve_review(
    review_id: str, subject: str | None = None, body: str | None = None
) -> ReviewItem:
    """Approve a queued draft (optionally edited) and send it."""
    queue = get_review_queue()
    item = _claim_pending(review_id)
    item.status = "sending"  # claimed before the await, so a second approve can't double-send
    queue.save(item)

    if subject is not None and subject.strip() and subject != item.subject:
        item.subject, item.edited = subject, True
    if body is not None and body.strip() and body != item.body:
        item.body, item.edited = body, True
    draft = item.draft.model_copy(update={"subject": item.subject, "body": item.body})

    result, _trace = await send_email(to_email=item.to, draft=draft)
    if result.get("status") == "held":
        # Daily limit or paused sending: keep the (edited) draft in the queue.
        item.status = "pending"
        queue.save(item)
        raise ReviewError(f"Not sent, still pending: {result['reason']}")
    item.delivery = result
    item.status = "sent" if delivered(result) else "failed"
    item.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
    queue.save(item)
    _record_review_outcome(item, sent=item.status == "sent")
    logger.info("Review %s approved (edited=%s): %s", review_id, item.edited, item.status)
    return item


def reject_review(review_id: str, reason: str | None = None) -> ReviewItem:
    """Reject a queued draft; nothing is sent."""
    item = _claim_pending(review_id)
    item.status = "rejected"
    item.reject_reason = reason
    item.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
    get_review_queue().save(item)
    _record_review_outcome(item, sent=False)
    logger.info("Review %s rejected: %s", review_id, reason or "no reason given")
    return item


async def _log_new_prospect_to_crm(
    conversation: ConversationState,
    email_draft,
) -> tuple[TraceRecord | None, TraceRecord | None]:
    """Create the contact record and attach the initial outbound note."""
    if not conversation.prospect.contact_email:
        return None, None

    crm = get_crm_client()
    crm_contact, crm_trace = await crm.create_contact(
        prospect=conversation.prospect,
        signal_brief=conversation.signal_brief,
        classification=conversation.classification,
    )
    conversation.crm_contact_id = crm_contact.get("id")
    save_conversation(conversation)

    if not conversation.crm_contact_id:
        return crm_trace, None

    _, note_trace = await crm.add_note(
        contact_id=conversation.crm_contact_id,
        note_body=_build_outbound_note(conversation, email_draft),
        prospect_company=conversation.prospect.company,
    )
    return crm_trace, note_trace


async def _log_reply_to_crm(
    conversation: ConversationState,
    reply_content: str,
    channel: ChannelType,
) -> None:
    """Attach inbound replies to the CRM timeline when a contact exists."""
    if not conversation.crm_contact_id:
        return

    note_body = f"Prospect reply via {channel.value}: {reply_content}"
    await get_crm_client().add_note(
        contact_id=conversation.crm_contact_id,
        note_body=note_body,
        prospect_company=conversation.prospect.company,
    )


async def _handoff_to_sms(conversation: ConversationState) -> dict:
    """Switch warm-lead scheduling to SMS when the prospect asks for it."""
    warm_lead_confirmed = _is_warm_lead_for_sms(conversation)
    if not warm_lead_confirmed:
        return {
            "thread_id": conversation.thread_id,
            "action": "sms_blocked",
            "reason": "sms_requires_prior_email_reply",
        }

    sms_body = (
        "Happy to switch to SMS for scheduling. "
        f"You can also book directly here: {get_calendar_client().get_booking_link()}"
    )
    sms_result, _sms_trace = await send_sms(
        to_phone=conversation.prospect.contact_phone or "",
        message=sms_body,
        thread_id=conversation.thread_id,
        warm_lead=warm_lead_confirmed,
    )
    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=sms_body,
        channel=ChannelType.SMS,
        metadata={"delivery": sms_result, "reason": "warm_lead_sms_fallback"},
    )
    update_status(conversation.thread_id, ConversationStatus.QUALIFIED)

    if conversation.crm_contact_id:
        await get_crm_client().update_contact_status(
            conversation.crm_contact_id,
            "QUALIFIED",
        )

    return {
        "thread_id": conversation.thread_id,
        "action": "sms_fallback",
        "sms_result": sms_result,
    }


async def _book_discovery_call(
    conversation: ConversationState,
    reply_content: str = "",
) -> dict:
    """
    Book a discovery slot, add the SDR as a co-attendee, and send the
    prospect a human-readable confirmation with the actual booked time.

    Both the prospect AND a designated SDR should
    receive a calendar invite. This is satisfied by passing sdr_email as
    a guest on the booking when the setting is configured.

    Future improvement: parse reply_content for an explicit time preference
    (e.g. "Thursday at 2pm") and use get_available_slots() to find the
    nearest matching real slot instead of the default window.
    """
    calendar = get_calendar_client()
    start_time, end_time = _default_booking_window()

    segment_label = (
        conversation.classification.segment if conversation.classification else "unknown"
    )
    notes = f"Thread {conversation.thread_id} | Segment: {segment_label}" + (
        f" | Prospect note: {reply_content[:200]}" if reply_content else ""
    )

    sdr_email = settings.sdr_email or None
    booking_result, booking_trace = await calendar.create_booking(
        prospect=conversation.prospect,
        start_time=start_time,
        end_time=end_time,
        notes=notes,
        thread_id=conversation.thread_id,
        sdr_email=sdr_email,
    )

    # Unwrap v2 envelope {"status": "success", "data": {...}}
    booking_data = booking_result.get("data", booking_result)
    booking_id = booking_data.get("id") or booking_data.get("uid")
    conversation.calcom_booking_id = booking_id
    save_conversation(conversation)
    update_status(conversation.thread_id, ConversationStatus.CALL_BOOKED)

    # Human-readable confirmation so the prospect knows the exact time —
    # not just a raw ISO string. The SDR invite goes via the calendar provider.
    human_time = _format_booking_time(start_time)
    confirmation = (
        f"I've booked a 30-minute discovery call for {human_time}. "
        "You'll receive a calendar invite at the email on this thread. "
        + (f"Our team lead ({sdr_email}) will also be on the invite. " if sdr_email else "")
        + f"Booking reference: {booking_id or 'pending'}."
    )
    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=confirmation,
        channel=ChannelType.EMAIL,
        metadata={
            "email_type": "booking_confirmation",
            "booking": booking_data,
            "trace_id": booking_trace.trace_id,
            "sdr_guest": sdr_email,
        },
    )

    if conversation.crm_contact_id:
        crm = get_crm_client()
        await crm.update_contact_status(conversation.crm_contact_id, "QUALIFIED")
        await crm.add_note(
            contact_id=conversation.crm_contact_id,
            note_body=confirmation,
            prospect_company=conversation.prospect.company,
        )

    logger.info(
        "Discovery call booked for %s: id=%s time=%s sdr=%s",
        conversation.prospect.company,
        booking_id,
        start_time,
        sdr_email or "none",
    )

    return {
        "thread_id": conversation.thread_id,
        "action": "booked_call",
        "booking": booking_data,
        "calcom_booking_id": booking_id,
        "booked_time": start_time,
        "human_time": human_time,
        "sdr_guest": sdr_email,
        "confirmation_sent": confirmation,
    }


async def handle_calcom_event(
    *,
    trigger: str,
    booking_payload: dict,
) -> dict:
    """
    Handle Cal.com webhook events and propagate confirmation state to CRM/thread.
    """
    trigger_upper = (trigger or "").upper()
    booking_uid = booking_payload.get("uid") or booking_payload.get("id") or ""
    metadata = booking_payload.get("metadata") or {}
    thread_id = metadata.get("thread_id")
    conversation = (
        get_conversation(thread_id) if thread_id else get_conversation_by_booking_id(booking_uid)
    )
    if not conversation:
        return {
            "status": "ok",
            "event": "unmatched_booking",
            "trigger": trigger_upper,
            "booking_uid": booking_uid,
        }

    if trigger_upper == "BOOKING_CREATED":
        conversation.calcom_booking_id = booking_uid or conversation.calcom_booking_id
        save_conversation(conversation)
        update_status(conversation.thread_id, ConversationStatus.CALL_BOOKED)
        if conversation.crm_contact_id:
            await get_crm_client().update_contact_status(conversation.crm_contact_id, "QUALIFIED")
            await get_crm_client().add_note(
                contact_id=conversation.crm_contact_id,
                note_body=f"Cal.com confirmed booking {booking_uid}",
                prospect_company=conversation.prospect.company,
            )
        return {
            "status": "ok",
            "event": "booking_created",
            "thread_id": conversation.thread_id,
            "booking_uid": booking_uid,
        }

    if trigger_upper == "BOOKING_CANCELLED":
        update_status(conversation.thread_id, ConversationStatus.QUALIFIED)
        if conversation.crm_contact_id:
            await get_crm_client().add_note(
                contact_id=conversation.crm_contact_id,
                note_body=f"Cal.com booking cancelled: {booking_uid}",
                prospect_company=conversation.prospect.company,
            )
        return {
            "status": "ok",
            "event": "booking_cancelled",
            "thread_id": conversation.thread_id,
            "booking_uid": booking_uid,
        }

    if trigger_upper == "BOOKING_RESCHEDULED":
        update_status(conversation.thread_id, ConversationStatus.CALL_BOOKED)
        if conversation.crm_contact_id:
            await get_crm_client().add_note(
                contact_id=conversation.crm_contact_id,
                note_body=f"Cal.com booking rescheduled: {booking_uid}",
                prospect_company=conversation.prospect.company,
            )
        return {
            "status": "ok",
            "event": "booking_rescheduled",
            "thread_id": conversation.thread_id,
            "booking_uid": booking_uid,
        }

    return {
        "status": "ok",
        "event": "unhandled",
        "trigger": trigger_upper,
        "thread_id": conversation.thread_id,
        "booking_uid": booking_uid,
    }


async def handle_inbound_sms(from_phone: str, message: str) -> dict:
    """
    Downstream handler for inbound SMS with no matching existing conversation.

    Opens a new inbound-originated conversation thread so the reply doesn't
    dead-end, writes a CRM note if we can resolve the contact by phone,
    and logs the trace. This is the application-level handler that the SMS
    webhook routes to when `get_conversation_by_phone` returns None.

    Kept deliberately small: it records state and emits a trace. Drafting a
    reply for unknown senders is not wired up yet.
    """
    logger.info("Inbound SMS: opening new thread for %s (len=%d)", from_phone, len(message))

    # Create a minimal conversation so subsequent replies can correlate.
    from agent.models import ProspectInfo

    prospect = ProspectInfo(
        company=f"SMS-Inbound-{from_phone[-4:] if from_phone else 'unknown'}",
        contact_phone=from_phone,
    )
    conversation = create_conversation(
        prospect=prospect,
        channel=ChannelType.SMS,
        initial_message=message,
    )
    # create_conversation already seeds the initial inbound SMS message.
    if conversation.messages:
        conversation.messages[0].metadata.update(
            {"inbound_source": "sms_webhook", "from_phone": from_phone}
        )

    # Best-effort CRM search — if the phone matches an existing contact,
    # attach a note recording the inbound SMS.
    try:
        contact = await get_crm_client().search_contact(email=from_phone)  # phone-fallback
        if contact and contact.get("id"):
            await get_crm_client().add_note(
                contact_id=contact["id"],
                note_body=f"Inbound SMS from {from_phone}: {message[:500]}",
                prospect_company=prospect.company,
            )
    except Exception as e:
        logger.debug("CRM attach for inbound SMS skipped: %s", e)

    return {
        "thread_id": conversation.thread_id,
        "action": "new_inbound_thread_opened",
        "channel": "sms",
        "message_length": len(message),
    }


async def handle_sms_opt_out(from_phone: str) -> dict:
    """
    TCPA-compliant STOP handler. Marks any conversation bound to this phone
    as opted-out and emits a trace. Downstream CRM update best-effort.
    """
    logger.info("SMS opt-out: %s", from_phone)
    from agent.core.conversation import get_conversation_by_phone

    conversation = get_conversation_by_phone(from_phone) if from_phone else None
    if from_phone:
        # Suppress even without a matching thread: STOP must always stick.
        get_suppression_list().add(
            from_phone,
            channel="sms",
            reason=SMS_STOP,
            thread_id=conversation.thread_id if conversation else None,
        )
    if conversation:
        update_status(conversation.thread_id, ConversationStatus.OPTED_OUT)
        add_message(
            thread_id=conversation.thread_id,
            role="prospect",
            content="STOP",
            channel=ChannelType.SMS,
            metadata={"opt_out": True},
        )
        if conversation.crm_contact_id:
            try:
                await get_crm_client().update_contact_status(
                    conversation.crm_contact_id,
                    "UNQUALIFIED",
                    properties={"hs_lead_status": "UNQUALIFIED"},
                )
            except Exception as e:
                logger.warning("CRM opt-out update failed: %s", e)
    return {
        "action": "opt_out",
        "from_phone": from_phone,
        "thread_id": conversation.thread_id if conversation else None,
    }


async def handle_unsubscribe(email: str, reason: str = UNSUBSCRIBE_LINK) -> dict:
    """
    Suppress an email address and close every thread addressed to it.
    Called from the unsubscribe link; CRM update is best-effort.
    """
    from agent.core.conversation import get_active_conversations

    newly_added = get_suppression_list().add(email, channel="email", reason=reason)
    closed = []
    for conversation in get_active_conversations():
        if (conversation.prospect.contact_email or "").lower() != email.lower():
            continue
        if conversation.status != ConversationStatus.OPTED_OUT:
            update_status(conversation.thread_id, ConversationStatus.OPTED_OUT)
            closed.append(conversation.thread_id)
        if conversation.crm_contact_id:
            try:
                await get_crm_client().update_contact_status(
                    conversation.crm_contact_id,
                    "UNQUALIFIED",
                    properties={"hs_lead_status": "UNQUALIFIED"},
                )
            except Exception as e:
                logger.warning("CRM unsubscribe update failed: %s", e)
    return {"action": "unsubscribed", "newly_suppressed": newly_added, "threads_closed": closed}


async def handle_sms_help(from_phone: str) -> dict:
    """TCPA HELP handler. Sends a canned response and logs the event."""
    logger.info("SMS HELP: %s", from_phone)
    company = get_playbook().company
    help_text = f"{company.name} outreach. Reply STOP to opt out."
    if company.support_email:
        help_text += f" For support, email {company.support_email}."
    try:
        await send_sms(
            to_phone=from_phone,
            message=help_text,
            thread_id=None,
            warm_lead=True,  # HELP replies are allowed by TCPA regardless of channel state
        )
    except Exception as e:
        logger.warning("HELP reply send failed: %s", e)
    return {"action": "help_sent", "from_phone": from_phone}


def _build_outbound_note(conversation: ConversationState, email_draft) -> str:
    """Summarize the outbound action for CRM notes."""
    needs_review = bool(
        conversation.signal_brief and conversation.signal_brief.requires_human_review
    )
    # P003 fix: when human review is required (e.g. founder departure, ambiguous
    # leadership transition), stamp the segment as PENDING_HUMAN_REVIEW so that
    # future nurture sequences do not act on a label that hasn't been confirmed.
    if needs_review:
        segment_value = "PENDING_HUMAN_REVIEW"
        confidence_value = None
    else:
        segment_value = conversation.classification.segment if conversation.classification else None
        confidence_value = (
            conversation.classification.confidence.value if conversation.classification else None
        )

    note = {
        "company": conversation.prospect.company,
        "segment": segment_value,
        "confidence": confidence_value,
        "subject": email_draft.subject,
        "grounded_claims": [claim.claim for claim in email_draft.grounded_claims],
        "requires_human_review": needs_review,
        "human_review_reason": (
            conversation.signal_brief.human_review_reason if conversation.signal_brief else None
        ),
    }
    return json.dumps(note, ensure_ascii=True)


def _is_qualified_for_booking(conversation: ConversationState) -> bool:
    """
    Qualification gate — returns True only when the prospect has been
    warmed up enough to justify booking a cal slot directly.

    Rules (any single rule = pass):

    1. Status is QUALIFIED or CALL_BOOKED: a prior warm-reply exchange
       already happened; the prospect has shown sustained intent.

    2. Classification confidence is HIGH and segment is not ABSTAIN: the
       ICP signal is strong enough to fast-track (skips one warm turn).

    3. At least one agent warm_reply message exists in the thread: the
       drafter already sent a qualifying follow-up, prospect replied again.

    If none of the rules pass the caller falls back to a warm reply, which
    acts as the qualifying turn. On the next scheduling-intent reply the
    gate will pass via Rule 1 (status=QUALIFIED) or Rule 3 (warm_reply exists).
    """
    # Rule 1 — prior warm exchange confirmed by status
    if conversation.status in (ConversationStatus.QUALIFIED, ConversationStatus.CALL_BOOKED):
        return True

    # Rule 2 — high-confidence ICP: skip one warm turn
    if (
        conversation.classification is not None
        and conversation.classification.confidence == Confidence.HIGH
        and conversation.classification.segment != ABSTAIN
    ):
        return True

    # Rule 3 — at least one warm reply already in the thread
    warm_replies = [
        m
        for m in conversation.messages
        if m.role == "agent" and m.metadata.get("email_type") == "warm_reply"
    ]
    return bool(warm_replies)


def _format_booking_time(iso_time: str) -> str:
    """
    Convert an ISO-8601 UTC string to a human-readable booking confirmation.
    Example: '2026-04-28T15:00:00+00:00' → 'Tuesday 28 Apr at 15:00 UTC'
    """
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return dt.strftime("%A %d %b at %H:%M UTC")
    except Exception:
        return iso_time


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


def _is_warm_lead_for_sms(conversation: ConversationState) -> bool:
    if conversation.status in (
        ConversationStatus.REPLIED,
        ConversationStatus.QUALIFIED,
        ConversationStatus.CALL_BOOKED,
    ):
        return True
    return any(
        msg.role == "prospect" and msg.channel == ChannelType.EMAIL for msg in conversation.messages
    )
