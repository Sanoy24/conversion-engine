"""
Crunchbase ODM sample loader.

Loads the 1,001-company Bright Data dataset and provides firmographic lookups.
Every lead must reference a real Crunchbase record by ``uuid``.

Dataset fields (source: luminati-io/Crunchbase-dataset-samples):
  name, id, uuid, url, cb_rank, region, about, industries, operating_status,
  company_type, founded_date, num_employees, country_code, website,
  contact_email, contact_phone, funding_rounds_list, current_employees,
  layoff, leadership_hire, investors, founders, ...
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from agent.config import settings
from agent.models import Confidence, FundingSignal, ProspectInfo, SourceRef

logger = logging.getLogger(__name__)

# In-memory cache of loaded Crunchbase data
_crunchbase_cache: list[dict] | None = None


def _load_crunchbase_data() -> list[dict]:
    """Load the Crunchbase ODM sample from disk."""
    global _crunchbase_cache
    if _crunchbase_cache is not None:
        return _crunchbase_cache

    data_path = Path(settings.crunchbase_data_path)
    if not data_path.exists():
        logger.warning("Crunchbase data not found at %s. Using empty dataset.", data_path)
        _crunchbase_cache = []
        return _crunchbase_cache

    with data_path.open(encoding="utf-8") as f:
        raw = json.load(f)

    # Handle both list and dict formats
    if isinstance(raw, list):
        _crunchbase_cache = raw
    elif isinstance(raw, dict) and "companies" in raw:
        _crunchbase_cache = raw["companies"]
    else:
        _crunchbase_cache = [raw]

    logger.info("Loaded %d companies from Crunchbase ODM sample.", len(_crunchbase_cache))
    return _crunchbase_cache


def search_company(
    company_name: str | None = None,
    domain: str | None = None,
    crunchbase_id: str | None = None,
) -> dict | None:
    """
    Search for a company in the Crunchbase ODM sample.

    Priority: exact UUID match → exact name → normalized domain.
    """
    data = _load_crunchbase_data()

    if crunchbase_id:
        for record in data:
            if record.get("uuid") == crunchbase_id or record.get("id") == crunchbase_id:
                return record

    if company_name:
        name_lower = company_name.lower().strip()
        for record in data:
            rec_name = (record.get("name") or "").lower().strip()
            if rec_name == name_lower:
                return record

    if domain:
        domain_host = _normalize_domain(domain)
        for record in data:
            rec_domain = _normalize_domain(record.get("website") or record.get("url") or "")
            if domain_host and rec_domain == domain_host:
                return record

    return None


def extract_prospect_info(record: dict) -> ProspectInfo:
    """Extract a ``ProspectInfo`` from a Crunchbase record."""
    # Parse contact info from the contacts field
    contact_name, contact_email, contact_phone, contact_title = _extract_contact(record)

    return ProspectInfo(
        company=record.get("name", "Unknown"),
        domain=record.get("website"),
        crunchbase_id=record.get("uuid") or record.get("id"),
        contact_name=contact_name,
        contact_email=contact_email or record.get("contact_email"),
        contact_phone=contact_phone or record.get("contact_phone"),
        contact_title=contact_title,
        hq_location=_extract_location(record),
        employee_count=_extract_employee_count(record),
        industry=_extract_industries(record),
        description=record.get("about") or record.get("full_description"),
    )


def extract_funding_signal(record: dict) -> FundingSignal:
    """Extract funding signal from a Crunchbase record."""
    funding_rounds = _parse_json_field(record.get("funding_rounds_list"))
    highlights = _parse_json_field(record.get("financials_highlights"))

    # Try to find the latest funding round
    event = None
    amount = None
    closed_at = None

    if isinstance(funding_rounds, list) and funding_rounds:
        latest = funding_rounds[0]  # Usually most recent first
        event = latest.get("funding_type") or latest.get("investment_type")
        amount_raw = latest.get("money_raised") or latest.get("amount")
        if amount_raw:
            amount = _parse_money(amount_raw)
        closed_at = latest.get("announced_on") or latest.get("date")
    elif highlights:
        # Fall back to highlights
        if isinstance(highlights, dict):
            funding_total = highlights.get("funding_total")
            if funding_total:
                amount = _parse_money(funding_total)

    # Also check top-level fields
    num_rounds = record.get("funding_rounds")
    if not event and num_rounds:
        try:
            rounds_count = int(num_rounds)
            if rounds_count > 0:
                event = f"{rounds_count} funding rounds"
        except (ValueError, TypeError):
            pass

    confidence = Confidence.LOW
    if event and amount and closed_at:
        confidence = Confidence.HIGH
    elif event or amount:
        confidence = Confidence.MEDIUM

    permalink = record.get("id") or record.get("url", "")
    return FundingSignal(
        event=event,
        amount_usd=amount,
        closed_at=closed_at,
        observed_at=datetime.now(UTC).isoformat(),
        confidence=confidence,
        sources=[SourceRef(url=f"https://www.crunchbase.com/organization/{permalink}")],
    )


def get_companies_by_sector(
    industry: str,
    min_employees: int = 0,
    max_employees: int = 100000,
    limit: int = 10,
) -> list[dict]:
    """Get companies in a sector + size band for competitor gap analysis."""
    data = _load_crunchbase_data()
    matches = []

    industry_lower = industry.lower()
    for record in data:
        rec_industry = _extract_industries(record).lower()
        if industry_lower not in rec_industry:
            continue

        emp_count = _extract_employee_count(record) or 0
        if min_employees <= emp_count <= max_employees:
            matches.append(record)

    # Sort by employee_count descending (larger companies first for top-quartile)
    matches.sort(key=lambda r: _extract_employee_count(r) or 0, reverse=True)
    return matches[:limit]


# ── Private helpers ────────────────────────────────────────────────────


def _extract_contact(record: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract primary contact from the contacts or current_employees field."""
    # Try current_employees for leadership contacts
    employees = _parse_json_field(record.get("current_employees"))
    if isinstance(employees, list):
        # Prefer CTO, VP Eng, or founder
        leadership_titles = {
            "cto",
            "vp engineering",
            "chief technology officer",
            "co-founder",
            "ceo",
        }
        for emp in employees:
            title = (emp.get("title") or emp.get("job_title") or "").lower()
            if any(t in title for t in leadership_titles):
                return (
                    emp.get("name") or emp.get("full_name"),
                    emp.get("email"),
                    emp.get("phone"),
                    emp.get("title") or emp.get("job_title"),
                )
        # Fall back to first employee
        if employees:
            first = employees[0]
            return (
                first.get("name") or first.get("full_name"),
                first.get("email"),
                first.get("phone"),
                first.get("title") or first.get("job_title"),
            )

    # Try founders field
    founders = _parse_json_field(record.get("founders"))
    if isinstance(founders, list) and founders:
        founder = founders[0]
        return (
            founder.get("name") or founder.get("full_name"),
            founder.get("email"),
            None,
            "Founder",
        )

    return None, None, None, None


def _extract_location(record: dict) -> str | None:
    """Extract HQ location string."""
    location = record.get("location")
    if location:
        return str(location)
    hq_regions = record.get("headquarters_regions")
    if hq_regions:
        return str(hq_regions)
    country = record.get("country_code")
    region = record.get("region")
    parts = [p for p in [region, country] if p]
    return ", ".join(parts) if parts else None


def _extract_employee_count(record: dict) -> int | None:
    """Extract employee count, handling range strings like '51-100'."""
    count = record.get("num_employees") or record.get("num_employee_profiles")
    if count is None:
        return None

    if isinstance(count, int):
        return count

    count_str = str(count).replace(",", "").strip()
    if "-" in count_str:
        parts = count_str.split("-")
        try:
            return (int(parts[0]) + int(parts[1])) // 2
        except (ValueError, IndexError):
            pass
    try:
        return int(count_str)
    except ValueError:
        return None


def _extract_industries(record: dict) -> str:
    """Extract industries as a clean string."""
    raw = record.get("industries")
    if not raw:
        return ""

    parsed = _parse_json_field(raw)
    if isinstance(parsed, list):
        # List of dicts like [{"id": "seo", "value": "SEO"}]
        values = []
        for item in parsed:
            if isinstance(item, dict):
                values.append(item.get("value") or item.get("id", ""))
            else:
                values.append(str(item))
        return ", ".join(values)

    return str(raw)


def _parse_json_field(value: object) -> object:
    """Safely parse a JSON string field (Crunchbase stores nested data as JSON strings)."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return value


def _normalize_domain(value: str) -> str:
    """Normalize a company domain or URL to a comparable host string."""
    raw = value.strip().lower()
    if not raw:
        return ""

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc or parsed.path
    host = host.split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _parse_money(value: object) -> int | None:
    """Parse monetary values from various formats."""
    if value is None:
        return None
    val_str = str(value).replace(",", "").replace("$", "").replace("USD", "").strip()
    # Handle "1.5M", "2B" etc.
    multiplier = 1
    if val_str.upper().endswith("B"):
        multiplier = 1_000_000_000
        val_str = val_str[:-1]
    elif val_str.upper().endswith("M"):
        multiplier = 1_000_000
        val_str = val_str[:-1]
    elif val_str.upper().endswith("K"):
        multiplier = 1_000
        val_str = val_str[:-1]
    try:
        return int(float(val_str) * multiplier)
    except (ValueError, TypeError):
        return None
