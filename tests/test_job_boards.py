"""Public job boards: finding a board, verifying its owner, and counting real roles."""

from __future__ import annotations

import httpx
import pytest

from agent.enrichment import job_posts
from agent.enrichment.job_boards import JobBoard, find_job_board, host_of
from agent.enrichment.job_posts import _is_engineering_role, _signal_from_board, scrape_job_posts
from agent.models import Confidence


def _client(routes: dict[str, httpx.Response]) -> tuple[httpx.AsyncClient, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        for fragment, response in routes.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


GREENHOUSE = {"jobs": [{"title": "Backend Engineer", "company_name": "Acme"}]}


@pytest.mark.parametrize(
    ("domain", "host"),
    [
        ("https://www.acme.com/", "acme.com"),
        ("http://acme.io/careers", "acme.io"),
        ("acme.com", "acme.com"),
    ],
)
def test_host_of(domain, host):
    assert host_of(domain) == host


@pytest.mark.asyncio
async def test_greenhouse_board_is_verified_by_company_name():
    client, _ = _client({"greenhouse": httpx.Response(200, json=GREENHOUSE)})
    board = await find_job_board("Acme", "https://www.acme.com/", client)
    assert board.provider == "Greenhouse"
    assert board.titles == ["Backend Engineer"]
    assert board.verified is True


@pytest.mark.asyncio
async def test_lever_board_is_read_from_a_list():
    jobs = [{"text": "Data Engineer", "descriptionPlain": "Join Acme to build things."}]
    client, _ = _client({"lever": httpx.Response(200, json=jobs)})
    board = await find_job_board("Acme", "acme.com", client)
    assert board.provider == "Lever"
    assert board.titles == ["Data Engineer"]
    assert board.verified is True


@pytest.mark.asyncio
async def test_ashby_skips_unlisted_jobs():
    jobs = {
        "jobs": [
            {"title": "ML Engineer", "isListed": True, "descriptionPlain": "Acme is hiring."},
            {"title": "Secret Engineer", "isListed": False, "descriptionPlain": "Acme."},
        ]
    }
    client, _ = _client({"ashby": httpx.Response(200, json=jobs)})
    board = await find_job_board("Acme", "acme.com", client)
    assert board.provider == "Ashby"
    assert board.titles == ["ML Engineer"]


@pytest.mark.asyncio
async def test_verified_board_beats_an_earlier_unverified_one():
    other = {"jobs": [{"title": "Engineer", "company_name": "Someone Else"}]}
    jobs = [{"text": "Engineer", "descriptionPlain": "About Acme"}]
    client, _ = _client(
        {"greenhouse": httpx.Response(200, json=other), "lever": httpx.Response(200, json=jobs)}
    )
    board = await find_job_board("Acme", "acme.com", client)
    assert board.provider == "Lever"
    assert board.verified is True


@pytest.mark.asyncio
async def test_board_with_the_most_listings_wins():
    leftover = {"jobs": [{"title": "Engineer", "company_name": "Acme"}]}
    real = {
        "jobs": [
            {"title": "Engineer", "descriptionPlain": "Acme"},
            {"title": "Data Engineer", "descriptionPlain": "Acme"},
        ]
    }
    client, _ = _client(
        {"greenhouse": httpx.Response(200, json=leftover), "ashby": httpx.Response(200, json=real)}
    )
    board = await find_job_board("Acme", "acme.com", client)
    assert board.provider == "Ashby"


@pytest.mark.asyncio
async def test_unverified_board_is_returned_when_nothing_better_exists():
    other = {"jobs": [{"title": "Engineer", "company_name": "Someone Else"}]}
    client, _ = _client({"greenhouse": httpx.Response(200, json=other)})
    board = await find_job_board("Acme", "acme.com", client)
    assert board.verified is False


@pytest.mark.asyncio
async def test_no_board_empty_board_and_bad_json_all_mean_none():
    empty, _ = _client({"greenhouse": httpx.Response(200, json={"jobs": []})})
    assert await find_job_board("Acme", "acme.com", empty) is None
    missing, _ = _client({})
    assert await find_job_board("Acme", "acme.com", missing) is None
    broken, _ = _client({"": httpx.Response(200, text="not json")})
    assert await find_job_board("Acme", "acme.com", broken) is None


@pytest.mark.asyncio
async def test_network_failure_means_none():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await find_job_board("Acme", "acme.com", client) is None


@pytest.mark.asyncio
async def test_board_is_tried_by_domain_then_by_name():
    client, seen = _client({})
    await find_job_board("Acme Corp", "https://www.acmehq.com", client)
    assert any("/acmehq" in url for url in seen)
    assert any("/acmecorp" in url for url in seen)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("senior backend engineer", True),
        ("engineering manager", True),
        ("forward deployed engineer", True),
        ("sales engineer", False),
        ("marketing engineer", False),
        ("senior developer success engineer - west", False),
        ("solutions engineer, emea", False),
        ("technical recruiter, engineering", False),
        ("account executive", False),
    ],
)
def test_engineering_role_means_building_the_product(title, expected):
    assert _is_engineering_role(title) is expected


def test_signal_counts_real_roles_and_keeps_examples():
    signal = _signal_from_board(
        JobBoard(
            provider="Greenhouse",
            url="https://boards.greenhouse.io/acme",
            titles=[
                " Backend  Engineer ",
                "Backend Engineer",
                "ML Engineer",
                "Sales Engineer",
                "Recruiter, AI",
                "Data Scientist",
            ],
            verified=True,
        )
    )
    assert signal.open_eng_roles == 3
    assert signal.ai_adjacent_eng_roles == 2
    assert signal.sample_titles == ["Backend Engineer", "ML Engineer"]
    assert signal.delta_60d is None
    assert signal.confidence == Confidence.HIGH
    assert signal.sources[0].url == "https://boards.greenhouse.io/acme"


def test_unverified_board_is_only_medium_confidence():
    board = JobBoard("Lever", "https://jobs.lever.co/acme", ["Backend Engineer"], verified=False)
    assert _signal_from_board(board).confidence == Confidence.MEDIUM


@pytest.mark.asyncio
async def test_job_board_is_used_before_the_crawl(monkeypatch):
    async def fake_find(_name, _domain):
        return JobBoard("Ashby", "https://jobs.ashbyhq.com/acme", ["Backend Engineer"], True)

    async def must_not_crawl(*_args, **_kwargs):
        raise AssertionError("crawled despite a job board")

    monkeypatch.setattr(job_posts, "_check_snapshot", lambda _name: None)
    monkeypatch.setattr(job_posts, "find_job_board", fake_find)
    monkeypatch.setattr(job_posts, "_scrape_live", must_not_crawl)

    signal = await scrape_job_posts("Acme", domain="acme.com")
    assert signal.open_eng_roles == 1
