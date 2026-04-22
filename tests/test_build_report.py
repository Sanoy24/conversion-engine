"""Tests for honest baseline/report rendering."""

from __future__ import annotations

from scripts.build_report import build_baseline_md, build_interim_report


def _make_score_entry(
    entry_type: str,
    *,
    run_status: str = "complete",
    completed_simulations: int = 150,
    expected_simulations: int = 150,
    pass_at_1: float = 0.38,
    ci_95: float = 0.04,
) -> dict:
    return {
        "entry_type": entry_type,
        "run_status": run_status,
        "completed_simulations": completed_simulations,
        "expected_simulations": expected_simulations,
        "n_simulations": completed_simulations,
        "n_tasks": 30,
        "n_trials": 5,
        "model": "openrouter/deepseek/deepseek-chat-v3-0324",
        "pass_at_1": pass_at_1,
        "ci_95": ci_95,
        "ci_95_range": [round(pass_at_1 - ci_95, 4), round(pass_at_1 + ci_95, 4)],
        "per_trial_pass_at_1": [0.34, 0.36, 0.39, 0.4, 0.41],
        "cost_per_run": 0.0123,
        "total_cost": 1.845,
        "task_latency_p50_s": 18.2,
        "task_latency_p95_s": 29.7,
        "wall_clock_s": 812.0,
        "run_id": "eval_dev_tier_baseline_123456",
    }


def test_build_baseline_md_marks_partial_runs_as_incomplete():
    score_log = [
        _make_score_entry(
            "dev_tier_baseline",
            run_status="partial",
            completed_simulations=63,
            expected_simulations=150,
        )
    ]

    baseline_md = build_baseline_md(score_log)

    assert "Act I status: incomplete." in baseline_md
    assert "63/150 simulations finished" in baseline_md
    assert "not sufficient for submission" in baseline_md


def test_build_interim_report_flags_incomplete_interim_submission():
    score_log = [
        _make_score_entry(
            "dev_tier_baseline",
            run_status="partial",
            completed_simulations=63,
            expected_simulations=150,
        )
    ]
    e2e_summary = {
        "n_total": 25,
        "n_success": 25,
        "kill_switch_enabled": True,
        "pipeline_latency_ms": {"p50": 19000, "p95": 30000, "mean": 21000},
        "cost_usd": {"total": 0.0352, "per_prospect": {"mean": 0.001409}},
        "segment_distribution": {"abstain": 12},
        "gap_brief_coverage": 25,
    }

    report_md = build_interim_report(score_log, e2e_summary, None, None)

    assert "Interim requirement status: still incomplete." in report_md
    assert "smoke-test metrics, not the required real email/SMS interactions" in report_md
    assert "submission blocker and must be replaced" in report_md


def test_build_interim_report_can_mark_complete_when_artifacts_and_live_outbound_exist():
    score_log = [
        _make_score_entry("dev_tier_baseline", pass_at_1=0.38),
        _make_score_entry("reproduction_check", pass_at_1=0.37),
    ]
    e2e_summary = {
        "n_total": 20,
        "n_success": 20,
        "kill_switch_enabled": False,
        "pipeline_latency_ms": {"p50": 18000, "p95": 26000, "mean": 20000},
        "cost_usd": {"total": 0.04, "per_prospect": {"mean": 0.002}},
        "segment_distribution": {"segment_1_recently_funded": 9},
        "gap_brief_coverage": 20,
    }

    report_md = build_interim_report(score_log, e2e_summary, None, None)

    assert "Interim requirement status: Act I artifacts are complete and Act II has live outbound evidence." in report_md
    assert "Current latency and cost numbers come from live outbound-enabled runs." in report_md
