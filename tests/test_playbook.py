"""Tests for playbook loading, validation, and playbook-driven classification."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.core.icp_classifier import classify_prospect
from agent.playbook import Condition, Playbook, Segment, load_playbook
from tests.helpers import make_brief


def _minimal_playbook(**overrides) -> dict:
    data = {
        "company": {"name": "Test Co", "offering": "We test things."},
        "segments": [
            {
                "id": "big_companies",
                "label": "Big companies",
                "criteria": [{"when": {"employees": {"min": 100}}, "weight": 1}],
            }
        ],
    }
    data.update(overrides)
    return data


def test_example_playbook_loads():
    playbook = load_playbook("playbooks/example")
    assert playbook.company.name
    assert [s.id for s in playbook.segments] == [
        "leadership_transition",
        "restructuring",
        "recently_funded",
        "capability_gap",
    ]
    assert playbook.read_text(playbook.voice.style_guide)
    assert playbook.load_capacity()["total_available"] > 0


def test_talent_example_playbook_loads():
    playbook = load_playbook("playbooks/talent-example")
    assert [s.id for s in playbook.segments] == [
        "actively_hiring",
        "funded_and_hiring",
        "ai_hiring",
        "new_technical_leader",
    ]
    assert playbook.read_text(playbook.voice.style_guide)
    assert playbook.read_text(playbook.knowledge[0].path)
    assert playbook.load_capacity() is None
    assert all(
        any(condition.layoffs for condition in segment.exclude) for segment in playbook.segments
    )


def test_missing_playbook_dir_gives_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="PLAYBOOK_DIR"):
        load_playbook(tmp_path)


def test_condition_must_set_exactly_one_field():
    with pytest.raises(ValidationError):
        Condition()
    with pytest.raises(ValidationError):
        Condition(layoffs=True, leadership_change=True)


def test_abstain_is_a_reserved_segment_id():
    with pytest.raises(ValidationError):
        Segment(id="abstain", label="x", criteria=[])


def test_duplicate_segment_ids_rejected():
    segment = _minimal_playbook()["segments"][0]
    with pytest.raises(ValidationError, match="Duplicate"):
        Playbook.model_validate(_minimal_playbook(segments=[segment, segment]))


def test_custom_playbook_drives_classification(sample_prospect, sample_hiring, high_ai_maturity):
    """A different playbook yields a different segment for the same brief."""
    playbook = Playbook.model_validate(_minimal_playbook())
    brief = make_brief(prospect=sample_prospect, hiring=sample_hiring, ai_maturity=high_ai_maturity)

    result = classify_prospect(brief, playbook=playbook)

    assert result.segment == "big_companies"
    assert result.segment_label == "Big companies"


def test_required_criterion_gates_segment(sample_prospect, sample_hiring, high_ai_maturity):
    playbook = Playbook.model_validate(
        _minimal_playbook(
            segments=[
                {
                    "id": "needs_layoffs",
                    "label": "Needs layoffs",
                    "criteria": [
                        {"when": {"layoffs": True}, "weight": 1, "required": True},
                        {"when": {"employees": {"min": 100}}, "weight": 5},
                    ],
                }
            ]
        )
    )
    brief = make_brief(prospect=sample_prospect, hiring=sample_hiring, ai_maturity=high_ai_maturity)

    result = classify_prospect(brief, playbook=playbook)

    assert result.segment == "abstain"
    assert "needs_layoffs: missing required layoffs" in result.disqualifiers_checked
