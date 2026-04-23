"""
Cal.com booking flow integration.
Books discovery calls between prospects and Tenacious delivery leads.
Provides real calendar events both attendees can see.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from agent.config import settings
from agent.models import ProspectInfo, TraceRecord

logger = logging.getLogger(__name__)


class CalComClient:
    """Cal.com client for discovery call booking."""

    def __init__(self):
        self.base_url = settings.calcom_base_url.rstrip("/")
        self.api_key = settings.calcom_api_key
        self.event_type_id = settings.calcom_event_type_id

    async def create_booking(
        self,
        prospect: ProspectInfo,
        start_time: str,
        end_time: str | None = None,
        notes: str | None = None,
    ) -> tuple[dict, TraceRecord]:
        """
        Book a discovery call slot via Cal.com API.

        Args:
            prospect: Prospect info with contact details
            start_time: ISO-8601 start time
            end_time: ISO-8601 end time (defaults to start + 30 min)
            notes: Additional context for the delivery lead
        """
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"

        booking_data = {
            "eventTypeId": self.event_type_id,
            "start": start_time,
            "attendee": {
                "name": prospect.contact_name or prospect.company,
                "email": prospect.contact_email or "",
                "timeZone": prospect.timezone or "UTC",
                "language": "en",
            },
            "bookingFieldsResponses": {
                "notes": notes or f"Discovery call with {prospect.company}",
            },
            "metadata": {
                "company": prospect.company,
                "thread_source": "conversion_engine",
            },
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/v2/bookings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "cal-api-version": "2024-08-13",
                        "Content-Type": "application/json",
                    },
                    json=booking_data,
                    timeout=15.0,
                )
                if not response.is_success:
                    # "Slot already booked" is a routine retry condition when
                    # the demo sweeps candidate offsets — log it as DEBUG to
                    # avoid scaring the operator. Real failures (auth, 500s)
                    # still bubble up as ERROR below via raise_for_status.
                    body_text = response.text
                    if (response.status_code == 400
                            and "already has booking" in body_text):
                        logger.debug("Cal.com slot taken (retry next offset): %s",
                                     body_text[:200])
                    else:
                        logger.error("Cal.com error body: %s", body_text)
                response.raise_for_status()
                result = response.json()

            # v2 wraps response in {"status": "success", "data": {...}}
            data = result.get("data", result)
            booking_id = data.get("id") or data.get("uid") or result.get("id") or result.get("uid")

            trace = TraceRecord(
                trace_id=trace_id,
                event_type="calcom_booking_created",
                prospect_company=prospect.company,
                input_data={
                    "event_type_id": self.event_type_id,
                    "start_time": start_time,
                },
                output_data={
                    "booking_id": booking_id,
                    "status": "confirmed",
                },
                cost_usd=0.0,
                success=True,
            )

            logger.info(
                "Cal.com booking created for %s: ID=%s, time=%s",
                prospect.company,
                booking_id,
                start_time,
            )
            return result, trace

        except Exception as e:
            # "Slot already booked" during offset sweep = expected retry,
            # demote to WARNING. Caller's loop will try the next slot.
            msg = str(e)
            if "already has booking" in msg or "400 Bad Request" in msg:
                logger.warning(
                    "Cal.com slot unavailable for %s (retrying next offset)",
                    prospect.company,
                )
            else:
                logger.error("Cal.com booking failed for %s: %s", prospect.company, msg)
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="calcom_booking_created",
                prospect_company=prospect.company,
                input_data=booking_data,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace

    async def get_available_slots(
        self,
        date_from: str,
        date_to: str,
    ) -> list[dict]:
        """Get available time slots for the event type."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/v2/slots/available",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "cal-api-version": "2024-09-04",
                    },
                    params={
                        "eventTypeId": self.event_type_id,
                        "startTime": date_from,
                        "endTime": date_to,
                        "duration": 30,
                    },
                    timeout=10.0,
                )
                if not response.is_success:
                    logger.warning("Cal.com slots error (%s): %s", response.status_code, response.text[:300])
                    return []
                body = response.json()
                # v2 response: {"status":"success","data":{"slots":{"2026-04-25":[{...}]}}}
                data = body.get("data", body)
                return data.get("slots", {})
        except Exception as e:
            logger.error("Cal.com availability check failed: %s", str(e))
            return []

    def get_booking_link(self) -> str:
        """Get the public booking URL for the event type."""
        return f"{self.base_url}/book/{self.event_type_id}"


# Module-level singleton
_calcom: CalComClient | None = None


def get_calcom_client() -> CalComClient:
    global _calcom
    if _calcom is None:
        _calcom = CalComClient()
    return _calcom
