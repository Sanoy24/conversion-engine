"""Guardrail tests for domain-specific correctness fixes."""

from __future__ import annotations

from pathlib import Path

from agent.config import settings
from agent.core.email_drafter import _load_seed_file
from agent.core.icp_classifier import classify_prospect
from agent.enrichment.crunchbase import search_company
from agent.enrichment.leadership import _check_crunchbase_people
from agent.models import AIMaturitySignal, Confidence, HiringSignal
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

    assert result.segment.value == "abstain"
    assert result.confidence == Confidence.LOW


def test_leadership_change_requires_recent_start_date():
    old_record = {
        "people": [
            {
                "name": "Pat Example",
                "title": "CTO",
                "started_on": "2025-01-01",
            }
        ]
    }
    recent_record = {
        "people": [
            {
                "name": "Sam Recent",
                "title": "VP Engineering",
                "started_on": "2026-03-15",
            }
        ]
    }

    old_signal = _check_crunchbase_people(old_record)
    recent_signal = _check_crunchbase_people(recent_record)

    assert old_signal.change is False
    assert recent_signal.change is True


def test_seed_loader_prefers_real_filename_over_placeholder(monkeypatch):
    temp_dir = Path("tests") / "_seed_loader_tmp"
    temp_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "seeds_dir", str(temp_dir))
    (temp_dir / "style_guide.md").write_text("real style guide", encoding="utf-8")
    (temp_dir / "style_guide_PLACEHOLDER.md").write_text("placeholder", encoding="utf-8")

    loaded = _load_seed_file(["style_guide.md", "style_guide_PLACEHOLDER.md"])

    assert loaded == "real style guide"
