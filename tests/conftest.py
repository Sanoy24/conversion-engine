"""Shared test fixtures for the conversion engine."""

from __future__ import annotations

import pytest

from agent.models import (
    AIMaturitySignal,
    Confidence,
    FundingSignal,
    HiringSignal,
    LayoffSignal,
    LeadershipSignal,
    ProspectInfo,
)


@pytest.fixture
def sample_prospect() -> ProspectInfo:
    return ProspectInfo(
        company="Acme Corp",
        domain="acme.io",
        crunchbase_id="acme-corp-uuid",
        contact_name="Jane Doe",
        contact_email="jane@acme.io",
        contact_title="CTO",
        hq_location="San Francisco, US",
        employee_count=150,
        industry="SaaS, Enterprise Software",
    )


@pytest.fixture
def sample_funded_prospect() -> ProspectInfo:
    return ProspectInfo(
        company="FreshFund Inc",
        domain="freshfund.io",
        crunchbase_id="freshfund-uuid",
        contact_name="Alice Chen",
        contact_email="alice@freshfund.io",
        contact_title="CEO",
        employee_count=45,
        industry="FinTech",
    )


@pytest.fixture
def sample_funding() -> FundingSignal:
    return FundingSignal(
        event="Series A",
        amount_usd=10_000_000,
        closed_at="2026-03-01",
        confidence=Confidence.HIGH,
    )


@pytest.fixture
def sample_hiring() -> HiringSignal:
    return HiringSignal(
        open_eng_roles=12,
        ai_adjacent_eng_roles=3,
        delta_60d="+4",
        confidence=Confidence.HIGH,
    )


@pytest.fixture
def sample_layoff() -> LayoffSignal:
    return LayoffSignal(
        event=True,
        headcount_pct=15.0,
        closed_at="2026-02-15",
        confidence=Confidence.HIGH,
    )


@pytest.fixture
def no_layoff() -> LayoffSignal:
    return LayoffSignal(event=False, confidence=Confidence.HIGH)


@pytest.fixture
def sample_leadership() -> LeadershipSignal:
    return LeadershipSignal(
        change=True,
        role="CTO",
        name="Bob Smith",
        appointed_at="2026-03-10",
        confidence=Confidence.HIGH,
    )


@pytest.fixture
def no_leadership() -> LeadershipSignal:
    return LeadershipSignal(change=False, confidence=Confidence.LOW)


@pytest.fixture
def high_ai_maturity() -> AIMaturitySignal:
    return AIMaturitySignal(score=3, confidence=Confidence.HIGH)


@pytest.fixture
def low_ai_maturity() -> AIMaturitySignal:
    return AIMaturitySignal(score=0, confidence=Confidence.HIGH)
