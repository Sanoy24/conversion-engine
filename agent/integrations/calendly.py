"""
Calendly client: books discovery calls through the Scheduling API.

Auth: personal access token (or OAuth token) as a bearer token. Booking via
API (`POST /invitees`) requires a paid Calendly plan.

Settings:
    CALENDLY_API_TOKEN       bearer token
    CALENDLY_EVENT_TYPE_URI  https://api.calendly.com/event_types/<uuid>
    CALENDLY_SCHEDULING_URL  public link for self-serve booking, e.g.
                             https://calendly.com/acme/discovery
    CALENDLY_LOCATION_KIND   the event type's location kind (zoom_conference,
                             google_conference, ...); leave empty when the
                             event type has no location
    CALENDLY_LOCATION        value for kinds that need one (e.g. a phone number)

The requested slot must be open on the host's calendar; otherwise Calendly
rejects the booking and the caller can try the next slot.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx

from agent.config import settings
from agent.integrations.crm import split_name
from agent.models import ProspectInfo, TraceRecord

logger = logging.getLogger(__name__)

CALENDLY_API_BASE = "https://api.calendly.com"


def to_calendly_time(iso_time: str) -> str:
    """Any ISO-8601 time -> UTC with a trailing Z, as Calendly requires."""
    parsed = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendlyClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def get_booking_link(self) -> str:
        return settings.calendly_scheduling_url

    async def create_booking(
        self,
        prospect: ProspectInfo,
        start_time: str,
        end_time: str | None = None,
        notes: str | None = None,
        thread_id: str | None = None,
        sdr_email: str | None = None,
    ) -> tuple[dict, TraceRecord]:
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"
        first, last = split_name(prospect)
        body: dict = {
            "event_type": settings.calendly_event_type_uri,
            "start_time": to_calendly_time(start_time),
            "invitee": {
                "name": prospect.contact_name or prospect.company,
                "first_name": first or prospect.company,
                "last_name": last,
                "email": prospect.contact_email,
                "timezone": prospect.timezone or "UTC",
            },
            # Carries the thread id so the booking can be traced back to the conversation.
            "tracking": {"utm_source": "conversion_engine", "utm_content": thread_id or ""},
        }
        if sdr_email:
            body["event_guests"] = [sdr_email]
        if settings.calendly_location_kind:
            body["location"] = {"kind": settings.calendly_location_kind}
            if settings.calendly_location:
                body["location"]["location"] = settings.calendly_location

        try:
            if not (settings.calendly_api_token and settings.calendly_event_type_uri):
                raise RuntimeError("CALENDLY_API_TOKEN and CALENDLY_EVENT_TYPE_URI must be set")
            if not prospect.contact_email:
                raise ValueError("Calendly bookings need the prospect's email")
            async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as client:
                response = await client.post(
                    f"{CALENDLY_API_BASE}/invitees",
                    json=body,
                    headers={"Authorization": f"Bearer {settings.calendly_api_token}"},
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Calendly returned {response.status_code}: {response.text[:300]}"
                )
            resource = response.json().get("resource", {})
        except Exception as e:
            logger.error("Calendly booking failed for %s: %s", prospect.company, e)
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="calendly_booking_created",
                prospect_company=prospect.company,
                input_data=body,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace

        result = {
            "id": resource.get("uri"),
            "event": resource.get("event"),
            "status": resource.get("status"),
            "cancel_url": resource.get("cancel_url"),
            "reschedule_url": resource.get("reschedule_url"),
        }
        trace = TraceRecord(
            trace_id=trace_id,
            event_type="calendly_booking_created",
            prospect_company=prospect.company,
            input_data={"event_type": body["event_type"], "start_time": body["start_time"]},
            output_data={"booking_id": result["id"], "event": result["event"]},
            cost_usd=0.0,
        )
        logger.info("Calendly booking created for %s at %s", prospect.company, body["start_time"])
        return result, trace
