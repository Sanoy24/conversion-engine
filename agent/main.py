"""
Conversion Engine — FastAPI Application.
Webhook endpoints for email replies, SMS inbound, and API routes
for prospect processing and system health.
"""

from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from agent.channels.email_handler import process_reply_webhook
from agent.config import settings
from agent.core.conversation import get_active_conversations, get_stalled_conversations
from agent.core.orchestrator import (
    ReviewError,
    approve_review,
    handle_calcom_event,
    handle_prospect_reply,
    handle_unsubscribe,
    process_new_prospect,
    reject_review,
)
from agent.core.review import get_review_queue
from agent.core.suppression import MANUAL, get_suppression_list, verify_unsubscribe_token
from agent.models import ChannelType, ConversationStatus
from agent.observability.trace_logger import compute_metrics, init_trace_logger
from agent.security import (
    check_api_key,
    check_calcom_signature,
    check_resend_signature,
    check_sms_webhook_token,
    check_twilio_signature,
)

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
    if not settings.is_dev:
        unset = [
            name
            for name, value in (
                ("API_KEY", settings.api_key),
                ("RESEND_WEBHOOK_SECRET", settings.resend_webhook_secret),
                ("CALCOM_WEBHOOK_SECRET", settings.calcom_webhook_secret),
                ("AT_WEBHOOK_TOKEN", settings.at_webhook_token)
                if settings.sms_provider == "africastalking"
                else ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            )
            if not value
        ]
        if unset:
            logger.error("Unset in production, matching requests will be rejected: %s", unset)
    if settings.live_outbound_enabled and not (
        settings.public_base_url and settings.unsubscribe_secret
    ):
        logger.error(
            "Live outbound is on but PUBLIC_BASE_URL / UNSUBSCRIBE_SECRET are unset: "
            "every email will be refused until both are configured."
        )
    yield
    logger.info("Conversion Engine shutting down.")


app = FastAPI(
    title="Conversion Engine",
    description="Signal-grounded B2B prospect research, outreach, and meeting booking",
    version="0.1.0",
    lifespan=lifespan,
)


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse(status_code=401, content={"status": "error", "reason": reason})


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Every /api/* route needs the API key; health, webhooks and unsubscribe stay public."""
    if request.url.path.startswith("/api/") and not check_api_key(request.headers):
        return _unauthorized("invalid_or_missing_api_key")
    return await call_next(request)


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


class SuppressionRequest(BaseModel):
    address: str
    channel: str = "email"
    reason: str = MANUAL


class ApproveRequest(BaseModel):
    subject: str | None = None
    body: str | None = None


class RejectRequest(BaseModel):
    reason: str | None = None


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
    if not check_resend_signature(request.headers, await request.body()):
        return _unauthorized("invalid_signature")
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Email webhook: malformed JSON body: %s", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "malformed_json"},
        )

    parsed = process_reply_webhook(payload)

    # Bounced/complained recipients were added to the suppression list
    # while parsing; nothing further is sent to them.
    if parsed.get("is_bounce"):
        return {
            "status": "received",
            "event_type": parsed["event_type"],
            "action": "suppressed",
            "suppressed": parsed.get("recipients", []),
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
        logger.error(
            "Email reply handling failed for thread %s: %s",
            parsed.get("thread_id"),
            e,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(e)},
        )


@app.post("/webhooks/sms/inbound")
async def sms_inbound_webhook(request: Request):
    """
    Webhook endpoint for Africa's Talking inbound SMS.

    Delegates to `agent.channels.sms_handler.route_inbound_sms`, which is the
    single source of truth for SMS inbound routing. The router enforces that
    every inbound SMS drives exactly one downstream orchestrator handler
    (`handle_prospect_reply`, `handle_inbound_sms`, `handle_sms_opt_out`, or
    `handle_sms_help`) — nothing dead-ends at parsing.
    """
    if not check_sms_webhook_token(request.query_params.get("token", "")):
        return _unauthorized("invalid_token")
    try:
        form = await request.form()
        payload = dict(form)
    except Exception as e:
        logger.warning("SMS webhook: malformed form body: %s", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "malformed_body"},
        )
    return await _route_sms(payload)


@app.post("/webhooks/sms/twilio")
async def twilio_sms_webhook(request: Request):
    """
    Webhook endpoint for Twilio inbound SMS (Messaging -> "A message comes in").

    Verifies X-Twilio-Signature, maps Twilio's fields onto the shared router's
    payload, and answers with empty TwiML so Twilio sends no automatic reply.
    """
    try:
        params = {k: str(v) for k, v in (await request.form()).items()}
    except Exception as e:
        logger.warning("Twilio webhook: malformed form body: %s", e)
        return JSONResponse(
            status_code=400, content={"status": "error", "reason": "malformed_body"}
        )

    path_and_query = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not check_twilio_signature(request.headers, path_and_query, params):
        return _unauthorized("invalid_signature")

    from agent.channels.sms_providers import normalize_twilio_inbound

    result = await _route_sms(normalize_twilio_inbound(params))
    if isinstance(result, JSONResponse) and result.status_code >= 400:
        return result
    return Response(content="<Response/>", media_type="application/xml")


async def _route_sms(payload: dict):
    """Hand a normalized inbound SMS to the shared router (one handler per message)."""
    try:
        from agent.channels.sms_handler import route_inbound_sms
        from agent.core.conversation import get_conversation_by_phone
        from agent.core.orchestrator import (
            handle_inbound_sms,
            handle_sms_help,
            handle_sms_opt_out,
        )

        result = await route_inbound_sms(
            payload,
            handle_prospect_reply=handle_prospect_reply,
            handle_inbound_sms=handle_inbound_sms,
            handle_sms_opt_out=handle_sms_opt_out,
            handle_sms_help=handle_sms_help,
            get_conversation_by_phone=get_conversation_by_phone,
            channel_type=ChannelType.SMS,
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
      Secret: same value as CALCOM_WEBHOOK_SECRET
    """
    if not check_calcom_signature(request.headers, await request.body()):
        return _unauthorized("invalid_signature")
    try:
        payload = await request.json()
    except Exception:
        # Cal.com may send empty pings to verify the URL
        return {"status": "ok", "note": "empty_or_invalid_body"}

    trigger = payload.get("triggerEvent", "UNKNOWN")
    booking = payload.get("payload", {})
    return await handle_calcom_event(trigger=trigger, booking_payload=booking)


# ── Unsubscribe & suppression ──────────────────────────────────────────


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        status_code=status_code,
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
            "padding:0 1rem;line-height:1.5}button{font:inherit;padding:.5rem 1rem}</style>"
            f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
        ),
    )


@app.get("/unsubscribe")
async def unsubscribe_page(t: str = ""):
    """
    Landing page for the unsubscribe link in every email.

    Asks for a click instead of unsubscribing on GET, because mail scanners
    fetch links automatically. Mail clients' one-click unsubscribe uses the
    POST endpoint directly (RFC 8058).
    """
    email = verify_unsubscribe_token(t)
    if not email:
        return _page("Link not valid", "<p>This unsubscribe link is not valid.</p>", 400)
    return _page(
        "Unsubscribe",
        f"<p>Stop all emails to <strong>{html.escape(email)}</strong>?</p>"
        f"<form method='post' action='/unsubscribe?t={html.escape(t)}'>"
        "<button type='submit'>Unsubscribe</button></form>",
    )


@app.post("/unsubscribe")
async def unsubscribe(t: str = ""):
    """Unsubscribe: the confirm button and RFC 8058 one-click both land here."""
    email = verify_unsubscribe_token(t)
    if not email:
        return _page("Link not valid", "<p>This unsubscribe link is not valid.</p>", 400)
    await handle_unsubscribe(email)
    return _page(
        "You're unsubscribed",
        f"<p>We won't email <strong>{html.escape(email)}</strong> again.</p>",
    )


@app.get("/api/suppressions")
async def list_suppressions():
    """Every suppressed email address and phone number, oldest first."""
    entries = get_suppression_list().all()
    return {"count": len(entries), "suppressions": entries}


@app.post("/api/suppressions")
async def add_suppression(request: SuppressionRequest):
    """Suppress an address by hand (e.g. a do-not-contact request received elsewhere)."""
    if not request.address.strip():
        raise HTTPException(status_code=400, detail="address is required")
    added = get_suppression_list().add(
        request.address, channel=request.channel, reason=request.reason
    )
    return {"status": "suppressed", "newly_added": added}


# ── Review queue ───────────────────────────────────────────────────────

_REVIEW_PAGE = (Path(__file__).parent / "review_page.html").read_text(encoding="utf-8")


@app.get("/review")
async def review_page():
    """Browser page for the review queue. Its data calls go through /api/review (API key)."""
    return HTMLResponse(_REVIEW_PAGE)


@app.get("/api/review")
async def list_review_items(status: str | None = "pending"):
    """Drafts in the review queue, oldest first. `status=all` lists every item."""
    items = get_review_queue().list(None if status == "all" else status)
    return {"count": len(items), "items": [item.model_dump(exclude={"draft"}) for item in items]}


@app.get("/api/review/{review_id}")
async def get_review_item(review_id: str):
    item = get_review_queue().get(review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    return item.model_dump()


@app.post("/api/review/{review_id}/approve")
async def approve_review_item(review_id: str, request: ApproveRequest):
    """Approve a draft, optionally with an edited subject/body, and send it."""
    try:
        item = await approve_review(review_id, subject=request.subject, body=request.body)
    except ReviewError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return item.model_dump(exclude={"draft"})


@app.post("/api/review/{review_id}/reject")
async def reject_review_item(review_id: str, request: RejectRequest):
    try:
        item = reject_review(review_id, reason=request.reason)
    except ReviewError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return item.model_dump(exclude={"draft"})


# ── Entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
    )
