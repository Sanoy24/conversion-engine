"""
Centralized channel handoff policy.

Keeps channel transition logic out of individual handlers so email/SMS flows use
the same decision policy.
"""

from __future__ import annotations

from enum import StrEnum

from agent.models import ChannelType


class HandoffAction(StrEnum):
    WARM_REPLY = "warm_reply"
    SMS_FALLBACK = "sms_fallback"
    BOOK_CALL = "book_call"


def decide_handoff_action(
    reply_content: str,
    *,
    channel: ChannelType,
    has_phone: bool,
) -> HandoffAction:
    """
    Decide next action for an inbound reply using a single policy entrypoint.
    """
    lowered = reply_content.lower()
    sms_requested = any(phrase in lowered for phrase in ("text me", "sms", "text is easier"))
    scheduling_intent = any(
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

    # SMS is only a warm scheduling channel after prior email engagement.
    if sms_requested and has_phone and channel == ChannelType.EMAIL:
        return HandoffAction.SMS_FALLBACK

    if scheduling_intent:
        return HandoffAction.BOOK_CALL

    return HandoffAction.WARM_REPLY
