"""
Competitor gap brief generator.
Identifies 5-10 top-quartile competitors in the prospect's sector,
scores their AI maturity, and extracts specific gaps the prospect shows.
Converts outbound from vendor pitch into a research finding.
"""

from __future__ import annotations

import logging

from agent.enrichment.ai_maturity import score_ai_maturity
from agent.enrichment.crunchbase import get_companies_by_sector
from agent.models import (
    CompetitorGapBrief,
    CompetitorRecord,
    Confidence,
    GapEntry,
    ProspectInfo,
)

logger = logging.getLogger(__name__)


async def generate_competitor_gap_brief(
    prospect: ProspectInfo,
    prospect_ai_maturity_score: int,
    prospect_ai_inputs: list,
) -> CompetitorGapBrief:
    """
    Generate a competitor gap brief for a prospect.

    1. Find 5-10 companies in the same sector and size band
    2. Score each company's AI maturity
    3. Compute the prospect's position in the distribution
    4. Extract 2-3 gaps where top quartile shows practices the prospect doesn't

    The output is a research finding, not a pitch.
    """
    industry = prospect.industry or "technology"
    employee_count = prospect.employee_count or 50

    # Determine size band
    size_band = _get_size_band(employee_count)
    min_emp, max_emp = _get_size_range(size_band)

    # Find competitor companies in the same sector
    competitors = get_companies_by_sector(
        industry=industry,
        min_employees=min_emp,
        max_employees=max_emp,
        limit=10,
    )

    # Filter out the prospect itself
    competitors = [
        c for c in competitors if (c.get("name") or "").lower() != (prospect.company or "").lower()
    ]

    # Score AI maturity for each competitor
    cohort: list[CompetitorRecord] = []
    maturity_scores: list[int] = []

    for comp in competitors[:10]:
        # Basic AI maturity scoring for competitors (simplified — no live scraping)
        comp_maturity = score_ai_maturity(
            crunchbase_record=comp,
        )
        cohort.append(
            CompetitorRecord(
                company=comp.get("name") or comp.get("company_name", "Unknown"),
                ai_maturity=comp_maturity.score,
                source_urls=[f"https://crunchbase.com/organization/{comp.get('permalink', '')}"],
            )
        )
        maturity_scores.append(comp_maturity.score)

    # Compute prospect's position
    if maturity_scores:
        scores_below = sum(1 for s in maturity_scores if s <= prospect_ai_maturity_score)
        percentile = int((scores_below / len(maturity_scores)) * 100)
        rank = f"{scores_below + 1} of {len(maturity_scores) + 1}"
    else:
        percentile = 50
        rank = "unknown"

    # Extract gaps — practices the top quartile shows but the prospect doesn't
    gaps = _identify_gaps(
        prospect_ai_score=prospect_ai_maturity_score,
        prospect_ai_inputs=prospect_ai_inputs,
        cohort=cohort,
        competitors=competitors,
    )

    return CompetitorGapBrief(
        prospect=prospect,
        sector=industry,
        size_band=size_band,
        cohort=cohort,
        prospect_position={"percentile": percentile, "rank": rank},
        gaps=gaps,
    )


def _identify_gaps(
    prospect_ai_score: int,
    prospect_ai_inputs: list,
    cohort: list[CompetitorRecord],
    competitors: list[dict],
) -> list[GapEntry]:
    """
    Identify specific gaps between the prospect and top-quartile peers.
    Only emit a gap if >= 3 of the cohort show the practice publicly.
    """
    gaps: list[GapEntry] = []

    if not cohort:
        return gaps

    # Sort cohort by AI maturity to identify top quartile
    sorted_cohort = sorted(cohort, key=lambda c: c.ai_maturity, reverse=True)
    top_quartile_size = max(1, len(sorted_cohort) // 4)
    sorted_cohort[: top_quartile_size + 1]

    # Check: Named AI/ML leadership
    prospect_has_leadership = any(
        getattr(inp, "type", None) == "named_ai_leadership" and getattr(inp, "evidence", None)
        for inp in prospect_ai_inputs
    )
    leaders_with_ai_leadership = sum(1 for c in cohort if c.ai_maturity >= 2)
    if leaders_with_ai_leadership >= 3 and not prospect_has_leadership:
        gaps.append(
            GapEntry(
                practice="Named Head of AI or VP Data on public team page",
                cohort_adoption=f"{leaders_with_ai_leadership} of {len(cohort)} sector peers",
                prospect_has_it=False,
                confidence=Confidence.MEDIUM,
            )
        )

    # Check: High AI maturity score (active AI function)
    high_maturity_count = sum(1 for c in cohort if c.ai_maturity >= 3)
    if high_maturity_count >= 3 and prospect_ai_score < 3:
        gaps.append(
            GapEntry(
                practice="Active AI function with multiple dedicated roles",
                cohort_adoption=f"{high_maturity_count} of {len(cohort)} top-quartile peers",
                prospect_has_it=False,
                confidence=Confidence.MEDIUM,
            )
        )

    # Check: AI-adjacent hiring velocity
    prospect_has_ai_roles = any(
        getattr(inp, "type", None) == "ai_adjacent_roles" and getattr(inp, "evidence", None)
        for inp in prospect_ai_inputs
    )
    ai_hiring_count = sum(1 for c in cohort if c.ai_maturity >= 1)
    if ai_hiring_count >= 3 and not prospect_has_ai_roles:
        gaps.append(
            GapEntry(
                practice="Active AI/ML hiring in open engineering roles",
                cohort_adoption=f"{ai_hiring_count} of {len(cohort)} sector peers",
                prospect_has_it=False,
                confidence=Confidence.LOW,
            )
        )

    return gaps[:3]  # Return top 2-3 gaps


def _get_size_band(employee_count: int) -> str:
    """Classify employee count into a size band."""
    if employee_count < 15:
        return "1-15"
    if employee_count <= 50:
        return "15-50"
    if employee_count <= 200:
        return "50-200"
    if employee_count <= 1000:
        return "200-1000"
    return "1000+"


def _get_size_range(size_band: str) -> tuple[int, int]:
    """Convert size band to min/max range with buffer."""
    ranges = {
        "1-15": (1, 50),
        "15-50": (10, 200),
        "50-200": (20, 500),
        "200-1000": (100, 2000),
        "1000+": (500, 100000),
    }
    return ranges.get(size_band, (1, 100000))
