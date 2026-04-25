"""
Leadership change detection.
Checks for new CTO / VP Engineering appointments in last 90 days.
Sources: Crunchbase people section + press releases via LLM analysis.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from agent.llm import get_llm_client
from agent.models import Confidence, LeadershipSignal, SourceRef, TraceRecord

logger = logging.getLogger(__name__)


async def check_leadership_change(
    company_name: str,
    crunchbase_record: dict | None = None,
) -> tuple[LeadershipSignal, list[TraceRecord]]:
    """
    Detect recent CTO/VP Engineering changes for a company.

    Uses Crunchbase people data if available, then supplements with
    LLM-based press release analysis.
    """
    traces: list[TraceRecord] = []

    # Check Crunchbase people section first
    if crunchbase_record:
        signal = _check_crunchbase_people(crunchbase_record)
        if signal.change:
            return signal, traces
        press_signal = _check_press_records(crunchbase_record)
        if press_signal.change:
            return press_signal, traces

    # Use LLM to analyze available information
    llm = get_llm_client()
    prompt = _build_leadership_prompt(company_name, crunchbase_record)

    try:
        result, trace = await llm.chat_json(
            messages=[
                {
                    "role": "system",
                    "content": "You are a B2B research analyst. Extract leadership change signals from company data. Return JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            trace_event="enrichment_leadership",
            prospect_company=company_name,
        )
        traces.append(trace)

        return LeadershipSignal(
            change=result.get("change", False),
            role=result.get("role"),
            name=result.get("name"),
            appointed_at=result.get("appointed_at"),
            observed_at=datetime.now(UTC).isoformat(),
            confidence=Confidence(result.get("confidence", "low")),
            sources=[
                SourceRef(
                    url=result.get("source_url"),
                    description=result.get("source_description", "LLM analysis of company data"),
                )
            ]
            if result.get("source_url")
            else [],
        ), traces

    except Exception as e:
        logger.warning("Leadership check failed for %s: %s", company_name, e)
        return LeadershipSignal(
            observed_at=datetime.now(UTC).isoformat(),
            confidence=Confidence.LOW,
        ), traces


def _check_crunchbase_people(record: dict) -> LeadershipSignal:
    """Check Crunchbase record for leadership changes."""
    people = record.get("people") or record.get("founders") or []
    leadership_roles = {
        "cto",
        "vp engineering",
        "vp of engineering",
        "chief technology officer",
        "vice president of engineering",
        "head of engineering",
    }

    for person in people:
        title = (person.get("title") or person.get("role") or "").lower()
        if any(role in title for role in leadership_roles):
            # Check if appointment is recent (from available data)
            started_at = person.get("started_on") or person.get("joined_at")
            if not _is_recent_transition(started_at):
                continue
            return LeadershipSignal(
                change=True,
                role=person.get("title") or person.get("role"),
                name=person.get("name") or person.get("first_name"),
                appointed_at=started_at,
                observed_at=datetime.now(UTC).isoformat(),
                confidence=Confidence.MEDIUM,
                sources=[SourceRef(description="Crunchbase people section")],
            )

    return LeadershipSignal(
        observed_at=datetime.now(UTC).isoformat(),
        confidence=Confidence.LOW,
    )


def _check_press_records(record: dict) -> LeadershipSignal:
    """
    Deterministic press/announcement extraction from Crunchbase-linked records.
    """
    text_blobs = []
    for key in ("press_references", "announcements", "news", "about", "description"):
        value = record.get(key)
        if value:
            text_blobs.append(str(value))
    if not text_blobs:
        return LeadershipSignal(observed_at=datetime.now(UTC).isoformat(), confidence=Confidence.LOW)

    joined = " ".join(text_blobs)
    role_match = re.search(
        r"(cto|chief technology officer|vp engineering|vice president of engineering|head of engineering)",
        joined,
        flags=re.IGNORECASE,
    )
    if not role_match:
        return LeadershipSignal(observed_at=datetime.now(UTC).isoformat(), confidence=Confidence.LOW)

    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", joined)
    appointed_at = date_match.group(1) if date_match else None
    if appointed_at and not _is_recent_transition(appointed_at):
        return LeadershipSignal(observed_at=datetime.now(UTC).isoformat(), confidence=Confidence.LOW)

    return LeadershipSignal(
        change=True,
        role=role_match.group(1),
        appointed_at=appointed_at,
        observed_at=datetime.now(UTC).isoformat(),
        confidence=Confidence.MEDIUM if appointed_at else Confidence.LOW,
        sources=[SourceRef(description="Crunchbase-linked press/announcement records")],
    )


def _is_recent_transition(started_at: str | None, window_days: int = 90) -> bool:
    """Return True only when the appointment date falls within the challenge window."""
    parsed = _parse_date(started_at)
    if parsed is None:
        return False

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    return parsed >= cutoff


def _parse_date(value: str | None) -> datetime | None:
    """Parse the common date shapes seen in Crunchbase-derived records."""
    if not value:
        return None

    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _build_leadership_prompt(company_name: str, record: dict | None = None) -> str:
    """Build the LLM prompt for leadership analysis."""
    context = f"Company: {company_name}\n"
    if record:
        context += f"Industry: {record.get('category_list', 'unknown')}\n"
        context += f"Description: {record.get('short_description', 'N/A')}\n"
        context += f"Employee count: {record.get('employee_count', 'unknown')}\n"
        if record.get("people"):
            context += f"Known people: {json.dumps(record['people'][:5])}\n"

    return f"""Analyze whether {company_name} has had a recent CTO or VP Engineering change
(within the last 90 days from today).

{context}

Return a JSON object with these exact fields:
{{
    "change": true/false,
    "role": "CTO" or "VP Engineering" or null,
    "name": "person name" or null,
    "appointed_at": "YYYY-MM-DD" or null,
    "confidence": "high" or "medium" or "low",
    "source_url": "url" or null,
    "source_description": "description of evidence"
}}

If you cannot determine with confidence, set change to false and confidence to "low".
Do NOT fabricate information. If uncertain, be honest about it.
"""
