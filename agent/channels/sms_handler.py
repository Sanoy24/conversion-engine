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


async def send_sms(
    to_phone: str,
    message: str,
    thread_id: str | None = None,
) -> tuple[dict, TraceRecord]:
    """
    Send an SMS via Africa's Talking sandbox.

    Kill switch: when LIVE_OUTBOUND_ENABLED is False, logs instead of sending.
    NEVER use for cold outbound — SMS is only for warm leads.
    """
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
    Process inbound SMS webhook from Africa's Talking.
    Handles STOP, HELP, UNSUB commands.
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
    }

    if parsed["is_opt_out"]:
        logger.info("SMS opt-out received from %s: %s", from_phone, message)
    if parsed["is_help"]:
        logger.info("SMS HELP request from %s", from_phone)

    return parsed


def _is_opt_out_sms(message: str) -> bool:
    """Check for TCPA-compliant opt-out keywords."""
    opt_out_words = {"stop", "unsubscribe", "unsub", "quit", "cancel", "end", "optout", "opt out"}
    return message.strip().lower() in opt_out_words
