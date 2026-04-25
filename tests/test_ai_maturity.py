from __future__ import annotations

from agent.enrichment.ai_maturity import (
    collect_ai_maturity_supporting_signals,
    score_ai_maturity,
)
from agent.models import Confidence, HiringSignal


def test_collect_ai_maturity_supporting_signals_reads_public_metadata():
    record = {
        "about": "CEO notes AI is central to roadmap",
        "github_url": "https://github.com/acme-ai",
        "technology_stack": "dbt, Snowflake, MLflow",
        "press_references": "Press: expanding AI automation",
    }
    signals = collect_ai_maturity_supporting_signals(record)

    assert signals["github_activity"] is not None
    assert signals["exec_commentary"] is not None
    assert signals["tech_stack_signals"] is not None
    assert signals["strategic_communications"] is not None


def test_collect_ai_maturity_supporting_signals_prefers_structured_sources():
    record = {
        "github_org_activity": {"org": "acme", "ai_repos": 2, "recent_ai_commits": 5},
        "exec_commentary": [{"quote": "Our CTO says AI is a core strategic priority."}],
        "announcements": [{"title": "AI roadmap update", "summary": "We are expanding inference platform"}],
    }
    signals = collect_ai_maturity_supporting_signals(record)

    assert signals["github_activity"]["org"] == "acme"
    assert signals["github_activity"]["ai_repos"] == 2
    assert any("core strategic priority" in text for text in (signals["exec_commentary"] or []))
    assert any("AI roadmap update" in text for text in (signals["strategic_communications"] or []))


def test_score_ai_maturity_returns_zero_for_silent_company():
    signal = score_ai_maturity(
        hiring=HiringSignal(open_eng_roles=0, ai_adjacent_eng_roles=0),
        crunchbase_record={},
        github_activity={"ai_repos": 0, "recent_ai_commits": 0},
        exec_commentary=[],
        tech_stack_signals=[],
        strategic_communications=[],
    )

    assert signal.score == 0
    assert signal.confidence == Confidence.LOW
    assert "no public signal" in (signal.language_notes or "").lower()


def test_score_ai_maturity_requires_high_signal_for_score_two_plus():
    signal = score_ai_maturity(
        hiring=HiringSignal(open_eng_roles=0, ai_adjacent_eng_roles=0),
        crunchbase_record={},
        github_activity={"ai_repos": 4, "recent_ai_commits": 8},
        exec_commentary=["AI is strategic for us"],
        tech_stack_signals=["dbt", "snowflake"],
        strategic_communications=["AI transformation priority"],
    )

    assert signal.score <= 1
