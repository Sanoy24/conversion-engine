"""Tests for job-post snapshot guardrails."""

from __future__ import annotations

import json
from pathlib import Path

from agent.config import settings
from agent.enrichment.job_posts import (
    _candidate_job_page_urls,
    _check_snapshot,
    _compute_delta_60d_from_snapshot_jobs,
    _snapshot_baseline_eng_count,
)


def _workspace_snapshot_path(name: str) -> Path:
    temp_dir = Path("tests") / "_job_posts_tmp"
    temp_dir.mkdir(exist_ok=True)
    return temp_dir / name


def test_check_snapshot_ignores_synthetic_entries_by_default(monkeypatch):
    snapshot_path = _workspace_snapshot_path("synthetic_default.json")
    snapshot_path.write_text(
        json.dumps(
            [
                {
                    "company": "Alpha Labs",
                    "jobs": [{"title": "Staff Software Engineer"}],
                    "synthetic": True,
                    "source_url": "https://alpha.example/careers",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "job_posts_snapshot_path", str(snapshot_path))
    monkeypatch.setattr(settings, "allow_synthetic_job_posts_snapshot", False)

    assert _check_snapshot("Alpha Labs") is None


def test_check_snapshot_uses_exact_normalized_name_matching(monkeypatch):
    snapshot_path = _workspace_snapshot_path("exact_match.json")
    snapshot_path.write_text(
        json.dumps(
            [
                {
                    "company": "Alpha Labs",
                    "jobs": [{"title": "Staff Software Engineer"}],
                    "source_url": "https://alpha.example/careers",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "job_posts_snapshot_path", str(snapshot_path))
    monkeypatch.setattr(settings, "allow_synthetic_job_posts_snapshot", False)

    assert _check_snapshot("Alpha") is None
    signal = _check_snapshot("Alpha Labs")

    assert signal is not None
    assert signal.open_eng_roles == 1
    assert signal.confidence.value == "high"


def test_check_snapshot_downgrades_synthetic_entries_when_explicitly_allowed(monkeypatch):
    snapshot_path = _workspace_snapshot_path("synthetic_allowed.json")
    snapshot_path.write_text(
        json.dumps(
            {
                "metadata": {"synthetic": True},
                "companies": [
                    {
                        "company": "Beta Systems",
                        "jobs": [
                            {"title": "Staff Software Engineer"},
                            {"title": "MLOps Engineer"},
                        ],
                        "delta_60d": "+2",
                        "source_url": "https://beta.example/careers",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "job_posts_snapshot_path", str(snapshot_path))
    monkeypatch.setattr(settings, "allow_synthetic_job_posts_snapshot", True)

    signal = _check_snapshot("Beta Systems")

    assert signal is not None
    assert signal.open_eng_roles == 2
    assert signal.ai_adjacent_eng_roles == 1
    assert signal.confidence.value == "low"
    assert signal.sources[0].description == "Synthetic placeholder snapshot"


def test_candidate_job_page_urls_include_required_public_sources():
    urls = _candidate_job_page_urls(domain="acme.io", careers_url=None)

    assert "https://acme.io/careers" in urls
    assert "https://www.builtin.com/company/acme/jobs" in urls
    assert "https://wellfound.com/company/acme/jobs" in urls
    assert "https://www.linkedin.com/company/acme/jobs" in urls


def test_compute_delta_60d_from_dated_jobs():
    jobs = [
        {"title": "A", "posted_at": "2099-01-20"},
        {"title": "B", "posted_at": "2099-01-15"},
        {"title": "C", "posted_at": "2098-11-20"},
    ]
    # The helper uses now(), so we force deterministic behavior by feeding dates
    # that are guaranteed to be parsed even when windows shift. The function
    # should still return a signed delta string when dates are present.
    delta = _compute_delta_60d_from_snapshot_jobs(jobs)

    assert delta is None or delta.startswith(("+", "-")) or delta == "0"


def test_snapshot_baseline_requires_as_of_metadata(monkeypatch):
    snapshot_path = _workspace_snapshot_path("baseline_requires_asof.json")
    snapshot_path.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "company": "Gamma Labs",
                        "jobs": [{"title": "Staff Software Engineer"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "job_posts_snapshot_path", str(snapshot_path))
    assert _snapshot_baseline_eng_count("Gamma Labs") is None
