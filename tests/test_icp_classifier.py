"""Tests for ICP classifier — overlap rules, abstention, and confidence calibration."""

from __future__ import annotations

from agent.core.icp_classifier import classify_prospect
from agent.models import (
    Confidence,
    ICPSegment,
    ProspectInfo,
)
from tests.helpers import make_brief


class TestSegment1RecentlyFunded:
    """Segment 1: Recently Funded (Series A/B, 15-80 people, no layoffs)."""

    def test_classic_series_a(self, sample_funded_prospect, sample_funding, no_layoff):
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.RECENTLY_FUNDED

    def test_disqualified_by_layoffs(self, sample_funded_prospect, sample_funding, sample_layoff):
        """Funding + layoffs → Segment 2 (restructuring), NOT Segment 1."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        # Overlap rule: funding + layoffs → segment 2
        assert result.segment == ICPSegment.MID_MARKET_RESTRUCTURING

    def test_disqualified_by_headcount(self, sample_funding, no_layoff):
        """Employee count > 200 disqualifies Segment 1."""
        prospect = ProspectInfo(company="BigCorp", employee_count=500, industry="SaaS")
        brief = make_brief(prospect=prospect, funding=sample_funding, layoffs=no_layoff)
        result = classify_prospect(brief)
        assert result.segment != ICPSegment.RECENTLY_FUNDED


class TestSegment2MidMarketRestructuring:
    """Segment 2: Mid-Market Restructuring (200-2000, layoffs)."""

    def test_classic_restructuring(self, sample_prospect, sample_layoff):
        sample_prospect.employee_count = 500
        brief = make_brief(
            prospect=sample_prospect,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.MID_MARKET_RESTRUCTURING


class TestSegment3LeadershipTransition:
    """Segment 3: Leadership Transition (new CTO/VP Eng in last 90 days)."""

    def test_leadership_change_is_primary(self, sample_prospect, sample_leadership, no_layoff):
        brief = make_brief(
            prospect=sample_prospect,
            leadership=sample_leadership,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.LEADERSHIP_TRANSITION

    def test_leadership_plus_funding_keeps_leadership_primary(
        self,
        sample_funded_prospect,
        sample_funding,
        sample_leadership,
        no_layoff,
    ):
        """Leadership transition + funding → Segment 3 primary, Segment 1 secondary."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            leadership=sample_leadership,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.LEADERSHIP_TRANSITION
        assert result.secondary_segment is not None


class TestSegment4CapabilityGap:
    """Segment 4: Capability Gap (AI maturity >= 2 + build signal)."""

    def test_high_ai_maturity(self, sample_prospect, sample_hiring, high_ai_maturity):
        brief = make_brief(
            prospect=sample_prospect,
            hiring=sample_hiring,
            ai_maturity=high_ai_maturity,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.CAPABILITY_GAP

    def test_low_ai_maturity_disqualifies(self, sample_prospect, sample_hiring, low_ai_maturity):
        """AI maturity < 2 → cannot be Segment 4."""
        brief = make_brief(
            prospect=sample_prospect,
            hiring=sample_hiring,
            ai_maturity=low_ai_maturity,
        )
        result = classify_prospect(brief)
        assert result.segment != ICPSegment.CAPABILITY_GAP


class TestAbstention:
    """Abstention: no segment qualifies with sufficient confidence."""

    def test_empty_signals_abstain(self, sample_prospect):
        brief = make_brief(prospect=sample_prospect)
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.ABSTAIN
        assert result.confidence == Confidence.LOW


class TestOverlapResolution:
    """Overlap resolution rules per ICP classifier skill."""

    def test_funding_plus_layoffs_forces_segment_2(
        self, sample_funded_prospect, sample_funding, sample_layoff
    ):
        """Critical rule: funding + layoffs → Segment 2 (cost pressure overrides)."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == ICPSegment.MID_MARKET_RESTRUCTURING


class TestConfidenceCalibration:
    """Confidence should scale with signal count and weight."""

    def test_multiple_high_signals_give_high_confidence(
        self,
        sample_prospect,
        sample_leadership,
        sample_funding,
        sample_hiring,
    ):
        brief = make_brief(
            prospect=sample_prospect,
            leadership=sample_leadership,
            funding=sample_funding,
            hiring=sample_hiring,
        )
        result = classify_prospect(brief)
        assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)

    def test_single_low_signal_gives_low_confidence(self, sample_prospect):
        """No strong signals → low confidence."""
        brief = make_brief(prospect=sample_prospect)
        result = classify_prospect(brief)
        assert result.confidence == Confidence.LOW
