"""
Attio CRM client: prospects become People records, CRM notes become notes.

Auth: workspace access token as a bearer token against https://api.attio.com/v2.
The token needs record and note read-write scopes.

Contacts are asserted (created or updated) by email address, so re-running a
prospect never creates a duplicate. Attio people have no lead-status attribute
by default: set `ATTIO_STATUS_ATTRIBUTE` to the slug of a text attribute on
People to store it there; otherwise status changes are recorded as notes.
"""

from __future__ import annotations

import httpx

from agent.config import settings
from agent.integrations.crm import BaseCRMClient, split_name
from agent.models import HiringSignalBrief, ICPClassification, ProspectInfo

ATTIO_API_BASE = "https://api.attio.com/v2"


class AttioClient(BaseCRMClient):
    provider = "attio"

    def _client(self) -> httpx.AsyncClient:
        if not settings.attio_api_key:
            raise RuntimeError("ATTIO_API_KEY must be set")
        return self._http(
            base_url=ATTIO_API_BASE,
            headers={"Authorization": f"Bearer {settings.attio_api_key}"},
        )

    async def _find_contact_id(self, email: str) -> str | None:
        async with self._client() as client:
            response = await client.post(
                "/objects/people/records/query",
                json={"filter": {"email_addresses": email}, "limit": 1},
            )
            response.raise_for_status()
            records = response.json().get("data") or []
        return records[0]["id"]["record_id"] if records else None

    async def _upsert_contact(
        self,
        prospect: ProspectInfo,
        signal_brief: HiringSignalBrief | None,
        classification: ICPClassification | None,
    ) -> tuple[str, bool]:
        if not prospect.contact_email:
            raise ValueError("Attio contacts are matched by email; prospect has no contact_email")
        first, last = split_name(prospect)
        values: dict = {
            "email_addresses": [{"email_address": prospect.contact_email}],
            "name": [
                {
                    "first_name": first,
                    "last_name": last,
                    "full_name": prospect.contact_name or prospect.company,
                }
            ],
        }
        if prospect.contact_title:
            values["job_title"] = prospect.contact_title
        if prospect.contact_phone:
            values["phone_numbers"] = [{"original_phone_number": prospect.contact_phone}]

        async with self._client() as client:
            response = await client.put(
                "/objects/people/records",
                params={"matching_attribute": "email_addresses"},
                json={"data": {"values": values}},
            )
            response.raise_for_status()
            # Assert does not report whether the record already existed.
            return response.json()["data"]["id"]["record_id"], False

    async def _add_note(self, contact_id: str, note_body: str) -> str | None:
        title = note_body.splitlines()[0][:120] if note_body.strip() else "Note"
        async with self._client() as client:
            response = await client.post(
                "/notes",
                json={
                    "data": {
                        "parent_object": "people",
                        "parent_record_id": contact_id,
                        "title": title,
                        "format": "plaintext",
                        "content": note_body,
                    }
                },
            )
            response.raise_for_status()
            return ((response.json().get("data") or {}).get("id") or {}).get("note_id")

    async def _set_status(self, contact_id: str, status: str) -> None:
        attribute = settings.attio_status_attribute
        if not attribute:
            await super()._set_status(contact_id, status)
            return
        async with self._client() as client:
            response = await client.patch(
                f"/objects/people/records/{contact_id}",
                json={"data": {"values": {attribute: status}}},
            )
            response.raise_for_status()
