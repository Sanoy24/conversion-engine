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


def process_reply_webhook(payload: dict) -> dict:
    """
    Process an inbound reply webhook from Resend.
    Extracts: sender, subject, body, thread_id.
    """
    return {
        "from_email": payload.get("from") or payload.get("sender"),
        "subject": payload.get("subject", ""),
        "body": payload.get("text") or payload.get("html", ""),
        "thread_id": (payload.get("headers") or {}).get("X-Thread-ID"),
        "received_at": datetime.utcnow().isoformat(),
        "raw_payload": payload,
    }
