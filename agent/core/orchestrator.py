"""
End-to-end orchestrator.
Wires together: enrichment → classification → drafting → sending → tracking.
This is the main workflow engine for the Conversion Engine.
"""

from __future__ import annotations

import logging
from datetime import datetime

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
from agent.models import (
    ChannelType,
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
    4. Create conversation thread
    5. Return everything for sending

    Returns a dict with all outputs for the caller to send and track.
    """
    all_traces: list[TraceRecord] = []
    pipeline_start = datetime.utcnow()

    # ── Step 1: Enrichment ──
    logger.info("Starting enrichment for %s", company_name or domain)
    signal_brief, gap_brief, enrich_traces = await generate_signal_brief(
        company_name=company_name,
        domain=domain,
        crunchbase_id=crunchbase_id,
    )
    all_traces.extend(enrich_traces)

    # Update prospect with contact info
    if contact_name:
        signal_brief.prospect.contact_name = contact_name
    if contact_email:
        signal_brief.prospect.contact_email = contact_email
    if contact_title:
        signal_brief.prospect.contact_title = contact_title

    # ── Step 2: ICP Classification ──
    classification = classify_prospect(signal_brief)
    logger.info(
        "Classified %s as %s (confidence: %s)",
        signal_brief.prospect.company,
        classification.segment.value,
        classification.confidence.value,
    )

    # ── Step 3: Create conversation ──
    conversation = create_conversation(
        prospect=signal_brief.prospect,
        channel=ChannelType.EMAIL,
    )

    # Store enrichment data in conversation
    conversation.signal_brief = signal_brief
    conversation.gap_brief = gap_brief
    conversation.classification = classification

    # ── Step 4: Draft email ──
    email_draft, draft_traces = await draft_email(
        signal_brief=signal_brief,
        classification=classification,
        email_type=EmailType.COLD,
        gap_brief=gap_brief,
        thread_id=conversation.thread_id,
    )
    all_traces.extend(draft_traces)

    # ── Step 5: Record in conversation ──
    add_message(
        thread_id=conversation.thread_id,
        role="agent",
        content=email_draft.body,
        metadata={
            "subject": email_draft.subject,
            "email_type": email_draft.email_type.value,
            "grounded_claims": [c.model_dump() for c in email_draft.grounded_claims],
        },
    )
    update_status(conversation.thread_id, ConversationStatus.OUTBOUND_SENT)

    # ── Compute pipeline latency ──
    pipeline_latency_ms = (datetime.utcnow() - pipeline_start).total_seconds() * 1000

    result = {
        "thread_id": conversation.thread_id,
        "prospect": signal_brief.prospect.model_dump(),
        "classification": classification.model_dump(),
        "signal_brief": signal_brief.model_dump(),
        "gap_brief": gap_brief.model_dump() if gap_brief else None,
        "email_draft": email_draft.model_dump(),
        "pipeline_latency_ms": pipeline_latency_ms,
        "trace_count": len(all_traces),
        "total_cost_usd": sum(t.cost_usd or 0 for t in all_traces),
        "requires_human_review": signal_brief.requires_human_review or email_draft.handoff_to_human,
    }

    logger.info(
        "Pipeline complete for %s: segment=%s, latency=%.0fms, cost=$%.4f",
        signal_brief.prospect.company,
        classification.segment.value,
        pipeline_latency_ms,
        result["total_cost_usd"],
    )

    return result


async def handle_prospect_reply(
    thread_id: str,
    reply_content: str,
    channel: ChannelType = ChannelType.EMAIL,
) -> dict:
    """
    Handle a reply from a prospect in an existing conversation.
    1. Parse the reply
    2. Update conversation state
    3. Draft a warm reply
    4. Check for scheduling / booking intent
    """
    conversation = get_conversation(thread_id)
    if not conversation:
        raise ValueError(f"Conversation {thread_id} not found")

    # Add the prospect's reply
    add_message(thread_id, role="prospect", content=reply_content, channel=channel)

    # Check for opt-out
    if conversation.status == ConversationStatus.OPTED_OUT:
        logger.info("Prospect opted out in thread %s", thread_id)
        return {
            "thread_id": thread_id,
            "action": "opted_out",
            "message": "Prospect opted out. No further messages will be sent.",
        }

    # Draft warm reply
    if conversation.signal_brief and conversation.classification:
        thread_history = get_thread_history(thread_id)

        email_draft, _traces = await draft_email(
            signal_brief=conversation.signal_brief,
            classification=conversation.classification,
            email_type=EmailType.WARM_REPLY,
            gap_brief=conversation.gap_brief,
            thread_history=thread_history,
            thread_id=thread_id,
        )

        add_message(
            thread_id=thread_id,
            role="agent",
            content=email_draft.body,
            metadata={"email_type": "warm_reply"},
        )
        update_status(thread_id, ConversationStatus.QUALIFIED)

        return {
            "thread_id": thread_id,
            "action": "warm_reply",
            "email_draft": email_draft.model_dump(),
            "handoff_to_human": email_draft.handoff_to_human,
        }

    return {
        "thread_id": thread_id,
        "action": "no_context",
        "message": "No signal brief available for this thread.",
    }
