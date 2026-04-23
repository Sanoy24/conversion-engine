"""
Resend email handler.
Primary outbound channel for Tenacious prospects.
Handles: email sending, reply webhook processing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import resend

from agent.config import settings
from agent.models import EmailDraft, TraceRecord

logger = logging.getLogger(__name__)


def init_resend():
    """Initialize the Resend client."""
    resend.api_key = settings.resend_api_key


async def send_email(
    to_email: str,
    draft: EmailDraft,
    reply_to: str | None = None,
) -> tuple[dict, TraceRecord]:
    """
    Send an email via Resend.

    Kill switch: when LIVE_OUTBOUND_ENABLED is False, logs the email
    instead of sending. All outbound routes to the staff sink.
    """
    trace_id = f"tr_{uuid.uuid4().hex[:8]}"

    email_data = {
        "from": settings.resend_from_email,
        "to": [to_email],
        "subject": draft.subject,
        "text": draft.body,
        "reply_to": reply_to or settings.resend_from_email,
        "headers": {
            "X-Thread-ID": draft.thread_id,
            "X-Draft-Status": "draft" if draft.draft_metadata.get("marked_draft") else "final",
        },
    }

    if not settings.live_outbound_enabled:
        # Kill switch active — log but don't send
        logger.info(
            "[SINK] Email NOT sent (kill switch active). To: %s, Subject: %s",
            to_email,
            draft.subject,
        )
        trace = TraceRecord(
            trace_id=trace_id,
            event_type="email_sent_sink",
            prospect_company=draft.draft_metadata.get("prospect_company"),
            thread_id=draft.thread_id,
            input_data=email_data,
            output_data={"status": "routed_to_sink", "reason": "kill_switch_active"},
            cost_usd=0.0,
            latency_ms=0,
            success=True,
        )
        return {"status": "sink", "id": trace_id}, trace

    try:
        init_resend()
        response = resend.Emails.send(email_data)

        trace = TraceRecord(
            trace_id=trace_id,
            event_type="email_sent",
            prospect_company=draft.draft_metadata.get("prospect_company"),
            thread_id=draft.thread_id,
            input_data={"to": to_email, "subject": draft.subject},
            output_data={"resend_id": response.get("id"), "status": "sent"},
            cost_usd=0.0,  # Resend free tier
            latency_ms=0,
            success=True,
        )

        logger.info("Email sent to %s (Resend ID: %s)", to_email, response.get("id"))
        return response, trace

    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, str(e))
        trace = TraceRecord(
            trace_id=trace_id,
            event_type="email_sent",
            thread_id=draft.thread_id,
            input_data={"to": to_email, "subject": draft.subject},
            output_data={"error": str(e)},
            success=False,
            error=str(e),
        )
        return {"status": "error", "error": str(e)}, trace


# Resend webhook event types we distinguish. The full list includes
# email.sent, email.delivered, email.delivery_delayed, email.bounced,
# email.complained, email.opened, email.clicked. We act on "received"
# (inbound reply) and flag bounces/complaints so downstream logic can
# suppress future sends; everything else is acknowledged and ignored.
_REPLY_EVENTS = {"email.received", "inbound"}
_BOUNCE_EVENTS = {"email.bounced", "email.complained"}
_DELIVERY_EVENTS = {"email.delivered", "email.sent", "email.delivery_delayed"}


def process_reply_webhook(payload: dict) -> dict:
    """
    Process an inbound webhook POST from Resend.

    Returns a structured record with:
      - event_type: normalized event classification
      - is_reply: True only for inbound-reply events
      - is_bounce / is_complaint: suppression signals for future sends
      - parse_error: set if the payload was malformed

    Downstream logic should check `is_reply` before treating the payload as
    a prospect reply. Bounces and delivery pings are captured for observability
    but must NOT be routed to the conversation handler as replies.
    """
    # 1. Validate payload shape up front — never silently drop malformed data.
    if not isinstance(payload, dict):
        logger.warning(
            "Resend webhook: non-dict payload (%s); ignoring", type(payload).__name__
        )
        return {
            "event_type": "malformed",
            "is_reply": False,
            "is_bounce": False,
            "parse_error": "payload_not_dict",
            "received_at": datetime.utcnow().isoformat(),
        }

    # Resend sends either `type` (v1) or `event` (newer) — try both.
    raw_event = (payload.get("type") or payload.get("event") or "").strip().lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    is_reply = raw_event in _REPLY_EVENTS or (
        # Heuristic: if no event type is set but there's a text/html body with a
        # `from` header, treat it as an inbound reply (legacy webhook shape).
        not raw_event and bool(data.get("from") or data.get("sender"))
        and bool(data.get("text") or data.get("html"))
    )
    is_bounce = raw_event in _BOUNCE_EVENTS

    # Headers can be nested in the data object or at the payload root.
    headers = data.get("headers") or payload.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    record = {
        "event_type": raw_event or ("reply_heuristic" if is_reply else "unknown"),
        "is_reply": is_reply,
        "is_bounce": is_bounce,
        "is_delivery": raw_event in _DELIVERY_EVENTS,
        "from_email": data.get("from") or data.get("sender"),
        "subject": data.get("subject", ""),
        "body": data.get("text") or data.get("html", ""),
        "thread_id": headers.get("X-Thread-ID"),
        "resend_id": data.get("id") or data.get("email_id"),
        "received_at": datetime.utcnow().isoformat(),
        "raw_payload": payload,
    }

    if is_bounce:
        logger.warning(
            "Resend webhook: bounce/complaint for %s (resend_id=%s)",
            record["from_email"], record["resend_id"],
        )
    elif is_reply:
        logger.info(
            "Resend webhook: reply from %s (thread=%s)",
            record["from_email"], record["thread_id"],
        )
    elif record["is_delivery"]:
        logger.debug("Resend webhook: delivery event %s", raw_event)
    else:
        logger.info("Resend webhook: unhandled event_type=%r", raw_event)

    return record
