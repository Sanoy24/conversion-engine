"""
Calendar abstraction for booking discovery calls.

Pick the provider with `CALENDAR_PROVIDER`:

    calcom    Cal.com (cloud or self-hosted)
    calendly  Calendly Scheduling API (needs a paid Calendly plan)

Every client exposes:

    create_booking(prospect, start_time, end_time=None, notes=None,
                   thread_id=None, sdr_email=None) -> (dict, TraceRecord)
        On success the dict carries the booking "id" (Cal.com wraps it in
        "data"); on failure it has "error". `start_time` is ISO-8601.
    get_booking_link() -> str
        Public self-serve booking URL, used in emails and SMS.
"""

from __future__ import annotations

import logging
from typing import Protocol

from agent.config import settings
from agent.models import ProspectInfo, TraceRecord

logger = logging.getLogger(__name__)

CALENDAR_PROVIDERS = ("calcom", "calendly")


class CalendarClient(Protocol):
    async def create_booking(
        self,
        prospect: ProspectInfo,
        start_time: str,
        end_time: str | None = None,
        notes: str | None = None,
        thread_id: str | None = None,
        sdr_email: str | None = None,
    ) -> tuple[dict, TraceRecord]: ...

    def get_booking_link(self) -> str: ...


_client: CalendarClient | None = None


def get_calendar_client() -> CalendarClient:
    """The calendar client for `CALENDAR_PROVIDER`, created once per process."""
    global _client
    if _client is not None:
        return _client

    provider = settings.calendar_provider
    if provider == "calcom":
        from agent.integrations.calcom import get_calcom_client

        _client = get_calcom_client()
    elif provider == "calendly":
        from agent.integrations.calendly import CalendlyClient

        _client = CalendlyClient()
    else:
        raise ValueError(
            f"Unknown CALENDAR_PROVIDER {provider!r}; expected one of {CALENDAR_PROVIDERS}"
        )
    logger.info("Calendar provider: %s", provider)
    return _client


def reset_calendar_client() -> None:
    """Forget the current client (the next call builds a fresh one)."""
    global _client
    _client = None
