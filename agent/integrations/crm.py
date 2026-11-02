"""
CRM abstraction: one interface, several providers.

Pick the provider with `CRM_PROVIDER`:

    hubspot     HubSpot (REST, or the official MCP server with USE_HUBSPOT_MCP=true)
    pipedrive   Pipedrive persons + notes
    salesforce  Salesforce Leads + Tasks
    attio       Attio people records + notes

Every client exposes the same coroutines:

    create_contact(prospect, signal_brief=None, classification=None) -> (dict, TraceRecord)
        dict has "id" on success, "error" on failure. Existing contacts are reused.
    add_note(contact_id, note_body, prospect_company=None) -> (dict, TraceRecord)
    update_contact_status(contact_id, status, properties=None) -> dict
        status is one of LEAD_STATUSES; `properties` is HubSpot-specific and
        ignored elsewhere.
    search_contact(email) -> dict | None
    close() -> None

CRM failures never stop the pipeline: they are logged and recorded in traces.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

import httpx

from agent.config import settings
from agent.models import HiringSignalBrief, ICPClassification, ProspectInfo, TraceRecord

logger = logging.getLogger(__name__)

CRM_PROVIDERS = ("hubspot", "pipedrive", "salesforce", "attio")

# Lead statuses the engine sets. Providers map them onto their own fields.
LEAD_STATUSES = ("NEW", "QUALIFIED", "UNQUALIFIED", "CALL_BOOKED")


class CRMClient(Protocol):
    async def create_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None = None,
        classification: ICPClassification | None = None,
    ) -> tuple[dict, TraceRecord]: ...

    async def add_note(
        self, contact_id: str, note_body: str, prospect_company: str | None = None
    ) -> tuple[dict, TraceRecord]: ...

    async def update_contact_status(
        self, contact_id: str, status: str, properties: dict | None = None
    ) -> dict: ...

    async def search_contact(self, email: str) -> dict | None: ...

    async def close(self) -> None: ...


def split_name(prospect: ProspectInfo) -> tuple[str, str]:
    """(first, last) from the contact name; falls back to the company as the last name."""
    parts = (prospect.contact_name or "").split()
    if not parts:
        return "", prospect.company
    return parts[0], " ".join(parts[1:])


class BaseCRMClient:
    """
    Shared tracing and error handling for the REST-based providers.

    Subclasses implement the provider calls: `_upsert_contact`, `_add_note`,
    `_find_contact_id` and optionally `_set_status` (default: record the
    status as a note, for CRMs without a lead-status field).
    """

    provider = "crm"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # `transport` lets tests swap in httpx.MockTransport.
        self._transport = transport

    def _http(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=10.0, **kwargs)

    # Provider hooks ----------------------------------------------------

    async def _upsert_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None,
        classification: ICPClassification | None,
    ) -> tuple[str, bool]:
        """Create the contact or find the existing one. Returns (id, already_existed)."""
        raise NotImplementedError

    async def _add_note(self, contact_id: str, note_body: str) -> str | None:
        raise NotImplementedError

    async def _find_contact_id(self, email: str) -> str | None:
        raise NotImplementedError

    async def _set_status(self, contact_id: str, status: str) -> None:
        await self._add_note(contact_id, f"Lead status: {status}")

    # Public interface --------------------------------------------------

    def _trace(self, event: str, trace_id: str, **fields: Any) -> TraceRecord:
        fields.setdefault("output_data", {})
        fields["output_data"] = {"provider": self.provider, **fields["output_data"]}
        return TraceRecord(trace_id=trace_id, event_type=f"crm_{event}", cost_usd=0.0, **fields)

    async def create_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None = None,
        classification: ICPClassification | None = None,
    ) -> tuple[dict, TraceRecord]:
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"
        input_data = {"company": prospect.company, "email": prospect.contact_email}
        try:
            contact_id, existed = await self._upsert_contact(prospect, signal_brief, classification)
        except Exception as e:
            logger.error(
                "%s contact creation failed for %s: %s", self.provider, prospect.company, e
            )
            trace = self._trace(
                "contact_created",
                trace_id,
                prospect_company=prospect.company,
                input_data=input_data,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace
        trace = self._trace(
            "contact_created",
            trace_id,
            prospect_company=prospect.company,
            input_data=input_data,
            output_data={"contact_id": contact_id, "existing": existed},
        )
        return {"id": contact_id, "existing": existed}, trace

    async def add_note(
        self, contact_id: str, note_body: str, prospect_company: str | None = None
    ) -> tuple[dict, TraceRecord]:
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"
        input_data = {"contact_id": contact_id, "note_length": len(note_body)}
        try:
            note_id = await self._add_note(contact_id, note_body)
        except Exception as e:
            logger.error("%s note creation failed: %s", self.provider, e)
            trace = self._trace(
                "note_added",
                trace_id,
                prospect_company=prospect_company,
                input_data=input_data,
                output_data={"error": str(e)},
                success=False,
                error=str(e),
            )
            return {"error": str(e)}, trace
        trace = self._trace(
            "note_added",
            trace_id,
            prospect_company=prospect_company,
            input_data=input_data,
            output_data={"note_id": note_id},
        )
        return {"id": note_id}, trace

    async def update_contact_status(
        self, contact_id: str, status: str, properties: dict | None = None
    ) -> dict:
        try:
            await self._set_status(contact_id, status)
        except Exception as e:
            logger.error("%s status update failed: %s", self.provider, e)
            return {"error": str(e)}
        return {"id": contact_id, "status": status}

    async def search_contact(self, email: str) -> dict | None:
        try:
            contact_id = await self._find_contact_id(email)
        except Exception as e:
            logger.error("%s search failed: %s", self.provider, e)
            return None
        return {"id": contact_id} if contact_id else None

    async def close(self) -> None:
        return


# ── Factory ───────────────────────────────────────────────────────────

_client: CRMClient | None = None


def get_crm_client() -> CRMClient:
    """The CRM client for `CRM_PROVIDER`, created once per process."""
    global _client
    if _client is not None:
        return _client

    provider = settings.crm_provider
    if provider == "hubspot":
        from agent.integrations.hubspot import get_hubspot_client

        _client = get_hubspot_client()
    elif provider == "pipedrive":
        from agent.integrations.pipedrive import PipedriveClient

        _client = PipedriveClient()
    elif provider == "salesforce":
        from agent.integrations.salesforce import SalesforceClient

        _client = SalesforceClient()
    elif provider == "attio":
        from agent.integrations.attio import AttioClient

        _client = AttioClient()
    else:
        raise ValueError(f"Unknown CRM_PROVIDER {provider!r}; expected one of {CRM_PROVIDERS}")
    logger.info("CRM provider: %s", provider)
    return _client


async def reset_crm_client() -> None:
    """Close and forget the current client (the next call builds a fresh one)."""
    global _client
    if _client is not None:
        await _client.close()
    _client = None
    if settings.crm_provider == "hubspot":
        import agent.integrations.hubspot as hubspot_module

        hubspot_module._hubspot = None
        hubspot_module._hubspot_client_impl = None
        try:
            import agent.integrations.hubspot_mcp as hubspot_mcp_module

            hubspot_mcp_module._hubspot_mcp = None
        except ImportError:
            pass
