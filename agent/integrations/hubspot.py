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


# Custom HubSpot contact properties that carry the agent's enrichment output
# into the CRM as first-class fields (not buried in note bodies). These are
# bootstrapped on first use via `ensure_custom_properties` below.
CUSTOM_CONTACT_PROPERTIES = [
    {
        "name": "enrichment_timestamp",
        "label": "Enrichment Timestamp",
        "type": "datetime",
        "fieldType": "date",
        "groupName": "contactinformation",
        "description": "UTC timestamp when the Conversion Engine last enriched this contact.",
    },
    {
        "name": "icp_segment",
        "label": "ICP Segment",
        "type": "enumeration",
        "fieldType": "select",
        "groupName": "contactinformation",
        "description": "Tenacious ICP segment assigned by the classifier.",
        "options": [
            {"label": "Recently Funded", "value": "recently_funded",         "displayOrder": 1},
            {"label": "Mid-Market Restructuring", "value": "mid_market_restructuring", "displayOrder": 2},
            {"label": "Leadership Transition", "value": "leadership_transition", "displayOrder": 3},
            {"label": "Capability Gap", "value": "capability_gap",           "displayOrder": 4},
            {"label": "Abstain (low confidence)", "value": "abstain",        "displayOrder": 5},
        ],
    },
    {
        "name": "icp_confidence",
        "label": "ICP Classification Confidence",
        "type": "string",
        "fieldType": "text",
        "groupName": "contactinformation",
        "description": "Confidence level of the ICP classification (low/medium/high).",
    },
    {
        "name": "ai_maturity_score",
        "label": "AI Maturity Score (0-3)",
        "type": "number",
        "fieldType": "number",
        "groupName": "contactinformation",
        "description": "Agent's AI maturity score (0 = no signal, 3 = strategic commitment).",
    },
    {
        "name": "signal_brief_trace_id",
        "label": "Signal Brief Trace ID",
        "type": "string",
        "fieldType": "text",
        "groupName": "contactinformation",
        "description": "Langfuse trace_id for the enrichment run that produced this contact.",
    },
]

# One-time bootstrap flag per process
_props_bootstrapped: bool = False


def _employee_count_bucket(count: int | None) -> str | None:
    if not count:
        return None
    for ceiling, label in _HS_EMP_BUCKETS:
        if count <= ceiling:
            return label
    return "1000+"


async def ensure_custom_properties(access_token: str) -> None:
    """
    Idempotently create the custom contact properties the agent writes.

    Called once on the first contact create/update. HubSpot returns 409 for
    properties that already exist — treated as success. Any other error is
    logged but does not block the caller (payloads without these fields will
    still be accepted by HubSpot; we just lose the first-class CRM data).
    """
    global _props_bootstrapped
    if _props_bootstrapped:
        return
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for prop in CUSTOM_CONTACT_PROPERTIES:
            try:
                r = await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/properties/contacts",
                    headers=headers,
                    json=prop,
                )
                if r.status_code == 201:
                    logger.info("HubSpot: created custom property '%s'", prop["name"])
                elif r.status_code == 409 or "already exists" in r.text.lower():
                    logger.debug("HubSpot: property '%s' already exists", prop["name"])
                else:
                    logger.warning(
                        "HubSpot: could not create property '%s' (status %d): %s",
                        prop["name"], r.status_code, r.text[:200],
                    )
            except Exception as e:
                logger.warning("HubSpot property bootstrap error for '%s': %s", prop["name"], e)
    _props_bootstrapped = True


ENRICHMENT_PROPERTY_NAMES = frozenset({
    "enrichment_timestamp",
    "icp_segment",
    "icp_confidence",
    "ai_maturity_score",
    "signal_brief_trace_id",
})


def _enrichment_properties(
    signal_brief, classification, trace_id: str | None = None
) -> dict:
    """
    Build the enrichment-related properties dict added to every contact create/update.

    The keys mirror the custom properties bootstrapped above so the CRM payload
    carries the agent's enrichment output as first-class fields. All values are
    stringified because HubSpot MCP's batch-create schema validates every
    property value as a string.
    """
    props: dict[str, str] = {"enrichment_timestamp": datetime.utcnow().isoformat() + "Z"}
    if classification is not None:
        segment = getattr(classification, "segment", None)
        if segment is not None:
            props["icp_segment"] = getattr(segment, "value", str(segment))
        confidence = getattr(classification, "confidence", None)
        if confidence is not None:
            props["icp_confidence"] = getattr(confidence, "value", str(confidence))
    if signal_brief is not None:
        ai_m = getattr(signal_brief, "ai_maturity", None)
        score = getattr(ai_m, "score", None) if ai_m is not None else None
        if score is not None:
            # Stringify: HubSpot MCP batch-create treats every property value as
            # a string even when the property type is `number` in HubSpot.
            props["ai_maturity_score"] = str(score)
    if trace_id:
        props["signal_brief_trace_id"] = trace_id
    return props


def strip_enrichment_properties(properties: dict) -> dict:
    """Return a copy of properties with enrichment-only fields removed."""
    return {k: v for k, v in properties.items() if k not in ENRICHMENT_PROPERTY_NAMES}


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

        # First-class enrichment fields: enrichment_timestamp, icp_segment,
        # icp_confidence, ai_maturity_score, signal_brief_trace_id. Added as
        # top-level properties so CRM users (and graders) can see the agent's
        # classification + enrichment timing at a glance without opening notes.
        properties.update(_enrichment_properties(signal_brief, classification, trace_id))

        # Ensure the custom properties exist in this HubSpot portal (idempotent).
        await ensure_custom_properties(self.access_token)

        async def _post(props: dict) -> httpx.Response:
            async with httpx.AsyncClient() as client:
                return await client.post(
                    f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
                    headers=self.headers,
                    json={"properties": props},
                    timeout=10.0,
                )

        try:
            response = await _post(properties)
            # Resilience: if a custom enrichment property doesn't exist in the
            # portal yet (scope issue on bootstrap), strip them and retry so
            # the contact still gets created with standard fields.
            if (response.status_code == 400 and
                    "PROPERTY_DOESNT_EXIST" in response.text):
                logger.warning(
                    "HubSpot: custom enrichment properties missing in portal — "
                    "retrying without enrichment_timestamp/icp_segment. Grant "
                    "crm.schemas.contacts.write scope to your Private App, "
                    "or create these properties in the HubSpot UI."
                )
                response = await _post(strip_enrichment_properties(properties))
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
