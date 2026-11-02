"""
Follow-ups for cold threads that got no reply.

FOLLOW_UP_DAYS (default "3,7") is the schedule: the first follow-up goes out
3 days after the last email we sent, the second 7 days after that. When the
last wait passes in silence, the thread is marked stalled and left alone.

A follow-up is only drafted for a thread that:
  - is still `outbound_sent`: any reply or opt-out moves it on, which stops
    the sequence
  - has no email waiting in the review queue
  - is not on the suppression list
Timing counts from when an email was actually sent (`sent_at`), not from when
it was drafted, so time spent in review never shortens the gap.

Follow-ups are drafted like any email (re_engagement type) and go through the
review queue when REQUIRE_APPROVAL is on. When the daily sending limit or a
pause is holding live email, the run stops without drafting anything.

Run it from a cron (POST /api/follow-ups/run) or let the app run it every
FOLLOW_UP_INTERVAL_MINUTES.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agent.config import settings
from agent.core.conversation import add_message, get_active_conversations, update_status
from agent.core.email_drafter import draft_email
from agent.core.orchestrator import delivered, send_or_queue
from agent.core.sending import get_sending_guard
from agent.core.suppression import get_suppression_list
from agent.models import ChannelType, ConversationState, ConversationStatus, EmailType

logger = logging.getLogger(__name__)


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _next_action(conversation: ConversationState, now: datetime, schedule: list[int]) -> str:
    """'follow_up', 'stall' or a reason to skip."""
    if any(m.role == "prospect" for m in conversation.messages):
        return "has_reply"
    emails = [
        m for m in conversation.messages if m.role == "agent" and m.channel == ChannelType.EMAIL
    ]
    if any(m.metadata.get("delivery", {}).get("status") == "pending_review" for m in emails):
        return "awaiting_review"
    sent = [m for m in emails if m.metadata.get("sent_at")]
    if not sent:
        return "nothing_sent"
    last_sent = max(_parse_time(m.metadata["sent_at"]) for m in sent)
    follow_ups = sum(m.metadata.get("email_type") == EmailType.RE_ENGAGEMENT.value for m in sent)

    if follow_ups >= len(schedule):
        return "stall" if now - last_sent >= timedelta(days=schedule[-1]) else "sequence_done"
    if now - last_sent < timedelta(days=schedule[follow_ups]):
        return "not_due"
    if not (conversation.signal_brief and conversation.classification):
        return "no_context"
    return "follow_up"


async def run_follow_ups(now: datetime | None = None) -> dict:
    """Draft (and send or queue) every follow-up that is due. Returns a summary."""
    now = now or datetime.now(UTC)
    schedule = settings.follow_up_schedule
    summary: dict = {"checked": 0, "follow_ups": [], "stalled": [], "skipped": {}, "held": None}
    if not schedule:
        return summary

    for conversation in get_active_conversations(status=ConversationStatus.OUTBOUND_SENT):
        summary["checked"] += 1
        action = _next_action(conversation, now, schedule)
        email = conversation.prospect.contact_email
        if action == "follow_up" and (not email or get_suppression_list().is_suppressed(email)):
            action = "suppressed"

        if action == "stall":
            update_status(conversation.thread_id, ConversationStatus.STALLED)
            summary["stalled"].append(conversation.thread_id)
            continue
        if action != "follow_up":
            summary["skipped"][action] = summary["skipped"].get(action, 0) + 1
            continue

        if settings.live_outbound_enabled:
            allowed, reason = get_sending_guard().check()
            if not allowed:
                summary["held"] = reason
                logger.warning("Follow-ups held: %s", reason)
                break

        await _send_follow_up(conversation, summary, now)
    return summary


async def _send_follow_up(conversation: ConversationState, summary: dict, now: datetime) -> None:
    number = (
        sum(
            m.metadata.get("email_type") == EmailType.RE_ENGAGEMENT.value
            for m in conversation.messages
        )
        + 1
    )
    history = [
        {"role": m.role, "content": m.content, "timestamp": m.timestamp}
        for m in conversation.messages
    ]
    draft, _traces = await draft_email(
        signal_brief=conversation.signal_brief,
        classification=conversation.classification,
        email_type=EmailType.RE_ENGAGEMENT,
        thread_history=history,
        thread_id=conversation.thread_id,
    )
    result, _trace = await send_or_queue(conversation, draft)
    metadata = {
        "subject": draft.subject,
        "email_type": EmailType.RE_ENGAGEMENT.value,
        "follow_up_number": number,
        "delivery": result,
        "review_id": result.get("review_id"),
    }
    if delivered(result):
        metadata["sent_at"] = now.isoformat()
    add_message(
        conversation.thread_id,
        role="agent",
        content=draft.body,
        channel=ChannelType.EMAIL,
        metadata=metadata,
    )
    summary["follow_ups"].append(
        {"thread_id": conversation.thread_id, "number": number, "delivery": result["status"]}
    )
    logger.info("Follow-up %d for %s: %s", number, conversation.prospect.company, result["status"])
