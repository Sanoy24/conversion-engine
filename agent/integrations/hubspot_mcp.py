"""
HubSpot CRM integration via the official HubSpot MCP server.

This is a drop-in replacement for `HubSpotClient` (direct REST API). It spawns
the `@hubspot/mcp-server` Node.js package over stdio, initializes an MCP session,
and routes every CRM write through MCP tool calls so every conversation event
flows through the agent's tool layer.

Design notes:
  - Lazy session: the MCP server is spawned on first use and reused across
    subsequent calls in the same event loop. `close()` tears it down.
  - Interface parity: method signatures and return shapes mirror
    `agent.integrations.hubspot.HubSpotClient` so the orchestrator swap is a
    one-line change.
  - Graceful fallback: if MCP setup fails (Node.js missing, token invalid,
    server crashes), `_ensure_session` raises `HubSpotMCPUnavailable` and the
    factory in hubspot.py falls back to the direct-API client.

Prerequisites:
  1. Node.js 18+ available on PATH (for `npx`)
  2. `HUBSPOT_ACCESS_TOKEN` set (Private App token with CRM scopes)
  3. `USE_HUBSPOT_MCP=true` in .env

Reference: https://developers.hubspot.com/mcp (nine tools as of Feb 2026).
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from agent.config import settings
from agent.models import (
    HiringSignalBrief,
    ICPClassification,
    ProspectInfo,
    TraceRecord,
)

logger = logging.getLogger(__name__)


class HubSpotMCPUnavailable(RuntimeError):
    """Raised when the HubSpot MCP server cannot be reached."""


_HS_EMP_BUCKETS = [(5, "1-5"), (25, "5-25"), (50, "25-50"), (100, "50-100"),
                   (500, "100-500"), (1000, "500-1000")]


def _employee_count_bucket(count: int | None) -> str | None:
    if not count:
        return None
    for ceiling, label in _HS_EMP_BUCKETS:
        if count <= ceiling:
            return label
    return "1000+"


def _parse_tool_result(result: Any) -> dict:
    """
    Parse an MCP CallToolResult into a plain dict.

    MCP tool results come back as a list of content items (TextContent,
    typically containing JSON). We flatten to the first JSON payload we find,
    or wrap the raw text if no JSON is present. Also flattens common HubSpot
    nested shapes like {"object": {...}} or {"result": {...}}.
    """
    try:
        content = getattr(result, "content", None) or []
        is_error = getattr(result, "isError", False)
        for item in content:
            text = getattr(item, "text", None)
            if not text:
                continue
            logger.debug("MCP raw text item: %s", text[:500])
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Some HubSpot MCP tools return plain-text confirmations
                # like "Created contact 12345". Extract any trailing integer
                # so the caller has something to anchor on.
                return {"text": text, "is_error": is_error}

            if not isinstance(parsed, dict):
                return {"raw": parsed, "is_error": is_error}

            # Unwrap common nested shapes
            for key in ("object", "result", "data", "contact", "note"):
                inner = parsed.get(key)
                if isinstance(inner, dict) and inner.get("id"):
                    # Merge top-level keys so `is_error`, `status`, etc. survive
                    merged = {**parsed, **inner}
                    merged["is_error"] = is_error
                    return merged
            parsed["is_error"] = is_error
            return parsed
        return {"is_error": is_error}
    except Exception as e:
        logger.warning("Failed to parse MCP tool result: %s", e)
        return {}


def _extract_id(payload: dict) -> str | None:
    """Pull a HubSpot object ID out of assorted response shapes."""
    if not isinstance(payload, dict):
        return None
    # Direct id
    for key in ("id", "contactId", "objectId", "hs_object_id", "engagementId"):
        v = payload.get(key)
        if v:
            return str(v)
    # Engagement create shape. HubSpot actually returns a doubly-nested structure:
    #   {"status": "...", "engagement": {"associationCreateFailures": [],
    #                                     "engagement": {"id": 123, ...}}}
    engagement = payload.get("engagement")
    if isinstance(engagement, dict):
        for key in ("id", "engagementId", "hs_object_id"):
            if engagement.get(key):
                return str(engagement[key])
        inner_engagement = engagement.get("engagement")
        if isinstance(inner_engagement, dict):
            for key in ("id", "engagementId", "hs_object_id"):
                if inner_engagement.get(key):
                    return str(inner_engagement[key])
    # Batch-create shape: {"results": [{"id": "..."}]}
    results = payload.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for key in ("id", "contactId", "objectId", "hs_object_id"):
                if first.get(key):
                    return str(first[key])
            props = first.get("properties")
            if isinstance(props, dict) and props.get("hs_object_id"):
                return str(props["hs_object_id"])
    # Nested shapes we didn't flatten
    props = payload.get("properties")
    if isinstance(props, dict) and props.get("hs_object_id"):
        return str(props["hs_object_id"])
    return None


def _existing_id_from_conflict(error_text: str) -> str | None:
    """
    Parse an existing HubSpot object ID out of a CONFLICT error message like:
      'Contact already exists. Existing ID: 475618342615'
    """
    if not error_text:
        return None
    import re as _re
    m = _re.search(r"Existing ID:\s*(\d+)", error_text)
    return m.group(1) if m else None


class HubSpotMCPClient:
    """HubSpot CRM client that routes through the official MCP server."""

    def __init__(self):
        self.access_token = settings.hubspot_access_token
        self._session: Any | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._owner_id: int | None = None  # cached after first get-user-details call

    async def _get_owner_id(self) -> int | None:
        """
        Fetch (and cache) an ownerId suitable for engagement creation.

        The HubSpot MCP `hubspot-get-user-details` tool returns plain text with
        an embedded JSON block like:
            - Token Info: {"userId": 91202042, "hubId": ..., ...}

        For Private App tokens there is no distinct "ownerId" — HubSpot's
        engagement API accepts the `userId` as the owner.
        """
        if self._owner_id is not None:
            return self._owner_id
        try:
            session = await self._ensure_session()
            result = await session.call_tool("hubspot-get-user-details", arguments={})
            payload = _parse_tool_result(result)

            # First try structured fields (in case a future MCP version returns JSON)
            owner_id = (
                payload.get("ownerId")
                or payload.get("userId")
                or (payload.get("owner") or {}).get("id")
                or (payload.get("user") or {}).get("id")
            )

            # Fall back: parse embedded JSON block out of the text field
            if not owner_id:
                text = payload.get("text") or ""
                import re as _re
                # Grab the first {...} JSON object we can find
                match = _re.search(r"\{[^{}]*\"userId\"[^{}]*\}", text, _re.DOTALL)
                if not match:
                    # Fallback: any {...} block
                    match = _re.search(r"\{.*?\}", text, _re.DOTALL)
                if match:
                    try:
                        parsed = json.loads(match.group(0))
                        owner_id = parsed.get("userId") or parsed.get("ownerId")
                    except json.JSONDecodeError:
                        pass

            if owner_id:
                self._owner_id = int(owner_id)
                logger.info("HubSpot MCP: resolved ownerId=%s", self._owner_id)
            else:
                logger.warning(
                    "HubSpot MCP: could not resolve ownerId from get-user-details "
                    "(text=%r)", (payload.get("text") or "")[:400],
                )
            return self._owner_id
        except Exception as e:
            logger.warning("HubSpot MCP get-user-details failed: %s", e)
            return None

    async def _ensure_session(self) -> Any:
        """Spawn the MCP server subprocess and initialize a session (once)."""
        if self._session is not None:
            return self._session

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise HubSpotMCPUnavailable(
                "mcp Python package not installed. Run: uv add mcp"
            ) from e

        if not self.access_token:
            raise HubSpotMCPUnavailable("HUBSPOT_ACCESS_TOKEN is empty")

        args_list = [a.strip() for a in settings.hubspot_mcp_args.split(",") if a.strip()]
        params = StdioServerParameters(
            command=settings.hubspot_mcp_command,
            args=args_list,
            env={"PRIVATE_APP_ACCESS_TOKEN": self.access_token},
        )

        try:
            self._exit_stack = AsyncExitStack()
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            logger.info("HubSpot MCP session initialized")
            return self._session
        except Exception as e:
            # Tear down anything we partially set up
            if self._exit_stack:
                try:
                    await self._exit_stack.aclose()
                except Exception:
                    pass
            self._exit_stack = None
            self._session = None
            raise HubSpotMCPUnavailable(f"Failed to start HubSpot MCP server: {e}") from e

    async def close(self) -> None:
        """Shut down the MCP session. Safe to call multiple times."""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("Error closing HubSpot MCP session: %s", e)
        self._exit_stack = None
        self._session = None

    async def create_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None = None,
        classification: ICPClassification | None = None,
    ) -> tuple[dict, TraceRecord]:
        """Create a HubSpot contact via the `hubspot-batch-create-objects` MCP tool."""
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
        properties = {k: v for k, v in raw_properties.items() if v not in (None, "")}
        properties["hs_lead_status"] = "NEW"

        # First-class enrichment fields on the CRM contact: enrichment_timestamp,
        # icp_segment, icp_confidence, ai_maturity_score, signal_brief_trace_id.
        # These make the agent's classification + timing visible in the CRM UI
        # rather than buried in note bodies.
        from agent.integrations.hubspot import (
            _enrichment_properties,
            ensure_custom_properties,
        )
        properties.update(_enrichment_properties(signal_brief, classification, trace_id))
        # Bootstrap the custom properties via the direct REST API (idempotent).
        # We do this via REST rather than via the hubspot-create-property MCP
        # tool because the REST endpoint returns a cleaner 409 on existing
        # properties, which we can treat as success without extra parsing.
        await ensure_custom_properties(self.access_token)

        async def _call_create(props: dict) -> dict:
            session = await self._ensure_session()
            result = await session.call_tool(
                "hubspot-batch-create-objects",
                arguments={
                    "objectType": "contacts",
                    "inputs": [{"properties": props}],
                },
            )
            return _parse_tool_result(result)

        try:
            payload = await _call_create(properties)

            # Resilience: if the portal doesn't have the custom enrichment
            # properties (scope / bootstrap failure), retry without them so
            # the contact still gets created with standard CRM fields.
            if (payload.get("is_error") and
                    ("PROPERTY_DOESNT_EXIST" in (payload.get("text") or "")
                     or "does not exist" in (payload.get("text") or ""))):
                from agent.integrations.hubspot import strip_enrichment_properties
                logger.warning(
                    "HubSpot MCP: custom enrichment properties missing in portal — "
                    "retrying without enrichment_timestamp/icp_segment. Grant "
                    "crm.schemas.contacts.write to your Private App, or create "
                    "these properties in the HubSpot UI for first-class fields."
                )
                payload = await _call_create(strip_enrichment_properties(properties))

            contact_id = _extract_id(payload)

            # One-time visibility: log the payload shape so we can verify the
            # HubSpot MCP response format matches what we parse.
            logger.info(
                "HubSpot MCP create_contact payload keys=%s id=%s is_error=%s text=%r",
                list(payload.keys())[:10], contact_id, payload.get("is_error"),
                (payload.get("text") or "")[:400],
            )

            # If the tool returned isError=True, surface that as a failure
            if payload.get("is_error"):
                err_text = payload.get("text") or payload.get("message") or str(payload)[:200]
                raise RuntimeError(f"HubSpot MCP tool error: {err_text}")

            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_mcp_contact_created",
                prospect_company=prospect.company,
                input_data={"properties_count": len(properties), "via": "mcp"},
                output_data={"contact_id": contact_id, "status": "created"},
                cost_usd=0.0,
                success=bool(contact_id),
            )
            logger.info("HubSpot MCP contact created: %s (ID: %s)", prospect.company, contact_id)
            return {"id": contact_id, **payload}, trace

        except HubSpotMCPUnavailable:
            raise  # let the factory fall back to direct API
        except Exception as e:
            # Treat duplicate-contact errors as success (matches direct-API behavior)
            msg = str(e)
            if "already exists" in msg.lower() or "409" in msg or "CONTACT_EXISTS" in msg:
                existing_id = _existing_id_from_conflict(msg)
                logger.info(
                    "HubSpot MCP: contact already exists for %s (ID: %s)",
                    prospect.company, existing_id,
                )
                trace = TraceRecord(
                    trace_id=trace_id,
                    event_type="hubspot_mcp_contact_created",
                    prospect_company=prospect.company,
                    input_data={"properties_count": len(properties), "via": "mcp"},
                    output_data={"contact_id": existing_id, "status": "already_exists"},
                    cost_usd=0.0,
                    success=True,
                )
                return {"id": existing_id, "status": "already_exists"}, trace

            logger.error("HubSpot MCP create_contact failed for %s: %s", prospect.company, msg)
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_mcp_contact_created",
                prospect_company=prospect.company,
                input_data=properties,
                output_data={"error": msg},
                success=False,
                error=msg,
            )
            return {"error": msg}, trace

    async def add_note(
        self,
        contact_id: str,
        note_body: str,
        prospect_company: str | None = None,
    ) -> tuple[dict, TraceRecord]:
        """Add a note engagement via the `hubspot-create-engagement` MCP tool."""
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"

        try:
            session = await self._ensure_session()
            owner_id = await self._get_owner_id()
            # contact_id must be an int for the `associations.contactIds` array
            try:
                contact_id_int = int(contact_id)
            except (TypeError, ValueError):
                raise RuntimeError(f"Invalid contact_id for MCP note: {contact_id!r}")

            arguments: dict[str, Any] = {
                "type": "NOTE",
                "associations": {"contactIds": [contact_id_int]},
                "metadata": {"body": note_body},
            }
            if owner_id is not None:
                arguments["ownerId"] = owner_id
            # timestamp is optional (defaults to now); skip it to avoid format issues

            result = await session.call_tool("hubspot-create-engagement", arguments=arguments)
            payload = _parse_tool_result(result)
            note_id = _extract_id(payload)
            logger.info(
                "HubSpot MCP add_note payload keys=%s id=%s is_error=%s engagement=%r",
                list(payload.keys())[:10], note_id, payload.get("is_error"),
                str(payload.get("engagement"))[:300],
            )

            if payload.get("is_error"):
                err_text = payload.get("text") or payload.get("message") or str(payload)[:200]
                raise RuntimeError(f"HubSpot MCP tool error: {err_text}")

            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_mcp_note_added",
                prospect_company=prospect_company,
                input_data={"contact_id": contact_id, "note_length": len(note_body), "via": "mcp"},
                output_data={"note_id": note_id},
                cost_usd=0.0,
                success=bool(note_id),
            )
            return {"id": note_id, **payload}, trace

        except HubSpotMCPUnavailable:
            raise
        except Exception as e:
            logger.error("HubSpot MCP add_note failed: %s", e)
            trace = TraceRecord(
                trace_id=trace_id,
                event_type="hubspot_mcp_note_added",
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
        """Update a contact via the `hubspot-update-object` MCP tool."""
        update_props = {"hs_lead_status": status}
        if properties:
            update_props.update(properties)

        try:
            session = await self._ensure_session()
            result = await session.call_tool(
                "hubspot-batch-update-objects",
                arguments={
                    "objectType": "contacts",
                    "inputs": [{"id": contact_id, "properties": update_props}],
                },
            )
            return _parse_tool_result(result)
        except HubSpotMCPUnavailable:
            raise
        except Exception as e:
            logger.error("HubSpot MCP update_contact failed: %s", e)
            return {"error": str(e)}

    async def search_contact(self, email: str) -> dict | None:
        """Search for a contact by email via the `hubspot-search-objects` MCP tool."""
        try:
            session = await self._ensure_session()
            result = await session.call_tool(
                "hubspot-search-objects",
                arguments={
                    "objectType": "contacts",
                    "filterGroups": [
                        {
                            "filters": [
                                {"propertyName": "email", "operator": "EQ", "value": email}
                            ]
                        }
                    ],
                },
            )
            payload = _parse_tool_result(result)
            results = payload.get("results", [])
            return results[0] if results else None
        except HubSpotMCPUnavailable:
            raise
        except Exception as e:
            logger.error("HubSpot MCP search failed: %s", e)
            return None


# Module-level singleton (same pattern as direct-API client)
_hubspot_mcp: HubSpotMCPClient | None = None


def get_hubspot_mcp_client() -> HubSpotMCPClient:
    global _hubspot_mcp
    if _hubspot_mcp is None:
        _hubspot_mcp = HubSpotMCPClient()
    return _hubspot_mcp
