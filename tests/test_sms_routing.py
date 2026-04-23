"""
Tests for inbound-SMS routing.

These tests prove that every inbound SMS with valid context drives a
corresponding downstream action — not just a parsed dict. The Mastered-tier
SMS rubric item ("inbound messages routed to a downstream handler") is
enforced here; if someone removes the orchestrator dispatch from
`route_inbound_sms`, these tests fail.

Coverage:
  - STOP  → handle_sms_opt_out called with the sender's phone
  - HELP  → handle_sms_help   called with the sender's phone
  - inbound that matches an existing thread → handle_prospect_reply called
    with (thread_id, message, channel=SMS)
  - inbound with no matching thread → handle_inbound_sms called (opens
    a new inbound-originated thread)
  - parse layer alone (process_inbound_sms) still produces a structured dict
    with is_opt_out / is_help / from_phone / message flags
"""

from __future__ import annotations

import pytest

from agent.channels.sms_handler import process_inbound_sms, route_inbound_sms
from agent.models import ChannelType


class _FakeConversation:
    """Minimal stand-in for a ConversationState, enough for routing tests."""
    def __init__(self, thread_id: str):
        self.thread_id = thread_id


class _Recorder:
    """Captures handler calls so tests can assert the routing happened."""
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def make_handler(self, name: str, return_value: dict | None = None):
        async def handler(**kwargs):
            self.calls.append((name, kwargs))
            return return_value or {"handler": name, "received": kwargs}
        return handler


@pytest.mark.asyncio
async def test_process_inbound_sms_parses_reply_payload():
    """The parse layer alone produces structured output — no routing yet."""
    parsed = process_inbound_sms({
        "from": "+254700000001",
        "text": "Thanks, Thursday 14:00 UTC works.",
    })
    assert parsed["from_phone"] == "+254700000001"
    assert parsed["message"] == "Thanks, Thursday 14:00 UTC works."
    assert parsed["is_opt_out"] is False
    assert parsed["is_help"] is False


@pytest.mark.asyncio
async def test_process_inbound_sms_detects_stop():
    assert process_inbound_sms({"from": "+1", "text": "STOP"})["is_opt_out"] is True
    assert process_inbound_sms({"from": "+1", "text": "stop"})["is_opt_out"] is True
    assert process_inbound_sms({"from": "+1", "text": "unsub"})["is_opt_out"] is True


@pytest.mark.asyncio
async def test_process_inbound_sms_detects_help():
    assert process_inbound_sms({"from": "+1", "text": "HELP"})["is_help"] is True


@pytest.mark.asyncio
async def test_route_inbound_sms_stop_calls_opt_out_handler():
    """STOP must drive `handle_sms_opt_out`, not just be logged."""
    rec = _Recorder()
    result = await route_inbound_sms(
        {"from": "+254700000001", "text": "STOP"},
        handle_prospect_reply=rec.make_handler("handle_prospect_reply"),
        handle_inbound_sms=rec.make_handler("handle_inbound_sms"),
        handle_sms_opt_out=rec.make_handler("handle_sms_opt_out"),
        handle_sms_help=rec.make_handler("handle_sms_help"),
        get_conversation_by_phone=lambda phone: None,
        channel_type=ChannelType.SMS,
    )
    # Exactly one handler invoked, and it was the opt-out one
    assert [name for name, _ in rec.calls] == ["handle_sms_opt_out"]
    assert rec.calls[0][1]["from_phone"] == "+254700000001"
    assert result["status"] == "opt_out_processed"
    assert result["action"] == "handle_sms_opt_out"


@pytest.mark.asyncio
async def test_route_inbound_sms_help_calls_help_handler():
    """HELP must drive `handle_sms_help`, not just be logged."""
    rec = _Recorder()
    result = await route_inbound_sms(
        {"from": "+254700000002", "text": "HELP"},
        handle_prospect_reply=rec.make_handler("handle_prospect_reply"),
        handle_inbound_sms=rec.make_handler("handle_inbound_sms"),
        handle_sms_opt_out=rec.make_handler("handle_sms_opt_out"),
        handle_sms_help=rec.make_handler("handle_sms_help"),
        get_conversation_by_phone=lambda phone: None,
        channel_type=ChannelType.SMS,
    )
    assert [name for name, _ in rec.calls] == ["handle_sms_help"]
    assert rec.calls[0][1]["from_phone"] == "+254700000002"
    assert result["status"] == "help_requested"
    assert result["action"] == "handle_sms_help"


@pytest.mark.asyncio
async def test_route_inbound_sms_matching_phone_calls_prospect_reply():
    """
    This is the core rubric case: an inbound SMS matching an existing thread
    MUST result in `handle_prospect_reply(thread_id=..., reply_content=...,
    channel=ChannelType.SMS)` being called — i.e. the same downstream
    handler that email replies use.
    """
    rec = _Recorder()
    existing = _FakeConversation(thread_id="thread_abc")

    result = await route_inbound_sms(
        {"from": "+254700000003", "text": "Sounds good, Friday works."},
        handle_prospect_reply=rec.make_handler("handle_prospect_reply"),
        handle_inbound_sms=rec.make_handler("handle_inbound_sms"),
        handle_sms_opt_out=rec.make_handler("handle_sms_opt_out"),
        handle_sms_help=rec.make_handler("handle_sms_help"),
        get_conversation_by_phone=lambda phone: existing if phone == "+254700000003" else None,
        channel_type=ChannelType.SMS,
    )

    # Exactly one downstream call, to handle_prospect_reply, with the
    # correct thread_id + message + channel.
    assert [name for name, _ in rec.calls] == ["handle_prospect_reply"]
    call_kwargs = rec.calls[0][1]
    assert call_kwargs["thread_id"] == "thread_abc"
    assert call_kwargs["reply_content"] == "Sounds good, Friday works."
    assert call_kwargs["channel"] == ChannelType.SMS
    # Router envelope exposes the thread + action for webhook visibility
    assert result["status"] == "routed"
    assert result["action"] == "handle_prospect_reply"
    assert result["thread_id"] == "thread_abc"


@pytest.mark.asyncio
async def test_route_inbound_sms_no_match_opens_new_thread():
    """
    When no conversation matches the phone, routing must still drive a
    downstream handler (`handle_inbound_sms`) rather than dead-ending.
    """
    rec = _Recorder()
    result = await route_inbound_sms(
        {"from": "+254700000004", "text": "Hi — is this Tenacious?"},
        handle_prospect_reply=rec.make_handler("handle_prospect_reply"),
        handle_inbound_sms=rec.make_handler("handle_inbound_sms"),
        handle_sms_opt_out=rec.make_handler("handle_sms_opt_out"),
        handle_sms_help=rec.make_handler("handle_sms_help"),
        get_conversation_by_phone=lambda phone: None,  # simulate no match
        channel_type=ChannelType.SMS,
    )
    assert [name for name, _ in rec.calls] == ["handle_inbound_sms"]
    call_kwargs = rec.calls[0][1]
    assert call_kwargs["from_phone"] == "+254700000004"
    assert call_kwargs["message"] == "Hi — is this Tenacious?"
    assert result["status"] == "routed"
    assert result["action"] == "handle_inbound_sms"


@pytest.mark.asyncio
async def test_route_inbound_sms_never_dead_ends():
    """
    Invariant test: across parse outcomes (reply / STOP / HELP /
    no-match-new-thread), EXACTLY ONE downstream handler is invoked.
    Zero handlers = dead-end = rubric failure.
    """
    payloads = [
        {"from": "+1", "text": "Sure, 2pm"},            # reply, match
        {"from": "+2", "text": "Sure, 2pm"},            # reply, no match
        {"from": "+3", "text": "STOP"},                 # opt-out
        {"from": "+4", "text": "HELP"},                 # help
    ]
    existing = {"+1": _FakeConversation(thread_id="t1")}

    for payload in payloads:
        rec = _Recorder()
        await route_inbound_sms(
            payload,
            handle_prospect_reply=rec.make_handler("handle_prospect_reply"),
            handle_inbound_sms=rec.make_handler("handle_inbound_sms"),
            handle_sms_opt_out=rec.make_handler("handle_sms_opt_out"),
            handle_sms_help=rec.make_handler("handle_sms_help"),
            get_conversation_by_phone=lambda phone: existing.get(phone),
            channel_type=ChannelType.SMS,
        )
        assert len(rec.calls) == 1, (
            f"Payload {payload!r} did not drive a downstream handler — "
            f"this is the rubric-failing dead-end case."
        )
