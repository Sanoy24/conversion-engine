"""Test helpers — reusable across test files."""

from __future__ import annotations

from agent.models import (
    AIMaturitySignal,
    Confidence,
    FundingSignal,
    HiringSignal,
    HiringSignalBrief,
    LayoffSignal,
    LeadershipSignal,
    ProspectInfo,
)


def make_brief(
    prospect: ProspectInfo,
    funding: FundingSignal | None = None,
    hiring: HiringSignal | None = None,
    layoffs: LayoffSignal | None = None,
    leadership: LeadershipSignal | None = None,
    ai_maturity: AIMaturitySignal | None = None,
) -> HiringSignalBrief:
    """Helper to assemble a signal brief with sensible defaults."""
    return HiringSignalBrief(
        prospect=prospect,
        enriched_at="2026-04-20T00:00:00",
        funding=funding or FundingSignal(),
        hiring=hiring or HiringSignal(),
        layoffs=layoffs or LayoffSignal(event=False),
        leadership=leadership or LeadershipSignal(),
        ai_maturity=ai_maturity or AIMaturitySignal(score=0, confidence=Confidence.LOW),
    )
