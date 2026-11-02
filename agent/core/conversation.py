"""
Conversation State Manager.
Tracks multi-turn, multi-channel conversations with prospects.
Persists state for each thread, prevents multi-thread leakage.

Storage is pluggable (see `conversation_store`). Functions here save after
every change they make; callers that edit a `ConversationState` in place must
call `save_conversation()` afterwards for the change to survive a restart.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from agent.config import settings
from agent.core.conversation_store import ConversationStore, create_store
from agent.models import (
    ChannelType,
    ConversationMessage,
    ConversationState,
    ConversationStatus,
    ProspectInfo,
)

logger = logging.getLogger(__name__)

_store: ConversationStore | None = None


def get_store() -> ConversationStore:
    """The process-wide conversation store, created on first use from settings."""
    global _store
    if _store is None:
        _store = create_store(settings.conversation_db_path)
    return _store


def set_store(store: ConversationStore) -> None:
    """Replace the process-wide store (used by tests and embedding apps)."""
    global _store
    _store = store


def save_conversation(state: ConversationState) -> None:
    """Persist a conversation after editing it in place."""
    get_store().save(state)


def create_conversation(
    prospect: ProspectInfo,
    channel: ChannelType = ChannelType.EMAIL,
    initial_message: str | None = None,
) -> ConversationState:
    """Create a new conversation thread for a prospect."""
    thread_id = f"thread_{uuid.uuid4().hex[:8]}"
    state = ConversationState(
        thread_id=thread_id,
        prospect=prospect,
        channel=channel,
    )

    if initial_message:
        state.messages.append(
            ConversationMessage(
                role="prospect",
                channel=channel,
                content=initial_message,
                metadata={"seeded_on_create": True},
            )
        )

    save_conversation(state)

    logger.info("Created conversation %s for %s via %s", thread_id, prospect.company, channel.value)
    return state


def get_conversation(thread_id: str) -> ConversationState | None:
    """Get a conversation by thread ID."""
    return get_store().get(thread_id)


def get_conversation_by_phone(phone: str) -> ConversationState | None:
    """
    Look up the most recently updated conversation for a given phone number.

    Used by the inbound-SMS webhook to correlate a reply back to an existing
    thread so it can be routed through `handle_prospect_reply`. Returns None
    if no conversation has that phone recorded (e.g. someone texted the
    shortcode without a prior email outreach).
    """
    if not phone:
        return None
    matches = [c for c in get_store().all() if c.prospect and c.prospect.contact_phone == phone]
    if not matches:
        return None
    return max(matches, key=lambda c: c.updated_at)


def get_conversation_by_booking_id(booking_id: str) -> ConversationState | None:
    """Look up a conversation by associated Cal.com booking ID."""
    if not booking_id:
        return None
    for conversation in get_store().all():
        if conversation.calcom_booking_id == booking_id:
            return conversation
    return None


def add_message(
    thread_id: str,
    role: str,
    content: str,
    channel: ChannelType | None = None,
    metadata: dict | None = None,
) -> ConversationState:
    """Add a message to a conversation thread."""
    state = get_store().get(thread_id)
    if not state:
        raise ValueError(f"Conversation {thread_id} not found")

    msg = ConversationMessage(
        role=role,
        channel=channel or state.channel,
        content=content,
        metadata=metadata or {},
    )
    state.messages.append(msg)
    state.updated_at = datetime.utcnow().isoformat()

    # Update status based on message
    if role == "prospect":
        if _is_opt_out(content):
            state.status = ConversationStatus.OPTED_OUT
        elif state.status == ConversationStatus.OUTBOUND_SENT:
            state.status = ConversationStatus.REPLIED

    save_conversation(state)

    logger.debug("Added message to %s: role=%s, status=%s", thread_id, role, state.status.value)
    return state


def update_status(thread_id: str, status: ConversationStatus) -> ConversationState:
    """Update the conversation status."""
    state = get_store().get(thread_id)
    if not state:
        raise ValueError(f"Conversation {thread_id} not found")

    state.status = status
    state.updated_at = datetime.utcnow().isoformat()
    save_conversation(state)
    return state


def get_thread_history(thread_id: str) -> list[dict]:
    """Get the message history for a specific thread (NOT the company)."""
    state = get_store().get(thread_id)
    if not state:
        return []

    return [
        {"role": m.role, "content": m.content, "timestamp": m.timestamp, "channel": m.channel.value}
        for m in state.messages
    ]


def has_sibling_threads(thread_id: str) -> bool:
    """
    Check if this thread has sibling threads at the same company.
    Used for multi-thread leakage prevention.
    """
    state = get_store().get(thread_id)
    if not state:
        return False

    company = (state.prospect.company or "").lower()
    siblings = [c for c in get_store().all() if (c.prospect.company or "").lower() == company]
    return len(siblings) > 1


def get_active_conversations(
    status: ConversationStatus | None = None,
    channel: ChannelType | None = None,
) -> list[ConversationState]:
    """Get all active conversations, optionally filtered."""
    results = get_store().all()

    if status:
        results = [c for c in results if c.status == status]
    if channel:
        results = [c for c in results if c.channel == channel]

    return results


def get_stalled_conversations(stall_hours: int = 48) -> list[ConversationState]:
    """Find conversations that have stalled (no activity for N hours)."""
    cutoff = datetime.utcnow().timestamp() - (stall_hours * 3600)
    stalled = []

    for state in get_store().all():
        if state.status in (ConversationStatus.OUTBOUND_SENT, ConversationStatus.REPLIED):
            try:
                updated = datetime.fromisoformat(state.updated_at).timestamp()
                if updated < cutoff:
                    stalled.append(state)
            except ValueError:
                continue

    return stalled


def _is_opt_out(content: str) -> bool:
    """Check if a message is an opt-out request."""
    opt_out_keywords = {"stop", "unsubscribe", "opt out", "remove me", "do not contact"}
    content_lower = content.lower().strip()
    return any(kw in content_lower for kw in opt_out_keywords)
