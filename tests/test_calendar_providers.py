"""Calendar providers: Calendly booking requests and provider selection."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.config import settings
from agent.integrations import calendar as calendar_module
from agent.integrations.calcom import CalComClient
from agent.integrations.calendar import get_calendar_client
from agent.integrations.calendly import CalendlyClient, to_calendly_time
from agent.models import ProspectInfo

EVENT_TYPE = "https://api.calendly.com/event_types/ET123"


@pytest.fixture
def calendly_settings(monkeypatch):
    monkeypatch.setattr(settings, "calendly_api_token", "cal-token")
    monkeypatch.setattr(settings, "calendly_event_type_uri", EVENT_TYPE)
    monkeypatch.setattr(settings, "calendly_scheduling_url", "https://calendly.com/acme/discovery")


@pytest.fixture
def prospect() -> ProspectInfo:
    return ProspectInfo(
        company="Acme Corp",
        contact_name="Jane Doe",
        contact_email="jane@acme.io",
        timezone="America/New_York",
    )


def _server(responses: list[httpx.Response]):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    return httpx.MockTransport(handler), requests


INVITEE = {
    "resource": {
        "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
        "event": "https://api.calendly.com/scheduled_events/EV1",
        "status": "active",
        "cancel_url": "https://calendly.com/cancellations/INV1",
        "reschedule_url": "https://calendly.com/reschedulings/INV1",
    }
}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("2026-11-03T15:00:00+00:00", "2026-11-03T15:00:00Z"),
        ("2026-11-03T18:00:00+03:00", "2026-11-03T15:00:00Z"),
        ("2026-11-03T15:00:00Z", "2026-11-03T15:00:00Z"),
        ("2026-11-03T15:00:00", "2026-11-03T15:00:00Z"),
    ],
)
def test_times_are_sent_as_utc_z(given, expected):
    assert to_calendly_time(given) == expected


@pytest.mark.asyncio
async def test_calendly_booking_request_and_result(calendly_settings, prospect):
    transport, requests = _server([httpx.Response(201, json=INVITEE)])
    result, trace = await CalendlyClient(transport).create_booking(
        prospect,
        start_time="2026-11-03T15:00:00+00:00",
        thread_id="thread_9",
        sdr_email="sdr@acme.example",
    )

    assert result["id"] == INVITEE["resource"]["uri"]
    assert result["event"] == INVITEE["resource"]["event"]
    assert result["reschedule_url"].endswith("INV1")
    assert trace.success and trace.event_type == "calendly_booking_created"
    request = requests[0]
    assert str(request.url) == "https://api.calendly.com/invitees"
    assert request.headers["authorization"] == "Bearer cal-token"
    body = json.loads(request.content)
    assert body == {
        "event_type": EVENT_TYPE,
        "start_time": "2026-11-03T15:00:00Z",
        "invitee": {
            "name": "Jane Doe",
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@acme.io",
            "timezone": "America/New_York",
        },
        "tracking": {"utm_source": "conversion_engine", "utm_content": "thread_9"},
        "event_guests": ["sdr@acme.example"],
    }


@pytest.mark.asyncio
async def test_calendly_location_is_sent_when_configured(monkeypatch, calendly_settings, prospect):
    monkeypatch.setattr(settings, "calendly_location_kind", "outbound_call")
    monkeypatch.setattr(settings, "calendly_location", "+15550100")
    transport, requests = _server([httpx.Response(201, json=INVITEE)])
    await CalendlyClient(transport).create_booking(prospect, start_time="2026-11-03T15:00:00Z")
    assert json.loads(requests[0].content)["location"] == {
        "kind": "outbound_call",
        "location": "+15550100",
    }


@pytest.mark.asyncio
async def test_calendly_taken_slot_is_reported(calendly_settings, prospect):
    transport, _ = _server(
        [httpx.Response(400, json={"title": "Invalid Argument", "message": "slot unavailable"})]
    )
    result, trace = await CalendlyClient(transport).create_booking(
        prospect, start_time="2026-11-03T15:00:00Z"
    )
    assert "400" in result["error"]
    assert not trace.success


@pytest.mark.asyncio
async def test_calendly_needs_config_and_email(prospect):
    result, _ = await CalendlyClient().create_booking(prospect, start_time="2026-11-03T15:00:00Z")
    assert "CALENDLY_API_TOKEN" in result["error"]


def test_booking_link(calendly_settings):
    assert CalendlyClient().get_booking_link() == "https://calendly.com/acme/discovery"


@pytest.mark.parametrize(
    ("provider", "cls"), [("calcom", CalComClient), ("calendly", CalendlyClient)]
)
def test_factory_picks_provider(monkeypatch, provider, cls):
    monkeypatch.setattr(settings, "calendar_provider", provider)
    monkeypatch.setattr(calendar_module, "_client", None)
    assert isinstance(get_calendar_client(), cls)
    monkeypatch.setattr(calendar_module, "_client", None)
