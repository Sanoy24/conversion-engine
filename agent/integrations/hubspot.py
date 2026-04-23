"""
HubSpot CRM integration.
Every conversation event is written to HubSpot.
Contact records track: firmographics, enrichment timestamps, conversation history.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import httpx

from agent.config import settings
from agent.models import (
    HiringSignalBrief,
    ICPClassification,
    ProspectInfo,
    TraceRecord,
)

logger = logging.getLogger(__name__)

HUBSPOT_API_BASE = "https://api.hubapi.com"

_HS_EMP_BUCKETS = [(5, "1-5"), (25, "5-25"), (50, "25-50"), (100, "50-100"),
                   (500, "100-500"), (1000, "500-1000")]


def _employee_count_bucket(count: int | None) -> str | None:
    if not count:
        return None
    for ceiling, label in _HS_EMP_BUCKETS:
        if count <= ceiling:
            return label
    return "1000+"


class HubSpotClient:
    """HubSpot CRM client for contact and deal management."""

    def __init__(self):
        self.access_token = settings.hubspot_access_token
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def create_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None = None,
        classification: ICPClassification | None = None,
    ) -> tuple[dict, TraceRecord]:
        """
        Create a HubSpot contact with firmographic data and enrichment fields.
        All fields must be non-null and enrichment timestamp must be current.
        """
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"

        raw_properties = {
            "email": prospect.contact_email
            or f"{prospect.company.lower().replace(' ', '')}@placeholder.com",
            "firstname": (prospect.contact_name or "").split()[0] if prospect.contact_name else "",
            "lastname": " ".join((prospect.contact_name or "").split()[1:])
            if prospect.contact_name
            else prospect.company,
            "company": prospect.company,
            "jobtitle": prospect.contact_title or "",
            "phone": prospect.contact_phone or "",
            "city": prospect.hq_location or "",
            "industry": prospect.industry or "",
            "website": prospect.domain or "",
            "numemployees": _employee_count_bucket(prospect.employee_count),
        }
        # HubSpot rejects empty strings for typed fields — strip them out.
        properties = {k: v for k, v in raw_properties.items() if v not in (None, "")}

        properties["hs_lead_status"] = "NEW"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
                    headers=self.headers,
                    json={"properties": properties},
                    timeout=10.0,
                )
                if not response.is_success:
                    logger.error("HubSpot error body: %s", response.text)
                response.raise_for_status()
                result = response.json()

            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_contact_created",
                prospect_company=prospect.company,
                input_data={"properties_count": len(properties)},
                output_data={"contact_id": result.get("id"), "status": "created"},
                cost_usd=0.0,
                success=True,
            )

            logger.info("HubSpot contact created: %s (ID: %s)", prospect.company, result.get("id"))
            return result, trace

        except httpx.HTTPStatusError as e:
            # 409 Conflict = contact already exists; extract the existing ID and treat as success.
            if e.response.status_code == 409:
                body = e.response.json()
                existing_id = None
                msg = body.get("message", "")
                if "Existing ID:" in msg:
                    existing_id = msg.split("Existing ID:")[-1].strip().rstrip("}")
                logger.info("HubSpot contact already exists for %s (ID: %s)", prospect.company, existing_id)
                trace = TraceRecord(
                    trace_id=trace_id,
                    event_type="hubspot_contact_created",
                    prospect_company=prospect.company,
                    input_data={"properties_count": len(properties)},
                    output_data={"contact_id": existing_id, "status": "already_exists"},
                    cost_usd=0.0,
                    success=True,
                )
                return {"id": existing_id, "status": "already_exists"}, trace
            logger.error("HubSpot contact creation failed for %s: %s", prospect.company, str(e))
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_contact_created",
                prospect_company=prospect.company,
                input_data=properties,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace
        except Exception as e:
            logger.error("HubSpot contact creation failed for %s: %s", prospect.company, str(e))
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_contact_created",
                prospect_company=prospect.company,
                input_data=properties,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace

    async def add_note(
        self,
        contact_id: str,
        note_body: str,
        prospect_company: str | None = None,
    ) -> tuple[dict, TraceRecord]:
        """Add a note/engagement to a HubSpot contact."""
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"

        try:
            async with httpx.AsyncClient() as client:
                # Create a note
                note_response = await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/notes",
                    headers=self.headers,
                    json={
                        "properties": {
                            "hs_note_body": note_body,
                            "hs_timestamp": datetime.utcnow().isoformat() + "Z",
                        },
                    },
                    timeout=10.0,
                )
                note_response.raise_for_status()
                note_data = note_response.json()

                # Associate note with contact
                note_id = note_data.get("id")
                if note_id:
                    await client.put(
                        f"{HUBSPOT_API_BASE}/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/202",
                        headers=self.headers,
                        timeout=10.0,
                    )

            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_note_added",
                prospect_company=prospect_company,
                input_data={"contact_id": contact_id, "note_length": len(note_body)},
                output_data={"note_id": note_id},
                cost_usd=0.0,
                success=True,
            )
            return note_data, trace

        except Exception as e:
            logger.error("HubSpot note creation failed: %s", str(e))
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_note_added",
                prospect_company=prospect_company,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace

    async def update_contact_status(
        self,
        contact_id: str,
        status: str,
        properties: dict | None = None,
    ) -> dict:
        """Update a contact's lead status and optional properties."""
        update_props = {"hs_lead_status": status}
        if properties:
            update_props.update(properties)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.patch(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}",
                    headers=self.headers,
                    json={"properties": update_props},
                    timeout=10.0,
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error("HubSpot contact update failed: %s", str(e))
            return {"error": str(e)}

    async def close(self) -> None:
        """No-op for interface parity with HubSpotMCPClient."""
        return

    async def search_contact(self, email: str) -> dict | None:
        """Search for a contact by email."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search",
                    headers=self.headers,
                    json={
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "email",
                                        "operator": "EQ",
                                        "value": email,
                                    }
                                ]
                            }
                        ]
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
                results = response.json().get("results", [])
                return results[0] if results else None
        except Exception as e:
            logger.error("HubSpot search failed: %s", str(e))
            return None


# Module-level singleton
_hubspot: HubSpotClient | None = None
_hubspot_client_impl: object | None = None  # may be MCP or direct, decided once at first call


def get_hubspot_client():
    """
    Return the active HubSpot client.

    If `USE_HUBSPOT_MCP=true` in settings, returns a `HubSpotMCPClient` that
    routes every CRM write through the official HubSpot MCP server. Otherwise
    returns the direct REST-API `HubSpotClient`. Both expose the same interface
    (create_contact, add_note, update_contact_status, search_contact).

    The MCP path degrades gracefully: if the MCP server can't be started
    (Node.js missing, bad token, etc.), this function falls back to the
    direct-API client with a one-time warning.
    """
    global _hubspot, _hubspot_client_impl

    if _hubspot_client_impl is not None:
        return _hubspot_client_impl

    if settings.use_hubspot_mcp:
        try:
            from agent.integrations.hubspot_mcp import get_hubspot_mcp_client
            _hubspot_client_impl = get_hubspot_mcp_client()
            logger.info("HubSpot: routing via MCP server (@hubspot/mcp-server)")
            return _hubspot_client_impl
        except Exception as e:
            logger.warning(
                "HubSpot MCP unavailable (%s); falling back to direct REST API", e
            )

    if _hubspot is None:
        _hubspot = HubSpotClient()
    _hubspot_client_impl = _hubspot
    logger.info("HubSpot: routing via direct REST API")
    return _hubspot_client_impl
