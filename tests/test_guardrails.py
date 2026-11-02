"""Guardrail tests for domain-specific correctness fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.core.email_drafter import _build_system_prompt
from agent.core.icp_classifier import classify_prospect
from agent.enrichment.crunchbase import search_company
from agent.enrichment.leadership import _check_crunchbase_people
from agent.models import AIMaturitySignal, Confidence, EmailType, HiringSignal
from agent.playbook import get_playbook
from tests.helpers import make_brief


def test_crunchbase_search_does_not_fuzzy_match_company_name(monkeypatch):
    monkeypatch.setattr(
        "agent.enrichment.crunchbase._crunchbase_cache",
        [
            {"name": "Alpha Labs", "uuid": "alpha-1", "website": "https://alpha.example"},
            {"name": "Beta Systems", "uuid": "beta-1", "website": "https://beta.example"},
        ],
    )

    assert search_company(company_name="Alpha") is None
    assert search_company(company_name="Alpha Labs")["uuid"] == "alpha-1"


def test_classifier_abstains_on_low_confidence_segment(sample_prospect):
    low_confidence_ai = AIMaturitySignal(score=2, confidence=Confidence.LOW)
    low_confidence_hiring = HiringSignal(
        open_eng_roles=2,
        ai_adjacent_eng_roles=2,
        confidence=Confidence.LOW,
    )
    brief = make_brief(
        prospect=sample_prospect,
        hiring=low_confidence_hiring,
        ai_maturity=low_confidence_ai,
    )

    result = classify_prospect(brief)

    assert result.segment == "abstain"
    assert result.confidence == Confidence.LOW


def test_leadership_change_requires_recent_start_date():
    today = datetime.now(UTC).date()
    old_record = {
        "people": [
            {
                "name": "Pat Example",
                "title": "CTO",
                "started_on": (today - timedelta(days=400)).isoformat(),
            }
        ]
    }
    recent_record = {
        "people": [
            {
                "name": "Sam Recent",
                "title": "VP Engineering",
                "started_on": (today - timedelta(days=30)).isoformat(),
            }
        ]
    }

    old_signal = _check_crunchbase_people(old_record)
    recent_signal = _check_crunchbase_people(recent_record)

    assert old_signal.change is False
    assert recent_signal.change is True


def test_drafter_prompt_uses_playbook_identity():
    """Company name, signature and banned phrases come from the playbook, not the code."""
    playbook = get_playbook()
    prompt = _build_system_prompt(playbook, EmailType.COLD, style_guide="")

    assert playbook.company.name in prompt
    assert playbook.company.signature in prompt
    for phrase in playbook.voice.banned_phrases:
        assert phrase in prompt
