"""
τ²-Bench evaluation harness.
Wraps the Sierra Research tau2-bench CLI to produce:
- trace_log.jsonl (per-task trace records)
- score_log.json (pass@1 with 95% CI, cost, latency)

Runs the retail domain with a pinned dev-tier model. The bundled tau2 package
lives at eval/tau2-bench and has its own uv-managed .venv, so we invoke its CLI
through that interpreter rather than the parent venv.
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
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
TAU2_DIR = EVAL_DIR / "tau2-bench"
SCORE_LOG_PATH = EVAL_DIR / "score_log.json"
TRACE_LOG_PATH = EVAL_DIR / "trace_log.jsonl"


def _tau2_python() -> str:
    """Path to the Python interpreter inside the tau2-bench .venv."""
    if os.name == "nt":
        exe = TAU2_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        exe = TAU2_DIR / ".venv" / "bin" / "python"
    if not exe.exists():
        raise RuntimeError(
            f"tau2-bench venv not found at {exe}. "
            f"Run `uv sync --directory {TAU2_DIR}` first."
        )
    return str(exe)


def ensure_tau2_bench() -> Path:
    """Ensure tau2-bench is cloned and the uv venv is synced."""
    if not TAU2_DIR.exists():
        logger.info("Cloning tau2-bench...")
        subprocess.run(
            ["git", "clone", "https://github.com/sierra-research/tau2-bench.git", str(TAU2_DIR)],
            check=True,
        )
    venv_dir = TAU2_DIR / ".venv"
    if not venv_dir.exists():
        logger.info("Syncing tau2-bench .venv via uv...")
        subprocess.run(
            ["uv", "sync", "--directory", str(TAU2_DIR)],
            check=True,
        )
    return TAU2_DIR


def run_baseline(
    model: str = "openrouter/deepseek/deepseek-chat-v3-0324",
    domain: str = "retail",
    n_tasks: int = 30,
    n_trials: int = 5,
    temperature: float = 0.0,
    entry_type: str = "dev_tier_baseline",
    api_key: str | None = None,
    max_concurrency: int = 4,
    timeout_s: int | None = 180,
    auto_resume: bool = True,
) -> dict:
    """Run the τ²-Bench baseline and write score/trace artifacts.

    Returns a dict with pass@1 (mean across trials), 95% CI, cost per run, and
    p50/p95 latency per task. Writes one aggregated entry to score_log.json and
    one trace record per task/trial to trace_log.jsonl.
    """
    ensure_tau2_bench()

    run_id = f"eval_{entry_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    logger.info(
        "Running %s: domain=%s, model=%s, n_tasks=%d, n_trials=%d, run_id=%s",
        entry_type, domain, model, n_tasks, n_trials, run_id,
    )

    start = time.monotonic()
    results_path, process_returncode = _run_tau2_cli(
        model=model,
        domain=domain,
        n_tasks=n_tasks,
        n_trials=n_trials,
        temperature=temperature,
        run_id=run_id,
        api_key=api_key,
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        auto_resume=auto_resume,
    )
    wall_clock_s = time.monotonic() - start

    raw = json.loads(results_path.read_text(encoding="utf-8"))
    aggregates = _aggregate_results(raw)
    expected_simulations = n_tasks * n_trials
    completed_simulations = aggregates["n_simulations"]
    run_status = "complete"
    if completed_simulations < expected_simulations:
        run_status = "partial"
    elif process_returncode != 0:
        run_status = "recovered_nonzero_exit"
    aggregates.update(
        entry_type=entry_type,
        timestamp=datetime.utcnow().isoformat(),
        model=model,
        domain=domain,
        n_tasks=n_tasks,
        n_trials=n_trials,
        temperature=temperature,
        wall_clock_s=round(wall_clock_s, 2),
        run_id=run_id,
        results_path=str(results_path.relative_to(EVAL_DIR)),
        expected_simulations=expected_simulations,
        completed_simulations=completed_simulations,
        process_returncode=process_returncode,
        run_status=run_status,
    )

    _write_traces(run_id, raw, entry_type)
    _update_score_log(aggregates)

    logger.info(
        "%s %s: pass@1=%.4f (95%% CI ±%.4f), cost=$%.4f, p50=%.1fs, wall=%ds",
        entry_type,
        run_status,
        aggregates["pass_at_1"],
        aggregates["ci_95"],
        aggregates["total_cost"],
        aggregates["task_latency_p50_s"],
        int(wall_clock_s),
    )
    return aggregates


def _run_tau2_cli(
    *,
    model: str,
    domain: str,
    n_tasks: int,
    n_trials: int,
    temperature: float,
    run_id: str,
    api_key: str | None = None,
    max_concurrency: int = 4,
    timeout_s: int | None = 180,
    auto_resume: bool = True,
) -> tuple[Path, int]:
    """Invoke the tau2 CLI and return the path to its results.json plus exit code."""
    env = os.environ.copy()
    if api_key:
        env["OPENROUTER_API_KEY"] = api_key
    # tau2-bench uses LiteLLM; route OpenAI-style calls through OpenRouter.
    if env.get("OPENROUTER_API_KEY") and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = env["OPENROUTER_API_KEY"]
    env.setdefault("OPENAI_API_BASE", "https://openrouter.ai/api/v1")
    # Force UTF-8 on Windows so rich/colorama doesn't crash on arrow glyphs.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    llm_args = json.dumps({"temperature": temperature})
    cmd = [
        _tau2_python(), "-m", "tau2.cli", "run",
        "--domain", domain,
        "--agent-llm", model,
        "--user-llm", model,
        "--num-trials", str(n_trials),
        "--num-tasks", str(n_tasks),
        "--agent-llm-args", llm_args,
        "--user-llm-args", llm_args,
        "--save-to", run_id,
        "--max-concurrency", str(max_concurrency),
        "--log-level", "WARNING",
    ]
    if auto_resume:
        cmd.append("--auto-resume")
    if timeout_s:
        cmd.extend(["--timeout", str(timeout_s)])

    logger.info("Launching tau2: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(TAU2_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    results_json = _resolve_results_json(run_id)
    if proc.returncode != 0:
        logger.error("tau2 stdout tail:\n%s", (proc.stdout or "")[-2000:])
        logger.error("tau2 stderr tail:\n%s", (proc.stderr or "")[-4000:])
        if results_json is not None:
            logger.warning(
                "tau2 exited with code %s but left results at %s; recovering partial run.",
                proc.returncode,
                results_json,
            )
            return results_json, proc.returncode
        raise RuntimeError(f"tau2 CLI exited with code {proc.returncode}")

    if results_json is None:
        raise RuntimeError(f"No tau2 results directory found for run_id={run_id}")
    return results_json, proc.returncode


def _resolve_results_json(run_id: str) -> Path | None:
    """Locate the results.json emitted for a run_id if it exists."""
    sims_dir = TAU2_DIR / "data" / "simulations"
    matches = sorted(sims_dir.glob(f"*{run_id}*"), key=lambda p: p.stat().st_mtime)
    if not matches:
        direct = sims_dir / run_id
        if direct.exists():
            matches = [direct]
    if not matches:
        return None

    results_json = matches[-1] / "results.json"
    if not results_json.exists():
        return None
    return results_json


def _aggregate_results(raw: dict) -> dict:
    """Compute pass@1, 95% CI, cost, and per-task latency from tau2 results.json."""
    sims = raw.get("simulations", [])
    if not sims:
        raise RuntimeError("tau2 results.json has no simulations")

    # Group per-trial pass@1 by trial index.
    rewards_by_trial: dict[int, list[float]] = defaultdict(list)
    task_latencies: list[float] = []
    total_cost = 0.0
    infra_errors = 0
    for sim in sims:
        reward_info = sim.get("reward_info") or {}
        reward = float(reward_info.get("reward", 0) or 0)
        if sim.get("termination_reason") == "infrastructure_error":
            infra_errors += 1
        trial = int(sim.get("trial", 0))
        rewards_by_trial[trial].append(reward)
        task_latencies.append(float(sim.get("duration", 0) or 0))
        total_cost += float(sim.get("agent_cost", 0) or 0) + float(sim.get("user_cost", 0) or 0)

    per_trial_pass_at_1 = [mean(rewards_by_trial[t]) for t in sorted(rewards_by_trial)]
    pass_at_1 = mean(per_trial_pass_at_1)
    if len(per_trial_pass_at_1) > 1:
        std = pstdev(per_trial_pass_at_1)
        ci_95 = 1.96 * std / math.sqrt(len(per_trial_pass_at_1))
    else:
        ci_95 = 0.0

    task_latencies_sorted = sorted(task_latencies)
    p50 = task_latencies_sorted[len(task_latencies_sorted) // 2]
    p95_idx = min(int(len(task_latencies_sorted) * 0.95), len(task_latencies_sorted) - 1)
    p95 = task_latencies_sorted[p95_idx]

    n_sims = len(sims)
    return {
        "pass_at_1": round(pass_at_1, 4),
        "ci_95": round(ci_95, 4),
        "ci_95_range": [round(pass_at_1 - ci_95, 4), round(pass_at_1 + ci_95, 4)],
        "per_trial_pass_at_1": [round(x, 4) for x in per_trial_pass_at_1],
        "n_simulations": n_sims,
        "cost_per_run": round(total_cost / n_sims, 6) if n_sims else 0,
        "total_cost": round(total_cost, 4),
        "task_latency_p50_s": round(p50, 2),
        "task_latency_p95_s": round(p95, 2),
        "infrastructure_errors": infra_errors,
    }


def _write_traces(run_id: str, raw: dict, entry_type: str) -> None:
    """Write one trace record per simulation (task × trial) to trace_log.jsonl."""
    TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
        for sim in raw.get("simulations", []):
            record = {
                "trace_id": f"tr_{run_id}_task{sim.get('task_id')}_trial{sim.get('trial')}",
                "event_type": "tau2_bench_simulation",
                "entry_type": entry_type,
                "timestamp": sim.get("timestamp") or datetime.utcnow().isoformat(),
                "run_id": run_id,
                "task_id": sim.get("task_id"),
                "trial": sim.get("trial"),
                "reward": (sim.get("reward_info") or {}).get("reward"),
                "termination_reason": sim.get("termination_reason"),
                "duration_s": sim.get("duration"),
                "agent_cost": sim.get("agent_cost"),
                "user_cost": sim.get("user_cost"),
                "num_messages": len(sim.get("messages", []) or []),
                "sim_id": sim.get("id"),
            }
            f.write(json.dumps(record) + "\n")


def _update_score_log(aggregates: dict) -> None:
    """Append an aggregate entry to score_log.json."""
    score_log: list[dict] = []
    if SCORE_LOG_PATH.exists():
        try:
            loaded = json.loads(SCORE_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                score_log = loaded
        except json.JSONDecodeError:
            score_log = []
    score_log.append(aggregates)
    SCORE_LOG_PATH.write_text(json.dumps(score_log, indent=2), encoding="utf-8")
    logger.info("Score log updated: %s (%d entries)", SCORE_LOG_PATH, len(score_log))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Two invocations required by the spec: baseline + reproduction check.
    n_tasks = int(os.environ.get("TAU2_N_TASKS", 30))
    n_trials = int(os.environ.get("TAU2_N_TRIALS", 5))
    model = os.environ.get("TAU2_MODEL", "openrouter/deepseek/deepseek-chat-v3-0324")
    max_conc = int(os.environ.get("TAU2_MAX_CONCURRENCY", 4))
    timeout_s = int(os.environ.get("TAU2_TIMEOUT_S", 180))
    run_baseline(entry_type="dev_tier_baseline", n_tasks=n_tasks, n_trials=n_trials,
                 model=model, max_concurrency=max_conc, timeout_s=timeout_s)
    run_baseline(entry_type="reproduction_check", n_tasks=n_tasks, n_trials=n_trials,
                 model=model, max_concurrency=max_conc, timeout_s=timeout_s)


if __name__ == "__main__":
    main()
