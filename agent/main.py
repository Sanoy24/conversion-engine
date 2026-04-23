"""
Conversion Engine — FastAPI Application.
Webhook endpoints for email replies, SMS inbound, and API routes
for prospect processing and system health.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.channels.email_handler import process_reply_webhook
from agent.channels.sms_handler import process_inbound_sms
from agent.config import settings
from agent.core.conversation import get_active_conversations, get_stalled_conversations
from agent.core.orchestrator import handle_prospect_reply, process_new_prospect
from agent.models import ChannelType, ConversationStatus
from agent.observability.trace_logger import compute_metrics, init_trace_logger

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application startup and shutdown."""
    init_trace_logger()
    logger.info(
        "Conversion Engine starting (env=%s, model=%s)", settings.app_env, settings.active_model
    )
    logger.info("Kill switch: live_outbound=%s", settings.live_outbound_enabled)
    yield
    logger.info("Conversion Engine shutting down.")


app = FastAPI(
    title="Tenacious Conversion Engine",
    description="Automated lead generation and conversion system for Tenacious Consulting and Outsourcing",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Request Models ─────────────────────────────────────────────────────


class NewProspectRequest(BaseModel):
    company_name: str | None = None
    domain: str | None = None
    crunchbase_id: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_title: str | None = None


class ReplyRequest(BaseModel):
    thread_id: str
    reply_content: str
    channel: str = "email"


# ── API Routes ─────────────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """System health check."""
    return {
        "status": "ok",
        "env": settings.app_env,
        "model": settings.active_model,
        "live_outbound": settings.live_outbound_enabled,
    }


@app.post("/api/prospect/new")
async def new_prospect(request: NewProspectRequest):
    """
    Process a new prospect through the full pipeline:
    enrich → classify → draft email → create conversation.
    """
    if not request.company_name and not request.domain and not request.crunchbase_id:
        raise HTTPException(
            status_code=400,
            detail="At least one of company_name, domain, or crunchbase_id is required.",
        )

    try:
        result = await process_new_prospect(
            company_name=request.company_name,
            domain=request.domain,
            crunchbase_id=request.crunchbase_id,
            contact_name=request.contact_name,
            contact_email=request.contact_email,
            contact_title=request.contact_title,
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error("New prospect pipeline failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/prospect/reply")
async def prospect_reply(request: ReplyRequest):
    """Handle a reply from a prospect."""
    try:
        channel = ChannelType(request.channel)
        result = await handle_prospect_reply(
            thread_id=request.thread_id,
            reply_content=request.reply_content,
            channel=channel,
        )
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error("Reply handling failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/conversations")
async def list_conversations(status: str | None = None):
    """List active conversations."""
    filter_status = ConversationStatus(status) if status else None
    conversations = get_active_conversations(status=filter_status)
    return [
        {
            "thread_id": c.thread_id,
            "company": c.prospect.company,
            "status": c.status.value,
            "channel": c.channel.value,
            "messages_count": len(c.messages),
            "updated_at": c.updated_at,
        }
        for c in conversations
    ]


@app.get("/api/conversations/stalled")
async def stalled_conversations(hours: int = 48):
    """Find stalled conversations."""
    stalled = get_stalled_conversations(stall_hours=hours)
    return [
        {
            "thread_id": c.thread_id,
            "company": c.prospect.company,
            "status": c.status.value,
            "updated_at": c.updated_at,
        }
        for c in stalled
    ]


@app.get("/api/metrics")
async def system_metrics():
    """Get system-wide metrics from the trace log."""
    return compute_metrics()


# ── Webhook Endpoints ──────────────────────────────────────────────────


@app.post("/webhooks/email/reply")
async def email_reply_webhook(request: Request):
    """
    Webhook endpoint for Resend email events.

    Resend fans out multiple event types (delivered, bounced, complained, etc.)
    to the same URL. We route only actual inbound replies to the conversation
    handler; bounces and delivery pings are acknowledged and logged but NOT
    treated as replies (which would create spurious conversation turns).
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Email webhook: malformed JSON body: %s", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "malformed_json"},
        )

    parsed = process_reply_webhook(payload)

    # Suppression signal — bounced/complained addresses should not get
    # further sends. Surfaced to the caller so observability layers can see
    # the event; a real deployment would mark the prospect as undeliverable.
    if parsed.get("is_bounce"):
        return {
            "status": "received",
            "event_type": parsed["event_type"],
            "action": "suppressed",
            "from_email": parsed.get("from_email"),
        }

    if parsed.get("event_type") == "malformed":
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": parsed.get("parse_error")},
        )

    if not parsed.get("is_reply"):
        # Delivery pings, opens, clicks — acknowledge but don't treat as a reply.
        return {"status": "received", "event_type": parsed["event_type"], "action": "ignored"}

    if not parsed.get("thread_id"):
        logger.warning("Email reply without X-Thread-ID header from %s", parsed.get("from_email"))
        return {"status": "received", "warning": "No thread_id found in reply"}

    try:
        result = await handle_prospect_reply(
            thread_id=parsed["thread_id"],
            reply_content=parsed.get("body", ""),
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error("Email reply handling failed for thread %s: %s",
                     parsed.get("thread_id"), e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(e)},
        )


@app.post("/webhooks/sms/inbound")
async def sms_inbound_webhook(request: Request):
    """
    Webhook endpoint for Africa's Talking inbound SMS.

    Routes the reply to the orchestrator's `handle_prospect_reply` (same code
    path as email replies) so the SMS channel doesn't dead-end. STOP / HELP
    commands are handled before routing so they never turn into conversation
    turns.
    """
    try:
        # Africa's Talking sends form-encoded data; malformed bodies become {}
        try:
            form = await request.form()
            payload = dict(form)
        except Exception as e:
            logger.warning("SMS webhook: malformed form body: %s", e)
            return JSONResponse(
                status_code=400,
                content={"status": "error", "reason": "malformed_body"},
            )

        parsed = process_inbound_sms(payload)

        if parsed["is_opt_out"]:
            return {"status": "opt_out_processed", "from": parsed.get("from_phone")}
        if parsed["is_help"]:
            return {"status": "help_requested", "from": parsed.get("from_phone")}

        # Look up the conversation by phone number and route to the same
        # reply-handling path that email uses.
        from agent.core.conversation import get_conversation_by_phone
        from_phone = parsed.get("from_phone", "")
        conversation = get_conversation_by_phone(from_phone) if from_phone else None

        if not conversation:
            logger.info(
                "SMS inbound from %s has no active conversation — acknowledging only",
                from_phone,
            )
            return {"status": "received", "action": "no_matching_thread", "parsed": parsed}

        result = await handle_prospect_reply(
            thread_id=conversation.thread_id,
            reply_content=parsed.get("message", ""),
            channel=ChannelType.SMS,
        )
        return JSONResponse(content=result)

    except Exception as e:
        logger.error("SMS webhook error: %s", str(e), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(e)},
        )


@app.post("/webhooks/calcom")
async def calcom_webhook(request: Request):
    """
    Webhook endpoint for Cal.com booking events.

    Cal.com sends POST requests for:
      - BOOKING_CREATED    — new discovery-call booking confirmed
      - BOOKING_CANCELLED  — prospect cancelled
      - BOOKING_RESCHEDULED — prospect moved the call

    Configure in Cal.com → Settings → Webhooks → Add webhook:
      URL: https://<your-render-service>.onrender.com/webhooks/calcom
      Events: BOOKING_CREATED, BOOKING_CANCELLED, BOOKING_RESCHEDULED
    """
    try:
        payload = await request.json()
    except Exception:
        # Cal.com may send empty pings to verify the URL
        return {"status": "ok", "note": "empty_or_invalid_body"}

    trigger = payload.get("triggerEvent", "UNKNOWN")
    booking = payload.get("payload", {})

    booking_uid = booking.get("uid") or booking.get("id") or "unknown"
    attendees = booking.get("attendees", [])
    attendee_email = attendees[0].get("email", "") if attendees else ""

    logger.info(
        "Cal.com webhook: trigger=%s booking_uid=%s attendee=%s",
        trigger, booking_uid, attendee_email,
    )

    # If the booking carries metadata we can correlate back to a thread, do so.
    metadata = booking.get("metadata") or {}
    thread_id = metadata.get("thread_id")

    if trigger == "BOOKING_CREATED":
        start = booking.get("startTime", "")
        logger.info("Discovery call booked: uid=%s start=%s thread=%s", booking_uid, start, thread_id)
        return {
            "status": "ok",
            "event": "booking_created",
            "booking_uid": booking_uid,
            "start": start,
            "thread_id": thread_id,
        }

    if trigger == "BOOKING_CANCELLED":
        reason = booking.get("cancellationReason", "")
        logger.info("Discovery call cancelled: uid=%s reason=%s thread=%s", booking_uid, reason, thread_id)
        return {
            "status": "ok",
            "event": "booking_cancelled",
            "booking_uid": booking_uid,
            "reason": reason,
            "thread_id": thread_id,
        }

    if trigger == "BOOKING_RESCHEDULED":
        new_start = booking.get("startTime", "")
        logger.info("Discovery call rescheduled: uid=%s new_start=%s thread=%s", booking_uid, new_start, thread_id)
        return {
            "status": "ok",
            "event": "booking_rescheduled",
            "booking_uid": booking_uid,
            "new_start": new_start,
            "thread_id": thread_id,
        }

    # Unknown trigger — acknowledge to prevent Cal.com retries
    logger.warning("Cal.com webhook: unhandled trigger=%s", trigger)
    return {"status": "ok", "event": "unhandled", "trigger": trigger}


# ── Entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
    )
