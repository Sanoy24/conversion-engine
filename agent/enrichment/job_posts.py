"""
Public job-post scraper via Playwright.
Fetches job listings from company careers pages, BuiltIn, Wellfound.
Respects robots.txt, no login, no captcha bypass.
Produces: open_eng_roles count, ai_adjacent count, 60-day delta.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from agent.config import settings
from agent.models import Confidence, HiringSignal, SourceRef

logger = logging.getLogger(__name__)

# AI/ML related keywords for ai_adjacent role detection
AI_KEYWORDS = {
    "machine learning",
    "ml engineer",
    "ai engineer",
    "data scientist",
    "applied scientist",
    "llm engineer",
    "ai product manager",
    "data platform engineer",
    "ml platform",
    "mlops",
    "deep learning",
    "nlp engineer",
    "computer vision",
    "ai research",
    "inference engineer",
    "model training",
    "agentic",
    "generative ai",
}

# Engineering role keywords
ENG_KEYWORDS = {
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack",
    "fullstack",
    "devops",
    "sre",
    "platform engineer",
    "data engineer",
    "infrastructure engineer",
    "cloud engineer",
    "mobile engineer",
    "ios engineer",
    "android engineer",
    "staff engineer",
    "principal engineer",
    "engineering manager",
}


async def scrape_job_posts(
    company_name: str,
    domain: str | None = None,
    careers_url: str | None = None,
) -> HiringSignal:
    """
    Scrape public job posts for a company.
    Falls back to the frozen snapshot if live scraping fails or is disabled.
    """
    # Try frozen snapshot first (preferred during challenge week)
    snapshot_signal = _check_snapshot(company_name)
    if snapshot_signal:
        return snapshot_signal

    # Attempt live scraping if snapshot not available
    if careers_url or domain:
        try:
            return await _scrape_live(company_name, domain, careers_url)
        except Exception as e:
            logger.warning("Live scraping failed for %s: %s", company_name, e)

    # Return empty signal if nothing found
    return HiringSignal(
        open_eng_roles=None,
        ai_adjacent_eng_roles=None,
        confidence=Confidence.LOW,
    )


def _check_snapshot(company_name: str) -> HiringSignal | None:
    """Check the frozen job-post snapshot from seed data."""
    snapshot_path = Path(settings.job_posts_snapshot_path)
    if not snapshot_path.exists():
        return None

    try:
        with snapshot_path.open(encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    company_key = _normalize_company_name(company_name)

    # Handle list or dict format
    companies = snapshot if isinstance(snapshot, list) else snapshot.get("companies", [])
    for entry in companies:
        entry_name = entry.get("company") or entry.get("name") or ""
        entry_key = _normalize_company_name(entry_name)
        if not company_key or company_key != entry_key:
            continue

        if _is_synthetic_snapshot(snapshot, entry) and not settings.allow_synthetic_job_posts_snapshot:
            logger.warning(
                "Ignoring synthetic job-post snapshot entry for %s; replace with a frozen or live public dataset.",
                company_name,
            )
            return None

        return _parse_snapshot_entry(entry, synthetic=_is_synthetic_snapshot(snapshot, entry))

    return None


def _parse_snapshot_entry(entry: dict, *, synthetic: bool = False) -> HiringSignal:
    """Parse a snapshot entry into a HiringSignal."""
    jobs = entry.get("jobs") or entry.get("postings") or []

    eng_count = 0
    ai_count = 0

    for job in jobs:
        title = (job.get("title") or "").lower()
        if _is_engineering_role(title):
            eng_count += 1
            if _is_ai_adjacent(title):
                ai_count += 1

    delta_60d = entry.get("delta_60d") or entry.get("velocity")

    confidence = Confidence.HIGH if eng_count > 0 else Confidence.LOW
    if synthetic and confidence == Confidence.HIGH:
        confidence = Confidence.LOW

    source_description = "Synthetic placeholder snapshot" if synthetic else "Frozen snapshot"

    return HiringSignal(
        open_eng_roles=eng_count,
        ai_adjacent_eng_roles=ai_count,
        delta_60d=str(delta_60d) if delta_60d else None,
        confidence=confidence,
        sources=[SourceRef(url=entry.get("source_url"), description=source_description)],
    )


def _is_synthetic_snapshot(snapshot: dict | list, entry: dict) -> bool:
    if bool(entry.get("synthetic")):
        return True
    if isinstance(snapshot, dict):
        metadata = snapshot.get("metadata")
        if isinstance(metadata, dict) and bool(metadata.get("synthetic")):
            return True
        return bool(snapshot.get("synthetic"))
    return False


def _normalize_company_name(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


async def _scrape_live(
    company_name: str,
    domain: str | None = None,
    careers_url: str | None = None,
) -> HiringSignal:
    """
    Live scrape using Playwright. Respects robots.txt, no login.
    Limited to 200 companies per challenge week.
    """
    from playwright.async_api import async_playwright

    target_url = careers_url
    if not target_url and domain:
        # Common careers page patterns
        target_url = f"https://{domain}/careers"

    if not target_url:
        return HiringSignal(confidence=Confidence.LOW)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            # Check robots.txt first
            parsed = urlparse(target_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            try:
                robots_resp = await page.goto(robots_url, timeout=5000)
                if robots_resp and robots_resp.ok:
                    robots_text = await page.content()
                    if _is_disallowed(robots_text, parsed.path):
                        logger.info("Robots.txt disallows scraping %s", target_url)
                        return HiringSignal(confidence=Confidence.LOW)
            except Exception:
                pass  # If robots.txt fails, proceed cautiously

            # Fetch the careers page
            await page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
            content = await page.content()

            # Extract job titles from the page
            job_titles = _extract_job_titles(content)

            eng_count = sum(1 for t in job_titles if _is_engineering_role(t.lower()))
            ai_count = sum(1 for t in job_titles if _is_ai_adjacent(t.lower()))

            confidence = Confidence.HIGH if len(job_titles) > 0 else Confidence.LOW

            return HiringSignal(
                open_eng_roles=eng_count,
                ai_adjacent_eng_roles=ai_count,
                confidence=confidence,
                sources=[SourceRef(url=target_url, description="Live scrape")],
            )

        except Exception as e:
            logger.warning("Playwright scraping error for %s: %s", target_url, e)
            return HiringSignal(confidence=Confidence.LOW)
        finally:
            await browser.close()


def _extract_job_titles(html_content: str) -> list[str]:
    """Extract job title strings from HTML using common patterns."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, "lxml")
    titles = set()

    # Common selectors for job listings
    selectors = [
        "h2",
        "h3",
        "h4",
        "[class*='job-title']",
        "[class*='position-title']",
        "[class*='opening']",
        "[class*='posting']",
        "[data-testid*='job']",
        "[data-testid*='position']",
        "a[href*='/jobs/']",
        "a[href*='/positions/']",
        "a[href*='/careers/']",
    ]

    for selector in selectors:
        for el in soup.select(selector):
            text = el.get_text(strip=True)
            if text and 10 < len(text) < 200:
                titles.add(text)

    return list(titles)


def _is_engineering_role(title: str) -> bool:
    """Check if a job title is an engineering role."""
    return any(kw in title for kw in ENG_KEYWORDS) or "engineer" in title


def _is_ai_adjacent(title: str) -> bool:
    """Check if a job title is AI/ML adjacent."""
    return any(kw in title for kw in AI_KEYWORDS)


def _is_disallowed(robots_text: str, path: str) -> bool:
    """Simple robots.txt check — only checks User-agent: * rules."""
    in_star_block = False
    for line in robots_text.split("\n"):
        line = line.strip().lower()
        if line.startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip()
            in_star_block = agent == "*"
        elif in_star_block and line.startswith("disallow:"):
            disallowed = line.split(":", 1)[1].strip()
            if disallowed and path.startswith(disallowed):
                return True
    return False
