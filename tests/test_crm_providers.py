"""CRM providers: each client is driven against a mock HTTP server that records requests."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.config import settings
from agent.integrations import crm as crm_module
from agent.integrations.attio import AttioClient
from agent.integrations.crm import get_crm_client
from agent.integrations.pipedrive import PipedriveClient
from agent.integrations.salesforce import SalesforceClient, soql_quote
from agent.models import ConversationState, ProspectInfo


class FakeServer:
    """Routes (method, path) to canned JSON responses and records every request."""

    def __init__(self, routes: dict[tuple[str, str], object]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404, json={"error": "no route"})
        if isinstance(response, list):  # a sequence of responses, consumed in order
            response = response.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == method and r.url.path == path and r.content
        ]


@pytest.fixture
def prospect() -> ProspectInfo:
    return ProspectInfo(
        company="Acme Corp",
        domain="acme.io",
        contact_name="Jane Q Doe",
        contact_email="jane@acme.io",
        contact_title="CTO",
        contact_phone="+15550100",
        employee_count=150,
    )


# ── Pipedrive ─────────────────────────────────────────────────────────


@pytest.fixture
def pipedrive_settings(monkeypatch):
    monkeypatch.setattr(settings, "pipedrive_api_token", "pd-token")
    monkeypatch.setattr(settings, "pipedrive_company_domain", "acme")


@pytest.mark.asyncio
async def test_pipedrive_creates_person_when_not_found(pipedrive_settings, prospect):
    server = FakeServer(
        {
            ("GET", "/api/v2/persons/search"): {"success": True, "data": {"items": []}},
            ("POST", "/api/v2/persons"): {"success": True, "data": {"id": 42}},
        }
    )
    result, trace = await PipedriveClient(server.transport).create_contact(prospect)

    assert result == {"id": "42", "existing": False}
    assert trace.success and trace.event_type == "crm_contact_created"
    assert trace.output_data["provider"] == "pipedrive"
    search = server.requests[0]
    assert search.url.host == "acme.pipedrive.com"
    assert search.headers["x-api-token"] == "pd-token"
    assert search.url.params["term"] == "jane@acme.io"
    assert search.url.params["fields"] == "email"
    assert server.bodies("POST", "/api/v2/persons") == [
        {
            "name": "Jane Q Doe",
            "emails": [{"value": "jane@acme.io", "primary": True, "label": "work"}],
            "phones": [{"value": "+15550100", "primary": True, "label": "work"}],
        }
    ]


@pytest.mark.asyncio
async def test_pipedrive_reuses_existing_person(pipedrive_settings, prospect):
    server = FakeServer(
        {
            ("GET", "/api/v2/persons/search"): {
                "data": {"items": [{"result_score": 1, "item": {"id": 7, "type": "person"}}]}
            }
        }
    )
    result, _ = await PipedriveClient(server.transport).create_contact(prospect)

    assert result == {"id": "7", "existing": True}
    assert [r.method for r in server.requests] == ["GET"]


@pytest.mark.asyncio
async def test_pipedrive_note_is_escaped_html(pipedrive_settings):
    server = FakeServer({("POST", "/api/v1/notes"): {"data": {"id": 9}}})
    result, trace = await PipedriveClient(server.transport).add_note("42", "Hi <b>\nthere")

    assert result == {"id": "9"}
    assert trace.event_type == "crm_note_added"
    assert server.bodies("POST", "/api/v1/notes") == [
        {"content": "Hi &lt;b&gt;<br>there", "person_id": 42}
    ]


@pytest.mark.asyncio
async def test_pipedrive_status_uses_custom_field_or_note(pipedrive_settings, monkeypatch):
    server = FakeServer(
        {("POST", "/api/v1/notes"): {"data": {"id": 1}}, ("PATCH", "/api/v2/persons/42"): {}}
    )
    client = PipedriveClient(server.transport)

    await client.update_contact_status("42", "QUALIFIED")
    assert server.bodies("POST", "/api/v1/notes")[0]["content"] == "Lead status: QUALIFIED"

    monkeypatch.setattr(settings, "pipedrive_status_field", "abc123")
    assert await client.update_contact_status("42", "CALL_BOOKED") == {
        "id": "42",
        "status": "CALL_BOOKED",
    }
    assert server.bodies("PATCH", "/api/v2/persons/42") == [
        {"custom_fields": {"abc123": "CALL_BOOKED"}}
    ]


@pytest.mark.asyncio
async def test_pipedrive_missing_credentials_fail_softly(prospect):
    result, trace = await PipedriveClient().create_contact(prospect)
    assert "PIPEDRIVE_API_TOKEN" in result["error"]
    assert not trace.success


# ── Attio ─────────────────────────────────────────────────────────────


@pytest.fixture
def attio_settings(monkeypatch):
    monkeypatch.setattr(settings, "attio_api_key", "attio-key")


@pytest.mark.asyncio
async def test_attio_asserts_person_by_email(attio_settings, prospect):
    server = FakeServer(
        {("PUT", "/v2/objects/people/records"): {"data": {"id": {"record_id": "rec-1"}}}}
    )
    result, trace = await AttioClient(server.transport).create_contact(prospect)

    assert result == {"id": "rec-1", "existing": False}
    assert trace.success
    request = server.requests[0]
    assert request.headers["authorization"] == "Bearer attio-key"
    assert request.url.params["matching_attribute"] == "email_addresses"
    values = json.loads(request.content)["data"]["values"]
    assert values["email_addresses"] == [{"email_address": "jane@acme.io"}]
    assert values["name"] == [
        {"first_name": "Jane", "last_name": "Q Doe", "full_name": "Jane Q Doe"}
    ]
    assert values["job_title"] == "CTO"


@pytest.mark.asyncio
async def test_attio_requires_email(attio_settings, prospect):
    no_email = prospect.model_copy(update={"contact_email": None})
    result, trace = await AttioClient(FakeServer({}).transport).create_contact(no_email)
    assert "email" in result["error"]
    assert not trace.success


@pytest.mark.asyncio
async def test_attio_note_and_search(attio_settings):
    server = FakeServer(
        {
            ("POST", "/v2/notes"): {"data": {"id": {"note_id": "note-1"}}},
            ("POST", "/v2/objects/people/records/query"): {
                "data": [{"id": {"record_id": "rec-9"}}]
            },
        }
    )
    client = AttioClient(server.transport)

    note, _ = await client.add_note("rec-1", "Outbound sent\nsegment=funded")
    assert note == {"id": "note-1"}
    assert server.bodies("POST", "/v2/notes") == [
        {
            "data": {
                "parent_object": "people",
                "parent_record_id": "rec-1",
                "title": "Outbound sent",
                "format": "plaintext",
                "content": "Outbound sent\nsegment=funded",
            }
        }
    ]
    assert await client.search_contact("x@acme.io") == {"id": "rec-9"}
    assert server.bodies("POST", "/v2/objects/people/records/query") == [
        {"filter": {"email_addresses": "x@acme.io"}, "limit": 1}
    ]


@pytest.mark.asyncio
async def test_attio_status_attribute(attio_settings, monkeypatch):
    monkeypatch.setattr(settings, "attio_status_attribute", "lead_status")
    server = FakeServer({("PATCH", "/v2/objects/people/records/rec-1"): {"data": {}}})
    await AttioClient(server.transport).update_contact_status("rec-1", "UNQUALIFIED")
    assert server.bodies("PATCH", "/v2/objects/people/records/rec-1") == [
        {"data": {"values": {"lead_status": "UNQUALIFIED"}}}
    ]


# ── Salesforce ────────────────────────────────────────────────────────


TOKEN = {"access_token": "sf-token", "instance_url": "https://acme.my.salesforce.com"}
API = "/services/data/v62.0"


@pytest.fixture
def salesforce_settings(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_domain", "https://acme.my.salesforce.com")
    monkeypatch.setattr(settings, "salesforce_client_id", "cid")
    monkeypatch.setattr(settings, "salesforce_client_secret", "csecret")


def test_soql_quote_escapes():
    assert soql_quote("o'brien@x.io") == "'o\\'brien@x.io'"
    assert soql_quote("a\\b") == "'a\\\\b'"


@pytest.mark.asyncio
async def test_salesforce_creates_lead(salesforce_settings, prospect):
    server = FakeServer(
        {
            ("POST", "/services/oauth2/token"): TOKEN,
            ("GET", f"{API}/query"): {"totalSize": 0, "records": []},
            ("POST", f"{API}/sobjects/Lead"): {"id": "00Q1", "success": True, "errors": []},
        }
    )
    result, trace = await SalesforceClient(server.transport).create_contact(prospect)

    assert result == {"id": "00Q1", "existing": False}
    assert trace.success
    token_request = server.requests[0]
    assert dict(httpx.QueryParams(token_request.content.decode())) == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "csecret",
    }
    query = server.requests[1]
    assert query.headers["authorization"] == "Bearer sf-token"
    assert query.url.params["q"] == "SELECT Id FROM Lead WHERE Email = 'jane@acme.io' LIMIT 1"
    lead = server.bodies("POST", f"{API}/sobjects/Lead")[0]
    assert lead["FirstName"] == "Jane"
    assert lead["LastName"] == "Q Doe"
    assert lead["Company"] == "Acme Corp"
    assert lead["Email"] == "jane@acme.io"
    assert lead["Status"] == "Open - Not Contacted"
    assert lead["NumberOfEmployees"] == 150


@pytest.mark.asyncio
async def test_salesforce_reuses_existing_lead_and_token(salesforce_settings, prospect):
    server = FakeServer(
        {
            ("POST", "/services/oauth2/token"): TOKEN,
            ("GET", f"{API}/query"): {"records": [{"Id": "00Q9"}]},
        }
    )
    client = SalesforceClient(server.transport)
    first, _ = await client.create_contact(prospect)
    second, _ = await client.create_contact(prospect)

    assert first == second == {"id": "00Q9", "existing": True}
    token_calls = [r for r in server.requests if r.url.path == "/services/oauth2/token"]
    assert len(token_calls) == 1


@pytest.mark.asyncio
async def test_salesforce_reauthenticates_on_expired_token(salesforce_settings):
    server = FakeServer(
        {
            ("POST", "/services/oauth2/token"): [TOKEN, {**TOKEN, "access_token": "fresh"}],
            ("GET", f"{API}/query"): [
                httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}]),
                {"records": [{"Id": "00Q5"}]},
            ],
        }
    )
    assert await SalesforceClient(server.transport).search_contact("a@b.io") == {"id": "00Q5"}
    assert server.requests[-1].headers["authorization"] == "Bearer fresh"


@pytest.mark.asyncio
async def test_salesforce_status_map_and_task_notes(salesforce_settings, monkeypatch):
    monkeypatch.setattr(settings, "salesforce_status_map", '{"QUALIFIED": "Working"}')
    server = FakeServer(
        {
            ("POST", "/services/oauth2/token"): TOKEN,
            ("PATCH", f"{API}/sobjects/Lead/00Q1"): httpx.Response(204),
            ("POST", f"{API}/sobjects/Task"): {"id": "00T1", "success": True},
        }
    )
    client = SalesforceClient(server.transport)

    await client.update_contact_status("00Q1", "QUALIFIED")
    await client.update_contact_status("00Q1", "UNQUALIFIED")
    assert server.bodies("PATCH", f"{API}/sobjects/Lead/00Q1") == [
        {"Status": "Working"},
        {"Status": "Closed - Not Converted"},
    ]

    note, _ = await client.add_note("00Q1", "Reply received\ndetails")
    assert note == {"id": "00T1"}
    task = server.bodies("POST", f"{API}/sobjects/Task")[0]
    assert task["WhoId"] == "00Q1"
    assert task["Subject"] == "Reply received"
    assert task["Status"] == "Completed"


# ── Factory and stored data ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "cls"),
    [("pipedrive", PipedriveClient), ("salesforce", SalesforceClient), ("attio", AttioClient)],
)
def test_factory_picks_provider(monkeypatch, provider, cls):
    monkeypatch.setattr(settings, "crm_provider", provider)
    monkeypatch.setattr(crm_module, "_client", None)
    assert isinstance(get_crm_client(), cls)
    monkeypatch.setattr(crm_module, "_client", None)


def test_threads_saved_with_old_field_name_still_load(prospect):
    state = ConversationState.model_validate(
        {"thread_id": "t1", "prospect": prospect.model_dump(), "hubspot_contact_id": "hs-1"}
    )
    assert state.crm_contact_id == "hs-1"
    assert "crm_contact_id" in state.model_dump()
