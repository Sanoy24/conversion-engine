"""
Pipedrive CRM client: prospects become persons, CRM notes become person notes.

Auth: API token in the `x-api-token` header, against your company domain
(`https://<PIPEDRIVE_COMPANY_DOMAIN>.pipedrive.com`). Persons use API v2;
notes use API v1 (Pipedrive has no v2 notes endpoint).

Pipedrive persons have no lead-status field. Set `PIPEDRIVE_STATUS_FIELD` to
the 40-character key of a text custom field to store the status there;
otherwise status changes are recorded as notes.
"""

from __future__ import annotations

import html

import httpx

from agent.config import settings
from agent.integrations.crm import BaseCRMClient
from agent.models import HiringSignalBrief, ICPClassification, ProspectInfo


class PipedriveClient(BaseCRMClient):
    provider = "pipedrive"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport)
        domain = settings.pipedrive_company_domain
        self.base_url = f"https://{domain}.pipedrive.com/api" if domain else ""

    def _client(self) -> httpx.AsyncClient:
        if not (self.base_url and settings.pipedrive_api_token):
            raise RuntimeError("PIPEDRIVE_API_TOKEN and PIPEDRIVE_COMPANY_DOMAIN must be set")
        return self._http(
            base_url=self.base_url, headers={"x-api-token": settings.pipedrive_api_token}
        )

    async def _find_contact_id(self, email: str) -> str | None:
        async with self._client() as client:
            response = await client.get(
                "/v2/persons/search",
                params={"term": email, "fields": "email", "exact_match": "true", "limit": 1},
            )
            response.raise_for_status()
            items = (response.json().get("data") or {}).get("items") or []
        return str(items[0]["item"]["id"]) if items else None

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

        body: dict = {"name": prospect.contact_name or prospect.company}
        if prospect.contact_email:
            body["emails"] = [{"value": prospect.contact_email, "primary": True, "label": "work"}]
        if prospect.contact_phone:
            body["phones"] = [{"value": prospect.contact_phone, "primary": True, "label": "work"}]
        async with self._client() as client:
            response = await client.post("/v2/persons", json=body)
            response.raise_for_status()
            return str(response.json()["data"]["id"]), False

    async def _add_note(self, contact_id: str, note_body: str) -> str | None:
        # Notes are HTML in Pipedrive; escape and keep line breaks.
        content = html.escape(note_body).replace("\n", "<br>")
        async with self._client() as client:
            response = await client.post(
                "/v1/notes", json={"content": content, "person_id": int(contact_id)}
            )
            response.raise_for_status()
            note_id = (response.json().get("data") or {}).get("id")
        return str(note_id) if note_id is not None else None

    async def _set_status(self, contact_id: str, status: str) -> None:
        field = settings.pipedrive_status_field
        if not field:
            await super()._set_status(contact_id, status)
            return
        async with self._client() as client:
            response = await client.patch(
                f"/v2/persons/{contact_id}", json={"custom_fields": {field: status}}
            )
            response.raise_for_status()
