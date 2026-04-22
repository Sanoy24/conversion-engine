"""
ICP Classifier with Abstention.
Maps hiring_signal_brief to one of four fixed segment labels (or abstains).
Segment names are fixed for grading — do not rename them.

Overlap rules:
- Recent funding + recent layoffs → Segment 2 (cost pressure overrides)
- Leadership transition + Segment 1/2 → Segment 3 primary (narrower window)
"""

from __future__ import annotations

import logging

from agent.models import (
    Confidence,
    EvidenceItem,
    HiringSignalBrief,
    ICPClassification,
    ICPSegment,
)

logger = logging.getLogger(__name__)


def classify_prospect(brief: HiringSignalBrief) -> ICPClassification:
    """
    Classify a prospect into one of four ICP segments or abstain.

    Rules applied in priority order:
    1. Check for leadership transition (narrow window, highest priority)
    2. Check for mid-market restructuring (cost pressure overrides funding)
    3. Check for recently-funded (fresh budget)
    4. Check for capability gap (requires AI maturity >= 2)
    5. Abstain if no segment matches with sufficient confidence
    """
    disqualifiers_checked: list[str] = []

    p = brief.prospect
    funding_sig = brief.funding
    hiring_sig = brief.hiring
    layoffs_sig = brief.layoffs
    leadership_sig = brief.leadership
    ai_sig = brief.ai_maturity
    emp_count = p.employee_count or 0

    # ── Evaluate each segment ──

    # Segment 3: Leadership Transition (check first — narrowest window)
    seg3_score, seg3_evidence = _evaluate_segment_3(leadership_sig)

    # Segment 2: Mid-market Restructuring
    seg2_score, seg2_evidence, seg2_disquals = _evaluate_segment_2(
        layoffs_sig, emp_count, funding_sig
    )
    disqualifiers_checked.extend(seg2_disquals)

    # Segment 1: Recently Funded
    seg1_score, seg1_evidence, seg1_disquals = _evaluate_segment_1(
        funding_sig, emp_count, layoffs_sig
    )
    disqualifiers_checked.extend(seg1_disquals)

    # Segment 4: Capability Gap
    seg4_score, seg4_evidence, seg4_disquals = _evaluate_segment_4(ai_sig, hiring_sig)
    disqualifiers_checked.extend(seg4_disquals)

    # ── Apply overlap resolution ──

    # Overlap 1: Recent funding + recent layoffs → Segment 2
    if seg1_score > 0 and seg2_score > 0 and layoffs_sig.event:
        logger.info("Overlap: funding + layoffs → forcing Segment 2 (restructuring)")
        seg1_score = 0  # Override segment 1
        seg2_evidence.append(
            EvidenceItem(
                signal="overlap_resolution",
                value="Funding within 180d overridden by more recent layoffs",
                weight="qualifying",
            )
        )

    # Find the best segment
    scores = {
        ICPSegment.LEADERSHIP_TRANSITION: seg3_score,
        ICPSegment.MID_MARKET_RESTRUCTURING: seg2_score,
        ICPSegment.RECENTLY_FUNDED: seg1_score,
        ICPSegment.CAPABILITY_GAP: seg4_score,
    }

    evidence_map = {
        ICPSegment.LEADERSHIP_TRANSITION: seg3_evidence,
        ICPSegment.MID_MARKET_RESTRUCTURING: seg2_evidence,
        ICPSegment.RECENTLY_FUNDED: seg1_evidence,
        ICPSegment.CAPABILITY_GAP: seg4_evidence,
    }

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_segment, best_score = ranked[0]
    second_segment, second_score = ranked[1] if len(ranked) > 1 else (None, 0)

    # ── Abstention check ──
    if best_score == 0:
        return ICPClassification(
            prospect=p,
            segment=ICPSegment.ABSTAIN,
            confidence=Confidence.LOW,
            evidence=[],
            disqualifiers_checked=disqualifiers_checked,
            overlap_notes="No segment met minimum qualifying signals.",
        )

    # ── Confidence calibration ──
    confidence = _calibrate_confidence(brief, best_segment)

    if confidence == Confidence.LOW:
        return ICPClassification(
            prospect=p,
            segment=ICPSegment.ABSTAIN,
            confidence=Confidence.LOW,
            evidence=evidence_map.get(best_segment, []),
            disqualifiers_checked=disqualifiers_checked,
            overlap_notes=(
                f"Best candidate was {best_segment.value}, but confidence stayed below the "
                "abstention threshold."
            ),
            pitch_guidance_ref=brief.pitch_guidance,
        )

    # Overlap 2: Leadership transition + Segment 1/2
    secondary = None
    overlap_notes = None
    if best_segment == ICPSegment.LEADERSHIP_TRANSITION and second_score > 0:
        secondary = second_segment
        overlap_notes = f"Leadership transition is primary (90-day window); {second_segment.value} is secondary."
    elif seg3_score > 0 and best_segment != ICPSegment.LEADERSHIP_TRANSITION:
        # If leadership transition qualifies but isn't primary, note it
        secondary = ICPSegment.LEADERSHIP_TRANSITION
        overlap_notes = (
            f"Leadership transition detected but {best_segment.value} is stronger signal."
        )

    return ICPClassification(
        prospect=p,
        segment=best_segment,
        secondary_segment=secondary,
        confidence=confidence,
        evidence=evidence_map.get(best_segment, []),
        disqualifiers_checked=disqualifiers_checked,
        overlap_notes=overlap_notes,
        pitch_guidance_ref=brief.pitch_guidance,
    )


def _evaluate_segment_1(funding, emp_count, layoffs) -> tuple[int, list[EvidenceItem], list[str]]:
    """Evaluate Segment 1: Recently Funded (Series A/B, 15-80 people, no recent layoffs)."""
    score = 0
    evidence: list[EvidenceItem] = []
    disqualifiers: list[str] = []

    # Qualifying: Funding event
    if funding.event and funding.event.lower() in ["series a", "series b"]:
        score += 3
        evidence.append(
            EvidenceItem(
                signal="funding",
                value=f"{funding.event}, ${funding.amount_usd:,}"
                if funding.amount_usd
                else funding.event,
                weight="qualifying",
            )
        )

    # Qualifying: Headcount in range
    if 15 <= emp_count <= 80:
        score += 1
        evidence.append(
            EvidenceItem(
                signal="headcount",
                value=f"{emp_count} employees (within 15-80 range)",
                weight="qualifying",
            )
        )
    elif emp_count > 200:
        disqualifiers.append("headcount_over_200")
        score = 0  # Disqualified

    # Disqualifying: Recent layoffs
    if layoffs.event:
        disqualifiers.append("layoffs_last_120d")
        score = 0  # Disqualified

    return score, evidence, disqualifiers


def _evaluate_segment_2(layoffs, emp_count, funding) -> tuple[int, list[EvidenceItem], list[str]]:
    """Evaluate Segment 2: Mid-Market Restructuring (200-2000, layoffs/restructure)."""
    score = 0
    evidence: list[EvidenceItem] = []
    disqualifiers: list[str] = []

    # Qualifying: Headcount range
    if 200 <= emp_count <= 2000:
        score += 1
        evidence.append(
            EvidenceItem(
                signal="headcount",
                value=f"{emp_count} employees (mid-market 200-2000)",
                weight="qualifying",
            )
        )

    # Qualifying: Layoff event
    if layoffs.event:
        score += 3
        evidence.append(
            EvidenceItem(
                signal="layoffs",
                value=f"Layoff: {layoffs.headcount_pct}% cut"
                if layoffs.headcount_pct
                else "Recent layoff event",
                weight="qualifying",
            )
        )

    # Disqualifying: Recent A/B with no cost signal
    if funding.event and not layoffs.event:
        disqualifiers.append("recent_funding_no_cost_signal")
        # Don't fully disqualify — could still be restructuring for other reasons

    return score, evidence, disqualifiers


def _evaluate_segment_3(leadership) -> tuple[int, list[EvidenceItem]]:
    """Evaluate Segment 3: Leadership Transition (new CTO/VP Eng in last 90 days)."""
    score = 0
    evidence: list[EvidenceItem] = []

    if leadership.change:
        score += 4  # High priority — narrow window
        evidence.append(
            EvidenceItem(
                signal="leadership_change",
                value=f"New {leadership.role}: {leadership.name}"
                if leadership.name
                else f"New {leadership.role}",
                weight="qualifying",
            )
        )

    return score, evidence


def _evaluate_segment_4(ai_maturity, hiring) -> tuple[int, list[EvidenceItem], list[str]]:
    """Evaluate Segment 4: Capability Gap (AI maturity >= 2 + specific build signal)."""
    score = 0
    evidence: list[EvidenceItem] = []
    disqualifiers: list[str] = []

    # Gate: AI maturity must be >= 2
    if ai_maturity.score < 2:
        disqualifiers.append("ai_maturity_below_2")
        return 0, evidence, disqualifiers

    score += 2
    evidence.append(
        EvidenceItem(
            signal="ai_maturity",
            value=f"Score {ai_maturity.score} (confidence: {ai_maturity.confidence.value})",
            weight="qualifying",
        )
    )

    # Check for specific build signals in hiring data
    if hiring.ai_adjacent_eng_roles and hiring.ai_adjacent_eng_roles >= 2:
        score += 1
        evidence.append(
            EvidenceItem(
                signal="ai_hiring",
                value=f"{hiring.ai_adjacent_eng_roles} AI-adjacent open roles",
                weight="supporting",
            )
        )

    return score, evidence, disqualifiers


def _calibrate_confidence(brief: HiringSignalBrief, _segment: ICPSegment) -> Confidence:
    """
    Calibrate confidence based on signal count AND signal weight.
    - High: 2+ HIGH-weight signals at high confidence
    - Medium: 1 HIGH-weight at high, or 2 MEDIUM at high
    - Low: All signals medium/low confidence
    """
    high_confidence_signals = 0

    if brief.funding.confidence == Confidence.HIGH and brief.funding.event:
        high_confidence_signals += 1
    if brief.hiring.confidence == Confidence.HIGH and brief.hiring.open_eng_roles:
        high_confidence_signals += 1
    if brief.layoffs.confidence == Confidence.HIGH and brief.layoffs.event:
        high_confidence_signals += 1
    if brief.leadership.confidence == Confidence.HIGH and brief.leadership.change:
        high_confidence_signals += 1
    if brief.ai_maturity.confidence == Confidence.HIGH:
        high_confidence_signals += 1

    if high_confidence_signals >= 2:
        return Confidence.HIGH
    if high_confidence_signals >= 1:
        return Confidence.MEDIUM
    return Confidence.LOW
