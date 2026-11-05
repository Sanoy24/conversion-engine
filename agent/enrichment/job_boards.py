"""
Public job-board APIs: Greenhouse, Lever and Ashby.

These publish a company's actual listings for anyone to read, with no key, login
or scraping, so they are far more accurate than crawling a careers page. There's
no directory of boards, so the board name is guessed from the company's domain and
name, and a guess can land on another company's board. A board only counts as
verified if the listings themselves name the company.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class JobBoard:
    provider: str
    url: str
    titles: list[str]
    verified: bool


def host_of(domain: str) -> str:
    """Bare host of a domain or URL: 'https://www.acme.com/' becomes 'acme.com'."""
    value = domain.strip()
    host = urlparse(value if "//" in value else f"//{value}").netloc.lower()
    return host.removeprefix("www.")


def _board_names(company_name: str | None, domain: str | None) -> list[str]:
    candidates = []
    if domain and (host := host_of(domain)):
        candidates.append(host.split(".")[0])
    if company_name:
        candidates.append("".join(ch for ch in company_name.lower() if ch.isalnum()))
    return list(dict.fromkeys(c for c in candidates if c))


def _names_company(company_name: str | None, texts: list[str]) -> bool:
    if not company_name:
        return False
    pattern = rf"\b{re.escape(company_name.lower())}\b"
    return any(re.search(pattern, text.lower()) for text in texts)


def _greenhouse(name: str, payload: dict, company_name: str | None) -> JobBoard:
    jobs = payload.get("jobs", [])
    return JobBoard(
        provider="Greenhouse",
        url=f"https://boards.greenhouse.io/{name}",
        titles=[job.get("title") or "" for job in jobs],
        verified=_names_company(company_name, [job.get("company_name") or "" for job in jobs]),
    )


def _lever(name: str, payload: list, company_name: str | None) -> JobBoard:
    return JobBoard(
        provider="Lever",
        url=f"https://jobs.lever.co/{name}",
        titles=[job.get("text") or "" for job in payload],
        verified=_names_company(
            company_name, [job.get("descriptionPlain") or "" for job in payload]
        ),
    )


def _ashby(name: str, payload: dict, company_name: str | None) -> JobBoard:
    jobs = [job for job in payload.get("jobs", []) if job.get("isListed", True)]
    return JobBoard(
        provider="Ashby",
        url=f"https://jobs.ashbyhq.com/{name}",
        titles=[job.get("title") or "" for job in jobs],
        verified=_names_company(company_name, [job.get("descriptionPlain") or "" for job in jobs]),
    )


_PROVIDERS = (
    ("https://boards-api.greenhouse.io/v1/boards/{name}/jobs", _greenhouse),
    ("https://api.lever.co/v0/postings/{name}?mode=json", _lever),
    ("https://api.ashbyhq.com/posting-api/job-board/{name}", _ashby),
)


async def find_job_board(
    company_name: str | None,
    domain: str | None,
    client: httpx.AsyncClient | None = None,
) -> JobBoard | None:
    """
    The company's public job board, if one with listings can be found.

    Every provider is checked, because a company can have an old board with a few
    leftover listings on one and its real board on another. A verified board beats an
    unverified one, then the board with the most listings wins. An unverified result
    tells the caller to treat its numbers with less confidence.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    boards: list[JobBoard] = []
    try:
        for name in _board_names(company_name, domain):
            for url_template, parse in _PROVIDERS:
                url = url_template.format(name=name)
                try:
                    response = await client.get(url)
                    if response.status_code != 200:
                        continue
                    board = parse(name, response.json(), company_name)
                except (httpx.HTTPError, ValueError) as e:
                    logger.warning("Job board lookup failed for %s: %s", url, e)
                    continue
                if board.titles:
                    boards.append(board)
    finally:
        if owns_client:
            await client.aclose()
    return max(boards, key=lambda b: (b.verified, len(b.titles)), default=None)
