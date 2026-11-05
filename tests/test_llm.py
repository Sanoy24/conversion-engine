"""LLM client: retries, daily limit, and what traces record."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI, RateLimitError

from agent import llm as llm_module
from agent.config import settings
from agent.llm import LLMClient, LLMDailyLimitError, is_free_model
from agent.main import app


def _completion(model: str = "google/gemma-4-31b-it:free", usage: dict | None = None) -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"ok": true}'},
            }
        ],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


DAILY = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day."}}
UPSTREAM = {"error": {"code": 429, "message": "temporarily rate-limited upstream"}}


@pytest.fixture
def client_for(monkeypatch):
    """Build an LLMClient against scripted HTTP responses; records calls and sleeps."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_module.asyncio, "sleep", fake_sleep)

    def build(responses: list[httpx.Response], model: str = "google/gemma-4-31b-it:free"):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return responses.pop(0)

        client = LLMClient(model=model)
        client.client = AsyncOpenAI(
            api_key="test",
            base_url="https://openrouter.test/api/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return client, requests, sleeps

    return build


MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    ("model", "free"),
    [
        ("google/gemma-4-31b-it:free", True),
        ("openrouter/free", True),
        ("anthropic/claude-sonnet-4-20250514", False),
        ("deepseek/deepseek-chat-v3-0324", False),
    ],
)
def test_is_free_model(model, free):
    assert is_free_model(model) is free


@pytest.mark.asyncio
async def test_trace_records_the_model_that_answered(client_for):
    client, _, _ = client_for(
        [httpx.Response(200, json=_completion("google/gemma-4-31b-it:free"))],
        model="openrouter/free",
    )
    _content, trace = await client.chat(MESSAGES)
    assert trace.model == "google/gemma-4-31b-it:free"
    assert trace.input_data["model"] == "openrouter/free"


@pytest.mark.asyncio
async def test_free_models_cost_nothing(client_for):
    client, _, _ = client_for([httpx.Response(200, json=_completion())])
    _content, trace = await client.chat(MESSAGES)
    assert trace.cost_usd == 0.0


@pytest.mark.asyncio
async def test_paid_models_use_the_reported_cost(client_for):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0123}
    client, _, _ = client_for(
        [httpx.Response(200, json=_completion("anthropic/x", usage))], model="anthropic/x"
    )
    _content, trace = await client.chat(MESSAGES)
    assert trace.cost_usd == 0.0123


@pytest.mark.asyncio
async def test_reasoning_is_turned_off_on_openrouter(client_for, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    client, requests, _ = client_for([httpx.Response(200, json=_completion())])
    await client.chat(MESSAGES)
    assert json.loads(requests[0].content)["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_other_endpoints_get_no_reasoning_field(client_for, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_base_url", "http://localhost:11434/v1")
    client, requests, _ = client_for([httpx.Response(200, json=_completion())])
    await client.chat(MESSAGES)
    assert "reasoning" not in json.loads(requests[0].content)


@pytest.mark.asyncio
async def test_empty_content_gives_a_clear_error(client_for):
    body = _completion()
    body["choices"][0]["message"]["content"] = None
    body["choices"][0]["finish_reason"] = "length"
    client, _, _ = client_for([httpx.Response(200, json=body)])
    with pytest.raises(ValueError, match=r"no content \(finish_reason=length\)"):
        await client.chat_json(MESSAGES)


@pytest.mark.asyncio
async def test_daily_limit_is_not_retried(client_for):
    client, requests, sleeps = client_for([httpx.Response(429, json=DAILY)])
    with pytest.raises(LLMDailyLimitError):
        await client.chat(MESSAGES)
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_transient_rate_limit_retries_then_succeeds(client_for):
    client, requests, sleeps = client_for(
        [
            httpx.Response(429, json=UPSTREAM, headers={"retry-after": "7"}),
            httpx.Response(200, json=_completion()),
        ]
    )
    content, _trace = await client.chat(MESSAGES)
    assert content == '{"ok": true}'
    assert len(requests) == 2
    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_retries_are_capped(client_for):
    client, requests, sleeps = client_for([httpx.Response(429, json=UPSTREAM) for _ in range(3)])
    with pytest.raises(RateLimitError):
        await client.chat(MESSAGES)
    assert len(requests) == 3
    assert sleeps == [5.0, 10.0]


def test_new_prospect_route_reports_the_daily_limit(monkeypatch):
    async def out_of_quota(**_kwargs):
        raise LLMDailyLimitError("Daily LLM request limit reached")

    monkeypatch.setattr("agent.main.process_new_prospect", out_of_quota)
    response = TestClient(app).post("/api/prospect/new", json={"company_name": "Acme"})
    assert response.status_code == 429
    assert "Daily LLM request limit" in response.json()["detail"]


@pytest.mark.asyncio
async def test_drafter_ignores_invented_links_and_past_times(monkeypatch):
    from agent.core.email_drafter import draft_email
    from agent.core.icp_classifier import classify_prospect
    from agent.models import EmailType, TraceRecord
    from evals.cases import load_cases

    class FakeLLM:
        async def chat_json(self, **_kwargs):
            return {
                "subject": "Question on hiring",
                "body": "Hi Priya.",
                "calcom_link": "https://cal.com/made-up/discovery",
                "proposed_times": [
                    {"prospect_local": "2026-04-22 10:00 ET", "utc": "2026-04-22 14:00 UTC"},
                    {"prospect_local": "2099-01-05 10:00 ET", "utc": "2099-01-05 15:00 UTC"},
                    {"prospect_local": "Tuesday", "utc": "Tuesday"},
                ],
                "score": 0.9,
            }, TraceRecord(trace_id="t", event_type="email_draft")

    class FakeCalendar:
        def get_booking_link(self):
            return "https://cal.example/real-link"

    monkeypatch.setattr("agent.core.email_drafter.get_llm_client", FakeLLM)
    monkeypatch.setattr("agent.core.email_drafter.get_calendar_client", FakeCalendar)
    case = next(c for c in load_cases() if c.id == "funded_clean")

    draft, _ = await draft_email(
        signal_brief=case.brief,
        classification=classify_prospect(case.brief),
        email_type=EmailType.COLD,
    )

    assert draft.calcom_link == "https://cal.example/real-link"
    assert [t.utc for t in draft.proposed_times] == ["2099-01-05 15:00 UTC"]
