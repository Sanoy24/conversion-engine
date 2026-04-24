"""
SCAP — Signal-Confidence-Aware Phrasing.

A deterministic pre-prompt transform that closes the gap between evidence
strength and assertion boldness in the Tenacious email drafter.

The mechanism is documented in `eval/method.md`. It applies three rules:

1. LOW-confidence signals are stripped from the user prompt; an explicit
   ASK directive replaces each one so the LLM is told to phrase the
   corresponding topic as a question rather than a claim.
2. LOW-confidence competitor gap entries are filtered out before the
   drafter sees them. If zero gap entries survive, the "lead with the gap"
   instruction is suppressed entirely so the drafter falls back to a
   segment-language cold email.
3. MEDIUM-confidence signals remain but are flagged as SOFT-ASSERT so the
   system prompt's confidence-aware phrasing block ("looking at the public
   signal, it seems…") is reinforced.

SCAP is a pure function: it takes a `HiringSignalBrief` plus an optional
`CompetitorGapBrief` and returns a transformed copy along with a list of
applied transformations that becomes part of the draft's trace record
for evidence-graph auditing. No LLM calls, no I/O, no mutation of inputs.

Feature flag: `settings.enable_scap` (default True). Ablation variants
flip sub-flags `scap_strip_low`, `scap_filter_gap_low`, `scap_soften_medium`
so the probe runner and held-out orchestrator can measure each rule's
contribution independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agent.models import (
    AIMaturitySignal,
    CompetitorGapBrief,
    Confidence,
    FundingSignal,
    GapEntry,
    HiringSignal,
    HiringSignalBrief,
    LayoffSignal,
    LeadershipSignal,
)


@dataclass
class SCAPConfig:
    """Sub-flags that enable each SCAP rule independently.

    Ablation variants flip exactly one of these to isolate rule
    contributions. The main method runs with all three True.
    """

    strip_low_confidence: bool = True
    filter_gap_low: bool = True
    soften_medium: bool = True

    @classmethod
    def full(cls) -> "SCAPConfig":
        return cls(True, True, True)

    @classmethod
    def off(cls) -> "SCAPConfig":
        return cls(False, False, False)


@dataclass
class SCAPResult:
    """Output of `apply_scap` — a transformed brief + audit trail."""

    brief: HiringSignalBrief
    gap_brief: CompetitorGapBrief | None
    transforms: list[str]
    ask_directives: list[str]


def apply_scap(
    brief: HiringSignalBrief,
    gap_brief: CompetitorGapBrief | None = None,
    config: SCAPConfig | None = None,
) -> SCAPResult:
    """Apply the SCAP pre-prompt transform to a brief and gap brief.

    Returns a new `HiringSignalBrief` with LOW-confidence values zeroed and
    a new `CompetitorGapBrief` with LOW-confidence gaps filtered out, plus
    the list of ASK directives the drafter should inject in place of the
    stripped signals.
    """
    config = config or SCAPConfig.full()
    transforms: list[str] = []
    ask_directives: list[str] = []

    # Deep-copy so upstream callers are not mutated.
    new_brief = brief.model_copy(deep=True)

    if config.strip_low_confidence:
        _strip_low_confidence_signals(new_brief, transforms, ask_directives)

    if config.soften_medium:
        _tag_medium_confidence_signals(new_brief, transforms)

    new_gap = gap_brief
    if gap_brief is not None and config.filter_gap_low:
        new_gap = _filter_low_confidence_gaps(gap_brief, transforms)

    return SCAPResult(
        brief=new_brief,
        gap_brief=new_gap,
        transforms=transforms,
        ask_directives=ask_directives,
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _strip_low_confidence_signals(
    brief: HiringSignalBrief,
    transforms: list[str],
    asks: list[str],
) -> None:
    """Replace LOW-confidence signals with ASK directives.

    Sets the signal's identifying value to None so the drafter's user-prompt
    builder skips the assertion lines, and emits an ASK directive the drafter
    can surface in place of the missing claim.
    """
    # Funding
    if brief.funding.confidence == Confidence.LOW and brief.funding.event:
        original = brief.funding.event
        brief.funding = FundingSignal(
            event=None, amount_usd=None, confidence=Confidence.LOW, sources=brief.funding.sources,
        )
        transforms.append(f"stripped funding.event ({original}) [confidence=LOW]")
        asks.append(
            "If recent funding is plausible but unverified, ASK the prospect "
            "(e.g. 'Did your team close a round recently?') rather than asserting."
        )

    # Hiring
    if brief.hiring.confidence == Confidence.LOW and brief.hiring.open_eng_roles is not None:
        original = brief.hiring.open_eng_roles
        brief.hiring = HiringSignal(
            open_eng_roles=None,
            ai_adjacent_eng_roles=None,
            delta_60d=None,
            confidence=Confidence.LOW,
            sources=brief.hiring.sources,
        )
        transforms.append(f"stripped hiring.open_eng_roles ({original}) [confidence=LOW]")
        asks.append(
            "Hiring signal is LOW-confidence — ASK about current hiring priorities "
            "instead of quoting specific role counts."
        )

    # Layoffs — if LOW confidence, do not reference layoffs at all.
    if brief.layoffs.confidence == Confidence.LOW and brief.layoffs.event:
        brief.layoffs = LayoffSignal(
            event=False, headcount_pct=None, confidence=Confidence.LOW, sources=brief.layoffs.sources,
        )
        transforms.append("stripped layoffs.event [confidence=LOW]")
        # No ASK directive here — layoffs are never raised in cold first touch.

    # Leadership
    if brief.leadership.confidence == Confidence.LOW and brief.leadership.change:
        brief.leadership = LeadershipSignal(
            change=False,
            role=None,
            name=None,
            confidence=Confidence.LOW,
            sources=brief.leadership.sources,
        )
        transforms.append("stripped leadership.change [confidence=LOW]")
        asks.append(
            "Leadership transition inferred weakly — do not name the new role "
            "or its timing; ASK about recent team changes instead."
        )

    # AI maturity — if LOW confidence, zero the numeric score so the drafter
    # cannot quote it. Keep the language_notes intact as it already says ASK.
    if brief.ai_maturity.confidence == Confidence.LOW and brief.ai_maturity.score >= 2:
        original = brief.ai_maturity.score
        brief.ai_maturity = AIMaturitySignal(
            score=0,
            confidence=Confidence.LOW,
            inputs=brief.ai_maturity.inputs,
            language_notes=(
                (brief.ai_maturity.language_notes or "")
                + " [SCAP: numeric score withheld due to LOW confidence; ASK about AI plans.]"
            ).strip(),
        )
        transforms.append(f"zeroed ai_maturity.score ({original} -> 0) [confidence=LOW]")
        asks.append(
            "AI maturity estimate has LOW confidence — do not quote a score "
            "or imply absence of AI work; ASK about AI priorities instead."
        )


def _tag_medium_confidence_signals(
    brief: HiringSignalBrief,
    transforms: list[str],
) -> None:
    """For MEDIUM-confidence signals, append a SOFT-ASSERT hint to language_notes.

    The email_drafter system prompt already contains a CONFIDENCE-AWARE PHRASING
    block; this hint reinforces it by injecting explicit soft-assertion wording
    into the pitch_guidance.language_notes so the drafter cannot ignore it.
    """
    notes = brief.pitch_guidance.language_notes or ""
    medium_signals: list[str] = []
    if brief.funding.confidence == Confidence.MEDIUM and brief.funding.event:
        medium_signals.append("funding")
    if brief.hiring.confidence == Confidence.MEDIUM and brief.hiring.open_eng_roles is not None:
        medium_signals.append("hiring")
    if brief.ai_maturity.confidence == Confidence.MEDIUM and brief.ai_maturity.score > 0:
        medium_signals.append("ai_maturity")

    if not medium_signals:
        return

    hint = (
        "SCAP: phrase " + ", ".join(medium_signals)
        + " as SOFT-ASSERT (\"looking at the public signal, it seems...\") "
        "not as direct claim. Do not quote hiring delta_60d unless confidence is HIGH."
    )
    brief.pitch_guidance.language_notes = (notes + " " + hint).strip()
    transforms.append(f"softened MEDIUM signals: {medium_signals}")


def _filter_low_confidence_gaps(
    gap_brief: CompetitorGapBrief,
    transforms: list[str],
) -> CompetitorGapBrief:
    """Drop gap entries whose confidence is LOW."""
    kept: list[GapEntry] = []
    dropped: list[str] = []
    for gap in gap_brief.gaps:
        if gap.confidence == Confidence.LOW:
            dropped.append(gap.practice)
        else:
            kept.append(gap)
    if dropped:
        transforms.append(f"filtered {len(dropped)} LOW-confidence gap(s): {dropped}")
    new_gap = gap_brief.model_copy(deep=True)
    new_gap.gaps = kept
    return new_gap


def render_ask_directives(directives: Iterable[str]) -> str:
    """Render the accumulated ASK directives as a prompt-ready block.

    Returns an empty string when no directives fired, so the drafter's user
    prompt builder can conditionally include the block without worrying
    about trailing whitespace.
    """
    directives = list(directives)
    if not directives:
        return ""
    lines = ["## SCAP ASK DIRECTIVES (signals withheld due to low confidence)"]
    for i, d in enumerate(directives, start=1):
        lines.append(f"{i}. {d}")
    lines.append(
        "Do not restore the withheld information from prior knowledge — "
        "the evidence is insufficient. Use ASK form."
    )
    return "\n".join(lines)
