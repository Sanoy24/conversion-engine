"""
Leadership change detection.
Checks for new CTO / VP Engineering appointments in last 90 days.
Sources: Crunchbase people section + press releases via LLM analysis.
"""

from __future__ import annotations

import json
import logging

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
        return LeadershipSignal(confidence=Confidence.LOW), traces


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
            return LeadershipSignal(
                change=True,
                role=person.get("title") or person.get("role"),
                name=person.get("name") or person.get("first_name"),
                appointed_at=started_at,
                confidence=Confidence.MEDIUM,
                sources=[SourceRef(description="Crunchbase people section")],
            )

    return LeadershipSignal(confidence=Confidence.LOW)


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
