"""Tests for ICP classifier — overlap rules, abstention, and confidence calibration."""

from __future__ import annotations

from agent.core.icp_classifier import classify_prospect
from agent.models import (
    Confidence,
    ProspectInfo,
)
from tests.helpers import make_brief


class TestSegment1RecentlyFunded:
    """Recently funded (Series A/B, 15-80 people, no layoffs)."""

    def test_classic_series_a(self, sample_funded_prospect, sample_funding, no_layoff):
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == "recently_funded"

    def test_disqualified_by_layoffs(self, sample_funded_prospect, sample_funding, sample_layoff):
        """Funding + layoffs → restructuring, NOT recently funded."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        # Overlap rule: layoffs exclude recently_funded
        assert result.segment == "restructuring"

    def test_disqualified_by_headcount(self, sample_funding, no_layoff):
        """Employee count > 200 disqualifies recently_funded."""
        prospect = ProspectInfo(company="BigCorp", employee_count=500, industry="SaaS")
        brief = make_brief(prospect=prospect, funding=sample_funding, layoffs=no_layoff)
        result = classify_prospect(brief)
        assert result.segment != "recently_funded"


class TestSegment2MidMarketRestructuring:
    """Restructuring (200-2000, layoffs)."""

    def test_classic_restructuring(self, sample_prospect, sample_layoff):
        sample_prospect.employee_count = 500
        brief = make_brief(
            prospect=sample_prospect,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == "restructuring"


class TestSegment3LeadershipTransition:
    """Leadership transition (new CTO/VP Eng in last 90 days)."""

    def test_leadership_change_is_primary(self, sample_prospect, sample_leadership, no_layoff):
        brief = make_brief(
            prospect=sample_prospect,
            leadership=sample_leadership,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == "leadership_transition"

    def test_leadership_plus_funding_keeps_leadership_primary(
        self,
        sample_funded_prospect,
        sample_funding,
        sample_leadership,
        no_layoff,
    ):
        """Leadership transition + funding → leadership primary, funding secondary (tie goes to playbook order)."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            leadership=sample_leadership,
            layoffs=no_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == "leadership_transition"
        assert result.secondary_segment is not None


class TestSegment4CapabilityGap:
    """Capability gap (AI maturity >= 2 + build signal)."""

    def test_high_ai_maturity(self, sample_prospect, sample_hiring, high_ai_maturity):
        brief = make_brief(
            prospect=sample_prospect,
            hiring=sample_hiring,
            ai_maturity=high_ai_maturity,
        )
        result = classify_prospect(brief)
        assert result.segment == "capability_gap"

    def test_low_ai_maturity_disqualifies(self, sample_prospect, sample_hiring, low_ai_maturity):
        """AI maturity < 2 → cannot be capability_gap."""
        brief = make_brief(
            prospect=sample_prospect,
            hiring=sample_hiring,
            ai_maturity=low_ai_maturity,
        )
        result = classify_prospect(brief)
        assert result.segment != "capability_gap"


class TestAbstention:
    """Abstention: no segment qualifies with sufficient confidence."""

    def test_empty_signals_abstain(self, sample_prospect):
        brief = make_brief(prospect=sample_prospect)
        result = classify_prospect(brief)
        assert result.segment == "abstain"
        assert result.confidence == Confidence.LOW


class TestOverlapResolution:
    """Overlap resolution rules per ICP classifier skill."""

    def test_funding_plus_layoffs_forces_restructuring(
        self, sample_funded_prospect, sample_funding, sample_layoff
    ):
        """Critical rule: funding + layoffs → restructuring (cost pressure overrides)."""
        brief = make_brief(
            prospect=sample_funded_prospect,
            funding=sample_funding,
            layoffs=sample_layoff,
        )
        result = classify_prospect(brief)
        assert result.segment == "restructuring"


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
