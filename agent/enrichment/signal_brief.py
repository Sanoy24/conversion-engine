"""
Signal Brief Orchestrator.
Merges all five enrichment signals + competitor gap into a complete
hiring_signal_brief.json and competitor_gap_brief.json.
This is the entry point for the enrichment pipeline.
"""

from __future__ import annotations

import logging
from datetime import datetime

from agent.config import settings
from agent.enrichment.ai_maturity import score_ai_maturity
from agent.enrichment.competitor_gap import generate_competitor_gap_brief
from agent.enrichment.crunchbase import (
    extract_funding_signal,
    extract_prospect_info,
    search_company,
)
from agent.enrichment.job_posts import scrape_job_posts
from agent.enrichment.layoffs import check_layoffs
from agent.enrichment.leadership import check_leadership_change
from agent.models import (
    BenchMatch,
    CompetitorGapBrief,
    Confidence,
    FundingSignal,
    HiringSignalBrief,
    PitchGuidance,
    ProspectInfo,
    TraceRecord,
)

logger = logging.getLogger(__name__)


async def generate_signal_brief(
    company_name: str | None = None,
    domain: str | None = None,
    crunchbase_id: str | None = None,
) -> tuple[HiringSignalBrief, CompetitorGapBrief | None, list[TraceRecord]]:
    """
    Generate a complete hiring signal brief and competitor gap brief for a prospect.

    This is the main entry point for the enrichment pipeline.
    Returns: (signal_brief, gap_brief, traces)
    """
    all_traces: list[TraceRecord] = []

    # ── Step 1: Find the company in Crunchbase ──
    cb_record = search_company(
        company_name=company_name,
        domain=domain,
        crunchbase_id=crunchbase_id,
    )

    if cb_record:
        prospect = extract_prospect_info(cb_record)
        logger.info(
            "Found Crunchbase record for %s (ID: %s)", prospect.company, prospect.crunchbase_id
        )
    else:
        prospect = ProspectInfo(
            company=company_name or domain or "Unknown",
            domain=domain,
            crunchbase_id=crunchbase_id,
        )
        logger.warning(
            "No Crunchbase record found for %s. Proceeding with limited data.", prospect.company
        )

    # ── Step 2: Extract funding signal ──
    funding = extract_funding_signal(cb_record) if cb_record else None

    # ── Step 3: Scrape job posts for hiring signal ──
    hiring = await scrape_job_posts(
        company_name=prospect.company,
        domain=prospect.domain,
    )

    # ── Step 4: Check layoffs ──
    layoffs = check_layoffs(prospect.company)

    # ── Step 5: Check leadership changes ──
    leadership, leadership_traces = await check_leadership_change(
        company_name=prospect.company,
        crunchbase_record=cb_record,
    )
    all_traces.extend(leadership_traces)

    # ── Step 6: Score AI maturity ──
    ai_maturity = score_ai_maturity(
        hiring=hiring,
        crunchbase_record=cb_record,
    )

    # ── Step 7: Generate pitch guidance ──
    pitch_guidance = _generate_pitch_guidance(ai_maturity.score, ai_maturity.confidence)

    # ── Step 8: Check bench match ──
    bench_match = _check_bench_match()

    # ── Step 9: Check human review triggers ──
    requires_review, review_reason = _check_human_review_triggers(layoffs, leadership)

    # ── Assemble the signal brief ──
    brief = HiringSignalBrief(
        prospect=prospect,
        enriched_at=datetime.utcnow().isoformat(),
        funding=funding or FundingSignal(),
        hiring=hiring,
        layoffs=layoffs,
        leadership=leadership,
        ai_maturity=ai_maturity,
        pitch_guidance=pitch_guidance,
        bench_match=bench_match,
        requires_human_review=requires_review,
        human_review_reason=review_reason,
    )

    # ── Step 10: Generate competitor gap brief ──
    gap_brief = None
    try:
        gap_brief = await generate_competitor_gap_brief(
            prospect=prospect,
            prospect_ai_maturity_score=ai_maturity.score,
            prospect_ai_inputs=ai_maturity.inputs,
        )
    except Exception as e:
        logger.warning("Competitor gap brief generation failed: %s", e)

    logger.info(
        "Signal brief generated for %s: funding=%s, hiring=%s roles, layoffs=%s, "
        "leadership_change=%s, ai_maturity=%d (confidence=%s)",
        prospect.company,
        brief.funding.event,
        brief.hiring.open_eng_roles,
        brief.layoffs.event,
        brief.leadership.change,
        brief.ai_maturity.score,
        brief.ai_maturity.confidence.value,
    )

    return brief, gap_brief, all_traces


def _generate_pitch_guidance(ai_score: int, ai_confidence: Confidence) -> PitchGuidance:
    """Generate pitch guidance based on AI maturity score."""
    segment_4_viable = ai_score >= 2

    if ai_score >= 2:
        tone = "scale_existing"
    else:
        tone = "stand_up_first"

    notes = []
    if ai_confidence == Confidence.LOW and ai_score >= 2:
        notes.append(
            f"Low confidence on ai_maturity={ai_score} — prefer ASK over ASSERT for AI claims."
        )
    if not segment_4_viable:
        notes.append("Segment 4 (capability gap) not viable at this AI maturity level.")

    return PitchGuidance(
        segment_4_viable=segment_4_viable,
        tone_for_segment_1=tone,
        language_notes=" ".join(notes) if notes else None,
    )


def _check_bench_match() -> BenchMatch:
    """
    Cross-reference prospect's implied need against bench_summary.
    Uses the placeholder bench data for now — will swap with real on Day 0.
    """
    bench_path = settings.seeds_path / "bench_summary_PLACEHOLDER.md"
    if not bench_path.exists():
        return BenchMatch(matched=False, gap="bench_summary_not_loaded")

    # For now, return a default match — will be enhanced with real bench parsing
    return BenchMatch(matched=True, thin=False)


def _check_human_review_triggers(layoffs, leadership) -> tuple[bool, str | None]:
    """
    Check if any triggers require routing to human review.
    Per signal-brief skill:
    - Layoff >= 25% in last 30 days
    - Founder departure or public restructure
    """
    reasons = []

    if layoffs.event and layoffs.headcount_pct and layoffs.headcount_pct >= 25:
        reasons.append(f"Recent layoff with {layoffs.headcount_pct}% headcount cut — tone risk.")

    if leadership.change:
        role_lower = (leadership.role or "").lower()
        if "founder" in role_lower:
            reasons.append("Founder departure detected — brand risk.")

    if reasons:
        return True, " | ".join(reasons)

    return False, None
