"""
τ²-Bench evaluation harness.
Wraps the Sierra Research tau2-bench to produce:
- trace_log.jsonl (to Langfuse)
- score_log.json (pass@1 with 95% CI)

Run against the retail domain with the pinned dev-tier model.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
TRACES_DIR = EVAL_DIR / "traces"
SCORE_LOG_PATH = EVAL_DIR / "score_log.json"
TRACE_LOG_PATH = EVAL_DIR / "trace_log.jsonl"


def ensure_tau2_bench() -> Path:
    """Ensure tau2-bench is cloned and available."""
    tau2_path = EVAL_DIR / "tau2-bench"
    if not tau2_path.exists():
        logger.info("Cloning tau2-bench...")
        subprocess.run(
            ["git", "clone", "https://github.com/sierra-research/tau2-bench.git", str(tau2_path)],
            check=True,
        )
    return tau2_path


def run_baseline(
    model: str = "deepseek/deepseek-chat-v3-0324",
    domain: str = "retail",
    n_tasks: int = 30,
    n_trials: int = 5,
    temperature: float = 0.0,
    api_key: str | None = None,
) -> dict:
    """
    Run the τ²-Bench baseline evaluation.

    Returns a dict with:
    - pass_at_1: mean pass@1 across trials
    - ci_95: 95% confidence interval
    - cost_per_run: estimated cost per evaluation run
    - latency_p50: p50 wall-clock latency
    - latency_p95: p95 wall-clock latency
    - per_trial_scores: list of per-trial pass@1 scores
    """
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    results = {
        "model": model,
        "domain": domain,
        "n_tasks": n_tasks,
        "n_trials": n_trials,
        "temperature": temperature,
        "timestamp": datetime.utcnow().isoformat(),
        "per_trial_scores": [],
        "per_trial_latencies": [],
        "per_trial_costs": [],
    }

    for trial in range(n_trials):
        trial_id = f"trial_{trial + 1}_{uuid.uuid4().hex[:6]}"
        logger.info("Running trial %d/%d (ID: %s)", trial + 1, n_trials, trial_id)

        start = time.monotonic()

        # Run tau2-bench for this trial
        trial_result = _run_single_trial(
            model=model,
            domain=domain,
            n_tasks=n_tasks,
            temperature=temperature,
            trial_id=trial_id,
            api_key=api_key,
        )

        elapsed = time.monotonic() - start
        results["per_trial_scores"].append(trial_result["pass_at_1"])
        results["per_trial_latencies"].append(elapsed)
        results["per_trial_costs"].append(trial_result.get("cost", 0))

        # Write trace
        _write_trace(trial_id, trial_result, elapsed)

    # Compute aggregate metrics
    scores = results["per_trial_scores"]
    latencies = results["per_trial_latencies"]

    mean_score = sum(scores) / len(scores) if scores else 0
    std_dev = (
        math.sqrt(sum((s - mean_score) ** 2 for s in scores) / len(scores))
        if len(scores) > 1
        else 0
    )
    ci_95 = 1.96 * std_dev / math.sqrt(len(scores)) if len(scores) > 1 else 0

    sorted_latencies = sorted(latencies)
    p50_lat = sorted_latencies[len(sorted_latencies) // 2] if sorted_latencies else 0
    p95_idx = int(len(sorted_latencies) * 0.95)
    p95_lat = sorted_latencies[min(p95_idx, len(sorted_latencies) - 1)] if sorted_latencies else 0

    results["pass_at_1"] = round(mean_score, 4)
    results["ci_95"] = round(ci_95, 4)
    results["ci_95_range"] = [round(mean_score - ci_95, 4), round(mean_score + ci_95, 4)]
    results["cost_per_run"] = (
        round(sum(results["per_trial_costs"]) / len(results["per_trial_costs"]), 4)
        if results["per_trial_costs"]
        else 0
    )
    results["total_cost"] = round(sum(results["per_trial_costs"]), 4)
    results["latency_p50_s"] = round(p50_lat, 2)
    results["latency_p95_s"] = round(p95_lat, 2)

    # Write to score_log.json
    _update_score_log(results)

    logger.info(
        "Baseline complete: pass@1=%.4f ± %.4f (95%% CI), cost=$%.2f, p50=%.1fs",
        mean_score,
        ci_95,
        results["total_cost"],
        p50_lat,
    )

    return results


def _run_single_trial(
    model: str,
    domain: str,
    n_tasks: int,
    temperature: float,
    trial_id: str,
    api_key: str | None = None,
) -> dict:
    """
    Run a single τ²-Bench trial.
    Attempts to use the tau2-bench CLI; falls back to simulated results if not installed.
    """
    tau2_path = EVAL_DIR / "tau2-bench"

    if tau2_path.exists():
        try:
            env = os.environ.copy()
            if api_key:
                env["OPENROUTER_API_KEY"] = api_key

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tau2_bench",
                    "--domain",
                    domain,
                    "--model",
                    model,
                    "--n_tasks",
                    str(n_tasks),
                    "--temperature",
                    str(temperature),
                    "--output_dir",
                    str(TRACES_DIR / trial_id),
                ],
                capture_output=True,
                text=True,
                cwd=str(tau2_path),
                env=env,
                timeout=300,
                check=False,
            )

            if result.returncode == 0:
                # Parse results from output
                return _parse_tau2_output(result.stdout, trial_id)

        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.warning("tau2-bench CLI failed: %s. Using placeholder.", str(e))

    # Placeholder results for development (before tau2-bench is fully set up)
    logger.warning("Using placeholder tau2-bench results for trial %s", trial_id)
    return {
        "trial_id": trial_id,
        "pass_at_1": 0.0,  # Will be filled with real values
        "cost": 0.0,
        "tasks_completed": 0,
        "tasks_total": n_tasks,
        "placeholder": True,
    }


def _parse_tau2_output(output: str, trial_id: str) -> dict:
    """Parse tau2-bench CLI output."""
    result = {
        "trial_id": trial_id,
        "pass_at_1": 0.0,
        "cost": 0.0,
        "tasks_completed": 0,
    }

    for line in output.split("\n"):
        line = line.strip()
        if "pass@1" in line.lower():
            try:
                val = float(line.split(":")[-1].strip().replace("%", "")) / 100
                result["pass_at_1"] = val
            except (ValueError, IndexError):
                pass
        if "cost" in line.lower():
            try:
                val = float(line.split("$")[-1].strip())
                result["cost"] = val
            except (ValueError, IndexError):
                pass

    return result


def _write_trace(trial_id: str, trial_result: dict, elapsed: float):
    """Write a trace record to trace_log.jsonl."""
    trace = {
        "trace_id": f"tr_eval_{trial_id}",
        "event_type": "tau2_bench_trial",
        "timestamp": datetime.utcnow().isoformat(),
        "input_data": {
            "trial_id": trial_id,
        },
        "output_data": trial_result,
        "latency_ms": elapsed * 1000,
        "success": True,
    }

    TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace) + "\n")


def _update_score_log(results: dict):
    """Update score_log.json with new baseline entry."""
    score_log = []
    if SCORE_LOG_PATH.exists():
        try:
            with SCORE_LOG_PATH.open(encoding="utf-8") as f:
                score_log = json.load(f)
        except (OSError, json.JSONDecodeError):
            score_log = []

    entry = {
        "entry_type": "dev_tier_baseline",
        "timestamp": results["timestamp"],
        "model": results["model"],
        "domain": results["domain"],
        "pass_at_1": results["pass_at_1"],
        "ci_95": results["ci_95"],
        "ci_95_range": results["ci_95_range"],
        "n_trials": results["n_trials"],
        "n_tasks": results["n_tasks"],
        "cost_per_run": results["cost_per_run"],
        "total_cost": results["total_cost"],
        "latency_p50_s": results["latency_p50_s"],
        "latency_p95_s": results["latency_p95_s"],
    }

    score_log.append(entry)

    with SCORE_LOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(score_log, f, indent=2)

    logger.info("Score log updated: %s", SCORE_LOG_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = run_baseline()
    print(json.dumps(results, indent=2))
