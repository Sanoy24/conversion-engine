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

        properties = {
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
            "numemployees": str(prospect.employee_count) if prospect.employee_count else "",
        }

        # Add enrichment data as notes/custom properties
        enrichment_notes = []
        if signal_brief:
            enrichment_notes.append(f"Crunchbase ID: {prospect.crunchbase_id}")
            enrichment_notes.append(f"Last enriched: {signal_brief.enriched_at}")
            enrichment_notes.append(f"Funding: {signal_brief.funding.event or 'None'}")
            enrichment_notes.append(f"AI Maturity: {signal_brief.ai_maturity.score}/3")
            enrichment_notes.append(
                f"Open Eng Roles: {signal_brief.hiring.open_eng_roles or 'Unknown'}"
            )
            enrichment_notes.append(f"Layoffs: {'Yes' if signal_brief.layoffs.event else 'No'}")
            enrichment_notes.append(
                f"Leadership Change: {'Yes' if signal_brief.leadership.change else 'No'}"
            )

        if classification:
            enrichment_notes.append(f"ICP Segment: {classification.segment.value}")
            enrichment_notes.append(f"Classification Confidence: {classification.confidence.value}")

        properties["hs_lead_status"] = "NEW"
        # Store enrichment as a description note
        if enrichment_notes:
            properties["description"] = "\n".join(enrichment_notes)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
                    headers=self.headers,
                    json={"properties": properties},
                    timeout=10.0,
                )
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


def get_hubspot_client() -> HubSpotClient:
    global _hubspot
    if _hubspot is None:
        _hubspot = HubSpotClient()
    return _hubspot
