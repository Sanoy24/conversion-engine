"""
Salesforce CRM client: prospects become Leads, CRM notes become completed Tasks.

Auth: OAuth 2.0 client-credentials flow against your org's My Domain
(`SALESFORCE_DOMAIN`, e.g. https://acme.my.salesforce.com). Create an External
Client App (or Connected App) with the client-credentials flow enabled and a
run-as user, then set SALESFORCE_CLIENT_ID / SALESFORCE_CLIENT_SECRET.

Lead statuses map onto the Lead `Status` picklist. The default map uses the
picklist values of a standard org; override it with SALESFORCE_STATUS_MAP if
yours differ, e.g. {"QUALIFIED": "Working"}.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx

from agent.config import settings
from agent.integrations.crm import BaseCRMClient, split_name
from agent.models import HiringSignalBrief, ICPClassification, ProspectInfo

logger = logging.getLogger(__name__)

DEFAULT_STATUS_MAP = {
    "NEW": "Open - Not Contacted",
    "QUALIFIED": "Working - Contacted",
    "CALL_BOOKED": "Working - Contacted",
    "UNQUALIFIED": "Closed - Not Converted",
}


def soql_quote(value: str) -> str:
    """Quote a string literal for SOQL."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class SalesforceClient(BaseCRMClient):
    provider = "salesforce"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport)
        self._token: str | None = None
        self._instance_url: str | None = None
        overrides = json.loads(settings.salesforce_status_map or "{}")
        self.status_map = {**DEFAULT_STATUS_MAP, **overrides}

    @property
    def _api(self) -> str:
        return f"/services/data/{settings.salesforce_api_version}"

    async def _authenticate(self) -> None:
        if not (
            settings.salesforce_domain
            and settings.salesforce_client_id
            and settings.salesforce_client_secret
        ):
            raise RuntimeError(
                "SALESFORCE_DOMAIN, SALESFORCE_CLIENT_ID and SALESFORCE_CLIENT_SECRET must be set"
            )
        async with self._http() as client:
            response = await client.post(
                f"{settings.salesforce_domain.rstrip('/')}/services/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.salesforce_client_id,
                    "client_secret": settings.salesforce_client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        self._token = payload["access_token"]
        self._instance_url = payload.get("instance_url") or settings.salesforce_domain

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request; re-authenticates once if the token has expired."""
        for attempt in (1, 2):
            if self._token is None:
                await self._authenticate()
            async with self._http(
                base_url=self._instance_url, headers={"Authorization": f"Bearer {self._token}"}
            ) as client:
                response = await client.request(method, f"{self._api}{path}", **kwargs)
            if response.status_code == 401 and attempt == 1:
                self._token = None
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("unreachable")

    async def _find_contact_id(self, email: str) -> str | None:
        query = f"SELECT Id FROM Lead WHERE Email = {soql_quote(email)} LIMIT 1"
        response = await self._request("GET", "/query", params={"q": query})
        records = response.json().get("records") or []
        return records[0]["Id"] if records else None

    async def _upsert_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None,
        classification: ICPClassification | None,
    ) -> tuple[str, bool]:
        if prospect.contact_email:
            existing = await self._find_contact_id(prospect.contact_email)
            if existing:
                return existing, True

        first, last = split_name(prospect)
        lead = {
            "FirstName": first or None,
            "LastName": last or prospect.company,  # required by Salesforce
            "Company": prospect.company,  # required by Salesforce
            "Email": prospect.contact_email,
            "Title": prospect.contact_title,
            "Phone": prospect.contact_phone,
            "Website": prospect.domain,
            "Industry": prospect.industry,
            "NumberOfEmployees": prospect.employee_count,
            "Status": self.status_map["NEW"],
            "LeadSource": "Conversion Engine",
        }
        lead = {k: v for k, v in lead.items() if v not in (None, "")}
        response = await self._request("POST", "/sobjects/Lead", json=lead)
        return response.json()["id"], False

    async def _add_note(self, contact_id: str, note_body: str) -> str | None:
        subject = note_body.splitlines()[0][:255] if note_body.strip() else "Note"
        response = await self._request(
            "POST",
            "/sobjects/Task",
            json={
                "WhoId": contact_id,
                "Subject": subject,
                "Description": note_body,
                "Status": "Completed",
                "ActivityDate": datetime.now(UTC).date().isoformat(),
            },
        )
        return response.json().get("id")

    async def _set_status(self, contact_id: str, status: str) -> None:
        picklist_value = self.status_map.get(status)
        if picklist_value is None:
            logger.warning("No Salesforce Lead status mapped for %s; recording as a Task", status)
            await super()._set_status(contact_id, status)
            return
        await self._request(
            "PATCH", f"/sobjects/Lead/{contact_id}", json={"Status": picklist_value}
        )
