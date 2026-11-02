"""
ICP Classifier with Abstention.

Scores a hiring_signal_brief against the segments declared in the active
playbook and returns the best match — or abstains.

Each segment is a list of weighted criteria plus optional exclusions:
  - any matching `exclude` condition disqualifies the segment
  - any unmatched `required` criterion disqualifies the segment
  - otherwise the score is the sum of the weights of matched criteria

Weak evidence may rule a segment out but never qualify it: an exclusion
matches on a signal at any confidence, while a criterion that asserts a
signal is present (a funding round, a layoff, a leadership change, AI
maturity, open roles) only matches when that signal is at least MEDIUM
confidence. Otherwise a low-confidence guess would pick the pitch, and the
pitch would restate the guess the drafter was told not to make.

The highest score wins; ties go to the segment listed first in the playbook.
If no segment scores above zero, or confidence in the underlying signals is
low, the classifier abstains so the drafter sends a generic exploratory email
instead of a segment-specific pitch.
"""

from __future__ import annotations

import logging

from agent.models import (
    Confidence,
    EvidenceItem,
    HiringSignalBrief,
    ICPClassification,
)
from agent.playbook import ABSTAIN, Condition, Playbook, Segment, get_playbook

logger = logging.getLogger(__name__)

# EvidenceItem.signal name reported for each condition kind
_SIGNAL_NAMES = {
    "funding_rounds": "funding",
    "employees": "headcount",
    "layoffs": "layoffs",
    "leadership_change": "leadership_change",
    "ai_maturity_min": "ai_maturity",
    "ai_roles_min": "ai_hiring",
    "open_roles_min": "hiring",
}


def classify_prospect(
    brief: HiringSignalBrief, playbook: Playbook | None = None
) -> ICPClassification:
    """Classify a prospect into one of the playbook's segments, or abstain."""
    playbook = playbook or get_playbook()
    p = brief.prospect

    scored: list[tuple[Segment, int, list[EvidenceItem]]] = []
    disqualifiers_checked: list[str] = []
    for segment in playbook.segments:
        score, evidence, disqualifiers = _score_segment(segment, brief)
        scored.append((segment, score, evidence))
        disqualifiers_checked.extend(disqualifiers)

    # Stable sort keeps playbook order as the tie-breaker
    ranked = sorted(scored, key=lambda item: item[1], reverse=True)
    best, best_score, best_evidence = ranked[0] if ranked else (None, 0, [])

    if best is None or best_score == 0:
        return ICPClassification(
            prospect=p,
            segment=ABSTAIN,
            confidence=Confidence.LOW,
            disqualifiers_checked=disqualifiers_checked,
            overlap_notes="No segment met minimum qualifying signals.",
        )

    confidence = _calibrate_confidence(brief)
    if confidence == Confidence.LOW:
        return ICPClassification(
            prospect=p,
            segment=ABSTAIN,
            confidence=Confidence.LOW,
            evidence=best_evidence,
            disqualifiers_checked=disqualifiers_checked,
            overlap_notes=(
                f"Best candidate was {best.id}, but confidence stayed below the "
                "abstention threshold."
            ),
            pitch_guidance_ref=brief.pitch_guidance,
        )

    secondary = next((seg for seg, score, _ in ranked[1:] if score > 0), None)
    overlap_notes = (
        f"{best.label} is primary; {secondary.label} also qualifies." if secondary else None
    )

    return ICPClassification(
        prospect=p,
        segment=best.id,
        segment_label=best.label,
        secondary_segment=secondary.id if secondary else None,
        confidence=confidence,
        evidence=best_evidence,
        disqualifiers_checked=disqualifiers_checked,
        overlap_notes=overlap_notes,
        pitch_guidance_ref=brief.pitch_guidance,
    )


def _score_segment(
    segment: Segment, brief: HiringSignalBrief
) -> tuple[int, list[EvidenceItem], list[str]]:
    for condition in segment.exclude:
        matched, _ = _evaluate(condition, brief)
        if matched:
            return 0, [], [f"{segment.id}: excluded by {condition.kind}"]

    score = 0
    evidence: list[EvidenceItem] = []
    for criterion in segment.criteria:
        matched, value = _evaluate(criterion.when, brief)
        matched = matched and _confident_enough(criterion.when, brief)
        if not matched:
            if criterion.required:
                return 0, [], [f"{segment.id}: missing required {criterion.when.kind}"]
            continue
        score += criterion.weight
        evidence.append(
            EvidenceItem(
                signal=_SIGNAL_NAMES[criterion.when.kind],
                value=value,
                weight="qualifying" if criterion.required or criterion.weight > 1 else "supporting",
            )
        )
    return score, evidence, []


def _evaluate(condition: Condition, brief: HiringSignalBrief) -> tuple[bool, str]:
    """Return (matched, human-readable evidence) for one condition."""
    funding, hiring, layoffs = brief.funding, brief.hiring, brief.layoffs
    leadership, ai = brief.leadership, brief.ai_maturity

    match condition.kind:
        case "funding_rounds":
            rounds = {r.lower() for r in condition.funding_rounds or []}
            matched = bool(funding.event) and funding.event.lower() in rounds
            value = (
                f"{funding.event}, ${funding.amount_usd:,}"
                if funding.amount_usd
                else str(funding.event)
            )
            return matched, value
        case "employees":
            bounds = condition.employees or {}
            emp = brief.prospect.employee_count or 0
            low, high = bounds.get("min", 0), bounds.get("max")
            matched = emp >= low and (high is None or emp <= high)
            band = f"{low}-{high}" if high is not None else f"{low}+"
            return matched, f"{emp} employees (band {band})"
        case "layoffs":
            if layoffs.event:
                value = (
                    f"Layoff: {layoffs.headcount_pct}% cut"
                    if layoffs.headcount_pct
                    else "Recent layoff event"
                )
            else:
                value = "No recent layoffs"
            return layoffs.event == condition.layoffs, value
        case "leadership_change":
            if leadership.change:
                value = (
                    f"New {leadership.role}: {leadership.name}"
                    if leadership.name
                    else f"New {leadership.role}"
                )
            else:
                value = "No leadership change"
            return leadership.change == condition.leadership_change, value
        case "ai_maturity_min":
            return (
                ai.score >= (condition.ai_maturity_min or 0),
                f"Score {ai.score} (confidence: {ai.confidence.value})",
            )
        case "ai_roles_min":
            roles = hiring.ai_adjacent_eng_roles or 0
            return roles >= (condition.ai_roles_min or 0), f"{roles} AI-adjacent open roles"
        case "open_roles_min":
            roles = hiring.open_eng_roles or 0
            return roles >= (condition.open_roles_min or 0), f"{roles} open engineering roles"
    raise ValueError(f"Unknown condition kind: {condition.kind}")


def _confident_enough(condition: Condition, brief: HiringSignalBrief) -> bool:
    """Whether the signal behind a positive criterion is solid enough to qualify a segment."""
    signal_confidence = {
        "funding_rounds": brief.funding.confidence,
        "layoffs": brief.layoffs.confidence if condition.layoffs else None,
        "leadership_change": brief.leadership.confidence if condition.leadership_change else None,
        "ai_maturity_min": brief.ai_maturity.confidence,
        "ai_roles_min": brief.hiring.confidence,
        "open_roles_min": brief.hiring.confidence,
    }.get(condition.kind)
    # Headcount, and "no layoff" / "no leadership change" criteria, carry no confidence.
    return signal_confidence is None or signal_confidence != Confidence.LOW


def _calibrate_confidence(brief: HiringSignalBrief) -> Confidence:
    """
    Calibrate confidence based on how many signals are present at high confidence.
    - High: 2+ signals at high confidence
    - Medium: exactly 1
    - Low: none
    """
    high_confidence_signals = sum(
        [
            brief.funding.confidence == Confidence.HIGH and bool(brief.funding.event),
            brief.hiring.confidence == Confidence.HIGH and bool(brief.hiring.open_eng_roles),
            brief.layoffs.confidence == Confidence.HIGH and brief.layoffs.event,
            brief.leadership.confidence == Confidence.HIGH and brief.leadership.change,
            brief.ai_maturity.confidence == Confidence.HIGH,
        ]
    )

    if high_confidence_signals >= 2:
        return Confidence.HIGH
    if high_confidence_signals >= 1:
        return Confidence.MEDIUM
    return Confidence.LOW
