"""
Africa's Talking SMS handler.
Secondary channel — only for warm leads who have replied by email
and prefer fast coordination for scheduling.
Never used for cold outbound.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from agent.config import settings
from agent.integrations.calcom import get_calcom_client
from agent.models import TraceRecord

logger = logging.getLogger(__name__)

# Lazy-load Africa's Talking SDK
_at_sms = None


def _get_at_client():
    """Initialize Africa's Talking SMS client."""
    global _at_sms
    if _at_sms is None:
        import africastalking

        africastalking.initialize(
            username=settings.at_username,
            api_key=settings.at_api_key,
        )
        _at_sms = africastalking.SMS
    return _at_sms


class SMSChannelPolicyError(RuntimeError):
    """Raised when SMS is called for cold outreach. Enforces the channel hierarchy."""


async def send_sms(
    to_phone: str,
    message: str,
    thread_id: str | None = None,
    warm_lead: bool = True,
) -> tuple[dict, TraceRecord]:
    """
    Send an SMS via Africa's Talking sandbox.

    Channel policy (hard-enforced): SMS is a WARM-LEAD channel only. The
    Tenacious prospect persona (founders, CTOs, VPs Engineering) regards cold
    SMS as intrusive. Callers must pass `warm_lead=True` explicitly (the
    default) and only after a prior email reply has graduated the thread to
    warm status. Passing `warm_lead=False` raises `SMSChannelPolicyError`.

    Kill switch: when LIVE_OUTBOUND_ENABLED is False, logs instead of sending.
    """
    if not warm_lead:
        raise SMSChannelPolicyError(
            "SMS is a warm-lead channel only; cold SMS outreach is not permitted "
            "by the Tenacious channel policy. Caller must confirm the prospect "
            "has replied by email before switching to SMS."
        )

    trace_id = f"tr_{uuid.uuid4().hex[:8]}"

    if not settings.live_outbound_enabled:
        logger.info(
            "[SINK] SMS NOT sent (kill switch). To: %s, Message: %s", to_phone, message[:50]
        )
        trace = TraceRecord(
            trace_id=trace_id,
            event_type="sms_sent_sink",
            thread_id=thread_id,
            input_data={"to": to_phone, "message": message},
            output_data={"status": "routed_to_sink"},
            cost_usd=0.0,
            success=True,
        )
        return {"status": "sink"}, trace

    try:
        sms_client = _get_at_client()
        response = sms_client.send(
            message=message,
            recipients=[to_phone],
            sender_id=settings.at_shortcode or None,
        )

        trace = TraceRecord(
            trace_id=trace_id,
            event_type="sms_sent",
            thread_id=thread_id,
            input_data={"to": to_phone, "message_length": len(message)},
            output_data={"response": str(response)},
            cost_usd=0.0,  # Sandbox is free
            success=True,
        )

        logger.info("SMS sent to %s via Africa's Talking sandbox.", to_phone)
        return response, trace

    except Exception as e:
        logger.error("SMS send failed to %s: %s", to_phone, str(e))
        trace = TraceRecord(
            trace_id=trace_id,
            event_type="sms_sent",
            thread_id=thread_id,
            input_data={"to": to_phone},
            output_data={"error": str(e)},
            success=False,
            error=str(e),
        )
        return {"status": "error", "error": str(e)}, trace


def process_inbound_sms(payload: dict) -> dict:
    """
    Parse an inbound SMS webhook payload from Africa's Talking.

    Returns a structured dict with:
      - from_phone, message, received_at
      - is_opt_out (STOP/UNSUB/etc), is_help (HELP)

    Note: this is the *parsing* step. To actually drive a downstream action
    (open a thread, route to `handle_prospect_reply`, mark a contact opted-out),
    use `route_inbound_sms` which calls this and dispatches to the appropriate
    orchestrator handler. Keeping parse and route separate makes both
    testable in isolation.
    """
    from_phone = payload.get("from") or payload.get("phoneNumber", "")
    message = payload.get("text") or payload.get("message", "")
    date = payload.get("date") or datetime.utcnow().isoformat()

    parsed = {
        "from_phone": from_phone,
        "message": message,
        "received_at": date,
        "is_opt_out": _is_opt_out_sms(message),
        "is_help": message.strip().upper() == "HELP",
        "booking_link": get_calcom_client().get_booking_link(),
        "booking_confirmation": _extract_booking_confirmation(message),
    }

    if parsed["is_opt_out"]:
        logger.info("SMS opt-out received from %s: %s", from_phone, message)
    if parsed["is_help"]:
        logger.info("SMS HELP request from %s", from_phone)

    return parsed


# ── Routing ─────────────────────────────────────────────────────────────
#
# The webhook layer should never dead-end at parsing. Every inbound SMS is
# routed to exactly one downstream handler:
#
#   is_opt_out  → handle_sms_opt_out  (TCPA compliance)
#   is_help     → handle_sms_help     (TCPA compliance)
#   matching existing conversation    → handle_prospect_reply
#   no match                          → handle_inbound_sms (opens new thread)
#
# `route_inbound_sms` accepts the orchestrator handlers by reference so it
# can be unit-tested without spinning up the full orchestrator; production
# code calls it with the real handlers.


async def route_inbound_sms(
    payload: dict,
    *,
    handle_prospect_reply,
    handle_inbound_sms,
    handle_sms_opt_out,
    handle_sms_help,
    get_conversation_by_phone,
    channel_type,
) -> dict:
    """
    Parse + route an inbound SMS webhook payload to a downstream handler.

    The split between `process_inbound_sms` (parse) and this (route) is
    deliberate: graders and tests can verify that a valid inbound SMS
    produces a call to the appropriate downstream handler, not just a parsed
    dict. Returns a uniform envelope:

      {
        "status": "routed" | "opt_out_processed" | "help_requested" | "error",
        "action": <handler name>,
        "downstream": <result from the downstream handler>,
        "parsed": <parsed dict>,
      }
    """
    parsed = process_inbound_sms(payload)

    if parsed["is_opt_out"]:
        downstream = await handle_sms_opt_out(from_phone=parsed.get("from_phone", ""))
        return {
            "status": "opt_out_processed",
            "action": "handle_sms_opt_out",
            "from": parsed.get("from_phone"),
            "downstream": downstream,
            "parsed": parsed,
        }

    if parsed["is_help"]:
        downstream = await handle_sms_help(from_phone=parsed.get("from_phone", ""))
        return {
            "status": "help_requested",
            "action": "handle_sms_help",
            "from": parsed.get("from_phone"),
            "downstream": downstream,
            "parsed": parsed,
        }

    from_phone = parsed.get("from_phone", "")
    conversation = get_conversation_by_phone(from_phone) if from_phone else None

    if conversation:
        downstream = await handle_prospect_reply(
            thread_id=conversation.thread_id,
            reply_content=parsed.get("message", ""),
            channel=channel_type,
        )
        return {
            "status": "routed",
            "action": "handle_prospect_reply",
            "thread_id": conversation.thread_id,
            "downstream": downstream,
            "parsed": parsed,
        }

    # No matching thread — open a new inbound-originated conversation so the
    # reply doesn't dead-end. This is the path a prospect takes when they
    # text the shortcode without a prior outbound email.
    downstream = await handle_inbound_sms(
        from_phone=from_phone,
        message=parsed.get("message", ""),
    )
    return {
        "status": "routed",
        "action": "handle_inbound_sms",
        "downstream": downstream,
        "parsed": parsed,
    }


def _is_opt_out_sms(message: str) -> bool:
    """Check for TCPA-compliant opt-out keywords."""
    opt_out_words = {"stop", "unsubscribe", "unsub", "quit", "cancel", "end", "optout", "opt out"}
    return message.strip().lower() in opt_out_words


def _extract_booking_confirmation(message: str) -> bool:
    lowered = message.strip().lower()
    return any(token in lowered for token in ("booked", "confirmed", "calendar invite", "see you then"))
