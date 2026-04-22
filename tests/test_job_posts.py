"""Tests for job-post snapshot guardrails."""

from __future__ import annotations

import json
from pathlib import Path

from agent.config import settings
from agent.enrichment.job_posts import _check_snapshot


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
