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
    """Webhook endpoint for Resend email replies."""
    try:
        payload = await request.json()
        parsed = process_reply_webhook(payload)

        if parsed.get("thread_id"):
            result = await handle_prospect_reply(
                thread_id=parsed["thread_id"],
                reply_content=parsed.get("body", ""),
            )
            return JSONResponse(content=result)

        return {"status": "received", "warning": "No thread_id found in reply"}

    except Exception as e:
        logger.error("Email webhook error: %s", str(e))
        return {"status": "error", "error": str(e)}


@app.post("/webhooks/sms/inbound")
async def sms_inbound_webhook(request: Request):
    """Webhook endpoint for Africa's Talking inbound SMS."""
    try:
        # Africa's Talking sends form-encoded data
        form = await request.form()
        payload = dict(form)
        parsed = process_inbound_sms(payload)

        if parsed["is_opt_out"]:
            return {"status": "opt_out_processed"}
        if parsed["is_help"]:
            return {"status": "help_requested"}

        return {"status": "received", "parsed": parsed}

    except Exception as e:
        logger.error("SMS webhook error: %s", str(e))
        return {"status": "error", "error": str(e)}


# ── Entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
    )
