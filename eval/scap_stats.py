"""
Statistical analysis for the Act IV SCAP held-out evaluation.

Reads the artifacts produced by `eval/run_heldout.py`:
  - eval/ablation_results.json — per-condition aggregates from the harness
  - eval/tau2-bench/data/simulations/<run_id>/results.json — raw sim records

For each condition we compute the per-task mean reward (averaged across
the trials of that task), then run a paired bootstrap over per-task deltas
between condition pairs. Output:

  - Delta A: scap_full vs baseline    (primary endpoint, p < 0.05 required)
  - Delta B: scap_full vs gepa_fewshot (Delta B — informational)
  - Delta C: scap_full vs published τ²-Bench reference (single-point report)
  - Per-ablation deltas (scap_ablation_a/b/c vs baseline) — diagnostic

Also extracts and concatenates the sealed-slice traces into
`eval/held_out_traces.jsonl`, one JSON line per (condition, task, trial,
sim_id) for evidence-graph audit.

Usage:
    python -m eval.scap_stats --bootstrap-iters 10000 --seed 4242

Outputs:
    eval/ablation_results.json (overwritten with stats block appended)
    eval/held_out_traces.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

logger = logging.getLogger("scap_stats")

EVAL_DIR = Path(__file__).parent
ABLATION_RESULTS_PATH = EVAL_DIR / "ablation_results.json"
HELDOUT_TRACES_PATH = EVAL_DIR / "held_out_traces.jsonl"
TAU2_SIMS_DIR = EVAL_DIR / "tau2-bench" / "data" / "simulations"

# Published τ²-Bench retail reference. Source: τ²-Bench leaderboard, Feb 2026
# (challenge brief line 119). Voice ceiling cited there is ~42% for voice
# agents; the text-mode retail pass@1 reference for Sonnet-class models is
# ~0.74. We treat this as a single-point published reference for Delta C.
PUBLISHED_TAU2_RETAIL_PASS_AT_1 = 0.74


# ── Data structures ───────────────────────────────────────────────────


@dataclass
class CondTaskRewards:
    """Per-task mean rewards for one condition, ordered by task_id."""

    condition: str
    task_ids: list[str]
    mean_reward_per_task: list[float]
    n_trials_per_task: list[int]
    raw_sims: list[dict]


# ── Loading ───────────────────────────────────────────────────────────


def _load_ablation_results() -> dict:
    if not ABLATION_RESULTS_PATH.exists():
        raise FileNotFoundError(f"{ABLATION_RESULTS_PATH} not found — run run_heldout.py first")
    return json.loads(ABLATION_RESULTS_PATH.read_text(encoding="utf-8"))


def _load_condition_sims(record: dict) -> list[dict]:
    """Locate the tau2 results.json for a condition entry and return its sims."""
    relpath = record.get("results_path")
    if not relpath:
        raise RuntimeError(f"condition {record.get('condition')} has no results_path")
    results_json = EVAL_DIR / relpath
    if not results_json.exists():
        raise FileNotFoundError(f"results.json missing for condition {record['condition']}: {results_json}")
    raw = json.loads(results_json.read_text(encoding="utf-8"))
    return raw.get("simulations", [])


def _per_task_mean_rewards(sims: list[dict]) -> tuple[list[str], list[float], list[int]]:
    """Group sims by task_id, average rewards across trials per task."""
    by_task: dict[str, list[float]] = {}
    for s in sims:
        tid = str(s.get("task_id"))
        reward_info = s.get("reward_info") or {}
        r = float(reward_info.get("reward", 0) or 0)
        by_task.setdefault(tid, []).append(r)
    task_ids = sorted(by_task.keys(), key=lambda x: (len(x), x))
    means = [mean(by_task[t]) for t in task_ids]
    counts = [len(by_task[t]) for t in task_ids]
    return task_ids, means, counts


# ── Bootstrap ─────────────────────────────────────────────────────────


def paired_bootstrap_delta(
    a: list[float],
    b: list[float],
    iters: int = 10_000,
    seed: int = 4242,
) -> dict:
    """Paired-bootstrap delta of `a − b` over equal-length per-task vectors.

    Returns:
        mean_delta: observed `mean(a − b)`
        ci_95_low / ci_95_high: percentile 95% CI on the bootstrap distribution
        p_one_sided: P(D <= 0) under H0 — fraction of bootstrap means at or
                     below 0 (one-sided test that `a` is greater than `b`).
        n: number of paired observations
    """
    if len(a) != len(b):
        raise ValueError(f"a and b must be same length; got {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return {"mean_delta": 0.0, "ci_95_low": 0.0, "ci_95_high": 0.0,
                "p_one_sided": 1.0, "n": 0}

    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    observed = mean(diffs)

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(iters):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boot_means.append(mean(sample))
    boot_means.sort()
    lo_idx = max(0, int(0.025 * iters) - 1)
    hi_idx = min(iters - 1, int(0.975 * iters))
    ci_lo = boot_means[lo_idx]
    ci_hi = boot_means[hi_idx]

    # One-sided p: how often the bootstrap mean was at or below 0
    le_zero = sum(1 for m in boot_means if m <= 0)
    p_one_sided = le_zero / iters

    return {
        "mean_delta": round(observed, 6),
        "ci_95_low": round(ci_lo, 6),
        "ci_95_high": round(ci_hi, 6),
        "p_one_sided": round(p_one_sided, 6),
        "n": n,
    }


def two_proportion_z(pa: float, pb: float, n: int) -> dict:
    """Approximate two-proportion z-test for unpaired pass-rate comparison.

    Used as a sanity-check companion to the paired bootstrap for the Delta A
    headline. Assumes both conditions sampled the same N independent sims.
    """
    if n <= 0 or (pa == 0 and pb == 0):
        return {"z": 0.0, "p_one_sided": 1.0}
    p_pool = (pa + pb) / 2
    se = math.sqrt(2 * p_pool * (1 - p_pool) / n) if 0 < p_pool < 1 else 1e-9
    z = (pa - pb) / se
    # one-sided p ~ 1 - Phi(z) using erf
    p = 0.5 * (1 - math.erf(z / math.sqrt(2)))
    return {"z": round(z, 4), "p_one_sided": round(p, 6)}


# ── Trace aggregation ─────────────────────────────────────────────────


def _emit_held_out_traces(by_cond: dict[str, CondTaskRewards]) -> int:
    """Concatenate per-condition sims into eval/held_out_traces.jsonl."""
    n_written = 0
    HELDOUT_TRACES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HELDOUT_TRACES_PATH.open("w", encoding="utf-8") as f:
        for cond, ctr in by_cond.items():
            for s in ctr.raw_sims:
                rec = {
                    "trace_id": f"heldout_{cond}_task{s.get('task_id')}_trial{s.get('trial')}",
                    "condition": cond,
                    "task_id": str(s.get("task_id")),
                    "trial": s.get("trial"),
                    "reward": (s.get("reward_info") or {}).get("reward"),
                    "termination_reason": s.get("termination_reason"),
                    "duration_s": s.get("duration"),
                    "agent_cost": s.get("agent_cost"),
                    "user_cost": s.get("user_cost"),
                    "num_messages": len(s.get("messages", []) or []),
                    "sim_id": s.get("id"),
                }
                f.write(json.dumps(rec) + "\n")
                n_written += 1
    return n_written


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-iters", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    payload = _load_ablation_results()
    conditions = payload.get("conditions", [])
    if not conditions:
        raise SystemExit("No conditions found in ablation_results.json")

    # Build per-condition per-task reward vectors
    by_cond: dict[str, CondTaskRewards] = {}
    for rec in conditions:
        cond = rec["condition"]
        sims = _load_condition_sims(rec)
        task_ids, means, counts = _per_task_mean_rewards(sims)
        by_cond[cond] = CondTaskRewards(
            condition=cond,
            task_ids=task_ids,
            mean_reward_per_task=means,
            n_trials_per_task=counts,
            raw_sims=sims,
        )
        logger.info(
            "Condition %s: %d sims across %d tasks (mean reward %.4f, trials/task=%s)",
            cond, len(sims), len(task_ids),
            mean(means) if means else 0.0, sorted(set(counts)),
        )

    # Sanity: every pairing must use the same task ordering. We enforce that
    # the union of task_ids across conditions is the held-out slice, and that
    # the bootstrap only runs over tasks present in both conditions.
    def _pair(cond_a: str, cond_b: str) -> dict:
        a = by_cond.get(cond_a)
        b = by_cond.get(cond_b)
        if a is None or b is None:
            return {"error": f"missing condition: {cond_a}={a is not None} {cond_b}={b is not None}"}
        common = sorted(set(a.task_ids) & set(b.task_ids), key=lambda x: (len(x), x))
        if not common:
            return {"error": "no overlapping task_ids"}
        a_vec = [a.mean_reward_per_task[a.task_ids.index(t)] for t in common]
        b_vec = [b.mean_reward_per_task[b.task_ids.index(t)] for t in common]
        boot = paired_bootstrap_delta(a_vec, b_vec, iters=args.bootstrap_iters, seed=args.seed)
        # Companion two-proportion sanity check (Wilson-style)
        ztest = two_proportion_z(mean(a_vec), mean(b_vec), n=sum(b.n_trials_per_task))
        return {
            "comparison": f"{cond_a} − {cond_b}",
            "n_paired_tasks": len(common),
            "task_ids": common,
            "mean_a": round(mean(a_vec), 6),
            "mean_b": round(mean(b_vec), 6),
            "paired_bootstrap": boot,
            "two_proportion_sanity": ztest,
            "ci_95_separates_zero": boot["ci_95_low"] > 0,
            "p_below_0_05": boot["p_one_sided"] < 0.05,
        }

    deltas: dict = {}
    deltas["delta_a_scap_vs_baseline"] = _pair("scap_full", "baseline")
    deltas["delta_b_scap_vs_gepa"] = _pair("scap_full", "gepa_fewshot")
    deltas["ablation_a_vs_baseline"] = _pair("scap_ablation_a", "baseline")
    deltas["ablation_b_vs_baseline"] = _pair("scap_ablation_b", "baseline")
    deltas["ablation_c_vs_baseline"] = _pair("scap_ablation_c", "baseline")

    # Delta C: single-point comparison vs published reference (no CI).
    scap = by_cond.get("scap_full")
    if scap and scap.mean_reward_per_task:
        deltas["delta_c_scap_vs_published"] = {
            "comparison": "scap_full − published_tau2_retail",
            "scap_pass_at_1": round(mean(scap.mean_reward_per_task), 6),
            "published_reference": PUBLISHED_TAU2_RETAIL_PASS_AT_1,
            "delta_pp": round(mean(scap.mean_reward_per_task) - PUBLISHED_TAU2_RETAIL_PASS_AT_1, 6),
            "note": "informational; single-point published reference",
        }

    n_traces = _emit_held_out_traces(by_cond)
    logger.info("Wrote %d held-out traces to %s", n_traces, HELDOUT_TRACES_PATH)

    # Persist the stats block back into ablation_results.json
    payload["stats"] = {
        "computed_at": datetime.now(UTC).isoformat(),
        "bootstrap_iters": args.bootstrap_iters,
        "seed": args.seed,
        "deltas": deltas,
        "held_out_traces_path": str(HELDOUT_TRACES_PATH.relative_to(EVAL_DIR.parent)),
        "n_traces_written": n_traces,
    }
    ABLATION_RESULTS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Headline log line
    da = deltas.get("delta_a_scap_vs_baseline", {})
    boot = da.get("paired_bootstrap", {})
    logger.info(
        "Delta A: mean=%.4f, 95%% CI=[%.4f, %.4f], p_one_sided=%.4f, n_tasks=%d",
        boot.get("mean_delta", 0), boot.get("ci_95_low", 0), boot.get("ci_95_high", 0),
        boot.get("p_one_sided", 1), da.get("n_paired_tasks", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
