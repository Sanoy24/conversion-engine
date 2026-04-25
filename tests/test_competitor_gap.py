from __future__ import annotations

from agent.enrichment.competitor_gap import _select_top_quartile_competitors


def test_select_top_quartile_competitors_returns_bounded_count():
    scored = [({"name": f"Comp{i}"}, i % 4) for i in range(40)]
    top = _select_top_quartile_competitors(scored)

    assert 5 <= len(top) <= 10


def test_select_top_quartile_competitors_prefers_highest_scores():
    scored = [
        ({"name": "low1"}, 0),
        ({"name": "low2"}, 1),
        ({"name": "high1"}, 3),
        ({"name": "high2"}, 3),
        ({"name": "mid1"}, 2),
        ({"name": "mid2"}, 2),
        ({"name": "mid3"}, 2),
        ({"name": "high3"}, 3),
    ]
    top = _select_top_quartile_competitors(scored)
    scores = [score for _, score in top]

    assert scores == sorted(scores, reverse=True)
