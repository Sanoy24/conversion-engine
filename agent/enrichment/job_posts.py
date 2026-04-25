"""
Public job-post scraper via Playwright.
Fetches job listings from company careers pages, BuiltIn, Wellfound.
Respects robots.txt, no login, no captcha bypass.
Produces: open_eng_roles count, ai_adjacent count, 60-day delta.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from agent.config import settings
from agent.models import Confidence, HiringSignal, SourceRef

logger = logging.getLogger(__name__)
_LIVE_CRAWLED_COMPANIES: set[str] = set()

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
        observed_at=datetime.now(UTC).isoformat(),
        sources=[
            SourceRef(
                description=(
                    "No snapshot match and no compliant public-page source resolved "
                    "(BuiltIn/Wellfound/LinkedIn/careers)."
                )
            )
        ],
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

    delta_60d = _compute_delta_60d_from_snapshot_jobs(jobs) or entry.get("delta_60d") or entry.get("velocity")

    confidence = Confidence.HIGH if eng_count > 0 else Confidence.LOW
    if synthetic and confidence == Confidence.HIGH:
        confidence = Confidence.LOW

    source_description = "Synthetic placeholder snapshot" if synthetic else "Frozen snapshot"

    source_ref_description = source_description
    if not synthetic:
        source_ref_description = (
            f"{source_description}; includes explicit source attribution. "
            "delta_60d computed from posting dates when available, otherwise "
            "falls back to snapshot-provided delta."
        )

    return HiringSignal(
        open_eng_roles=eng_count,
        ai_adjacent_eng_roles=ai_count,
        delta_60d=str(delta_60d) if delta_60d else None,
        confidence=confidence,
        observed_at=datetime.now(UTC).isoformat(),
        sources=[
            SourceRef(
                url=entry.get("source_url"),
                description=source_ref_description,
            )
        ],
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

    if not _can_live_crawl(company_name):
        return HiringSignal(
            confidence=Confidence.LOW,
            observed_at=datetime.now(UTC).isoformat(),
            sources=[
                SourceRef(
                    description=(
                        "Live crawl cap reached (200 companies). Falling back to null signal "
                        "to respect challenge constraints."
                    )
                )
            ],
        )

    target_urls = _candidate_job_page_urls(domain=domain, careers_url=careers_url)
    if not target_urls:
        return HiringSignal(confidence=Confidence.LOW)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            scraped_titles: list[str] = []
            source_refs: list[SourceRef] = []
            for target_url in target_urls:
                page = await browser.new_page()
                try:
                    if _is_non_public_source(target_url):
                        # Compliance rule: public-page-only, no-login scraping.
                        source_refs.append(
                            SourceRef(
                                url=target_url,
                                description="Skipped: likely non-public/login-gated path",
                            )
                        )
                        continue

                    if await _robots_disallow(page, target_url):
                        source_refs.append(
                            SourceRef(url=target_url, description="Skipped by robots.txt disallow rule")
                        )
                        continue

                    await page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                    content = await page.content()
                    titles = _extract_job_titles_for_source(target_url=target_url, html_content=content)
                    if titles:
                        scraped_titles.extend(titles)
                        source_refs.append(
                            SourceRef(
                                url=target_url,
                                description="Live scrape (public page, robots checked)",
                            )
                        )
                    else:
                        source_refs.append(
                            SourceRef(url=target_url, description="Live scrape found zero visible roles")
                        )
                except Exception as e:
                    logger.warning("Playwright scraping error for %s: %s", target_url, e)
                    source_refs.append(SourceRef(url=target_url, description=f"Live scrape failed: {e}"))
                finally:
                    await page.close()

            # Deduplicate titles extracted from overlapping pages.
            unique_titles = list(dict.fromkeys(scraped_titles))
            eng_count = sum(1 for t in unique_titles if _is_engineering_role(t.lower()))
            ai_count = sum(1 for t in unique_titles if _is_ai_adjacent(t.lower()))
            confidence = Confidence.HIGH if unique_titles else Confidence.LOW

            baseline = _snapshot_baseline_eng_count(company_name)
            delta_60d = None
            if baseline is not None:
                delta_60d = f"{eng_count - baseline:+d}"
            return HiringSignal(
                open_eng_roles=eng_count if unique_titles else 0,
                ai_adjacent_eng_roles=ai_count if unique_titles else 0,
                delta_60d=delta_60d,
                confidence=confidence,
                observed_at=datetime.now(UTC).isoformat(),
                sources=source_refs,
            )

        except Exception as e:
            logger.warning("Playwright scraping failed for %s: %s", company_name, e)
            return HiringSignal(
                confidence=Confidence.LOW,
                observed_at=datetime.now(UTC).isoformat(),
                sources=[SourceRef(description=f"Live scrape failed: {e}")],
            )
        finally:
            await browser.close()


def _candidate_job_page_urls(domain: str | None, careers_url: str | None) -> list[str]:
    """Build explicit source targets: BuiltIn, Wellfound, LinkedIn, and careers."""
    urls: list[str] = []
    if careers_url:
        urls.append(careers_url)
    if domain:
        host = domain.replace("https://", "").replace("http://", "").strip("/")
        company_slug = host.split(".")[0]
        urls.extend(
            [
                f"https://{host}/careers",
                f"https://www.builtin.com/company/{company_slug}/jobs",
                f"https://wellfound.com/company/{company_slug}/jobs",
                f"https://www.linkedin.com/company/{company_slug}/jobs",
            ]
        )
    # Keep order stable and remove duplicates.
    return list(dict.fromkeys(urls))


def _can_live_crawl(company_name: str) -> bool:
    normalized = _normalize_company_name(company_name)
    if normalized in _LIVE_CRAWLED_COMPANIES:
        return True
    if len(_LIVE_CRAWLED_COMPANIES) >= 200:
        return False
    _LIVE_CRAWLED_COMPANIES.add(normalized)
    return True


def _snapshot_baseline_eng_count(company_name: str) -> int | None:
    """
    Return an engineering-role baseline only when the snapshot is suitable for
    a 60-day delta contract.

    Contract:
      - snapshot metadata has `as_of` timestamp, and
      - as_of is within an expected 60-day baseline window.
    """
    snapshot_path = Path(settings.job_posts_snapshot_path)
    if not snapshot_path.exists():
        return None
    try:
        with snapshot_path.open(encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    company_key = _normalize_company_name(company_name)
    metadata = snapshot.get("metadata", {}) if isinstance(snapshot, dict) else {}
    as_of = _parse_snapshot_as_of(metadata.get("as_of"))
    if as_of is None:
        # Without as_of we cannot assert this baseline corresponds to a 60-day comparison.
        return None
    age_days = (datetime.now(UTC) - as_of).days
    if age_days < 30 or age_days > 90:
        # Guardrail: if baseline is too recent/too old, do not claim "60-day" delta.
        return None

    companies = snapshot if isinstance(snapshot, list) else snapshot.get("companies", [])
    for entry in companies:
        entry_name = entry.get("company") or entry.get("name") or ""
        if _normalize_company_name(entry_name) != company_key:
            continue
        jobs = entry.get("jobs") or entry.get("postings") or []
        return sum(1 for job in jobs if _is_engineering_role((job.get("title") or "").lower()))
    return None


def _parse_snapshot_as_of(raw: object) -> datetime | None:
    if not raw:
        return None
    value = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _is_non_public_source(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("/login", "/signin", "/auth", "accounts.google.com"))


async def _robots_disallow(page, target_url: str) -> bool:
    parsed = urlparse(target_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        robots_resp = await page.goto(robots_url, timeout=5000)
        if robots_resp and robots_resp.ok:
            robots_text = await page.content()
            if _is_disallowed(robots_text, parsed.path):
                logger.info("Robots.txt disallows scraping %s", target_url)
                return True
    except Exception:
        # If robots fetch fails, proceed cautiously with target fetch.
        return False
    return False


def _compute_delta_60d_from_snapshot_jobs(jobs: list[dict]) -> str | None:
    """
    Compute 60-day hiring velocity from dated postings when available.

    If posting dates are unavailable in snapshot entries, return None and let
    callers fall back to an explicit stored delta if present.
    """
    if not jobs:
        return "0"

    now = datetime.now(UTC)
    current_window_start = now - timedelta(days=60)
    previous_window_start = now - timedelta(days=120)

    current_count = 0
    previous_count = 0
    for job in jobs:
        posted_at = _parse_job_date(job)
        if posted_at is None:
            continue
        if posted_at >= current_window_start:
            current_count += 1
        elif previous_window_start <= posted_at < current_window_start:
            previous_count += 1

    if current_count == 0 and previous_count == 0:
        return None
    return f"{current_count - previous_count:+d}"


def _parse_job_date(job: dict) -> datetime | None:
    raw = (
        job.get("posted_at")
        or job.get("postedAt")
        or job.get("created_at")
        or job.get("createdAt")
        or job.get("date")
        or job.get("published_at")
    )
    if not raw:
        return None

    value = str(raw).strip()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


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

    cleaned: list[str] = []
    for title in titles:
        normalized = re.sub(r"\s+", " ", title).strip()
        if normalized:
            cleaned.append(normalized)
    return cleaned


def _extract_job_titles_for_source(target_url: str, html_content: str) -> list[str]:
    host = urlparse(target_url).netloc.lower()
    if "builtin.com" in host:
        return _extract_job_titles_builtin(html_content)
    if "wellfound.com" in host:
        return _extract_job_titles_wellfound(html_content)
    if "linkedin.com" in host:
        return _extract_job_titles_linkedin(html_content)
    return _extract_job_titles_careers(html_content)


def _extract_job_titles_builtin(html_content: str) -> list[str]:
    return _extract_job_titles_with_selectors(
        html_content,
        ["[data-testid*='job-title']", ".job-title", "h2", "h3", "a[href*='/job/']"],
    )


def _extract_job_titles_wellfound(html_content: str) -> list[str]:
    return _extract_job_titles_with_selectors(
        html_content,
        ["[data-test*='job']", "[class*='job-title']", "h2", "h3", "a[href*='/jobs/']"],
    )


def _extract_job_titles_linkedin(html_content: str) -> list[str]:
    return _extract_job_titles_with_selectors(
        html_content,
        [".jobs-search__results-list h3", "[class*='job-card'] h3", "h3", "a[href*='/jobs/view/']"],
    )


def _extract_job_titles_careers(html_content: str) -> list[str]:
    return _extract_job_titles(html_content)


def _extract_job_titles_with_selectors(html_content: str, selectors: list[str]) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, "lxml")
    titles: set[str] = set()
    for selector in selectors:
        for el in soup.select(selector):
            text = re.sub(r"\s+", " ", el.get_text(strip=True)).strip()
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
