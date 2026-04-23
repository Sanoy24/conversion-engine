"""Extract τ²-Bench scores from all completed runs and generate score_log.json."""

import json
from pathlib import Path

SIM_DIR = Path("eval/tau2-bench/data/simulations")
OUTPUT_DIR = Path("outputs")
EVAL_DIR = Path("eval")

OUTPUT_DIR.mkdir(exist_ok=True)
EVAL_DIR.mkdir(exist_ok=True)


def extract_run_scores(run_dir: Path) -> dict | None:
    results_path = run_dir / "results.json"
    if not results_path.exists():
        return None

    data = json.loads(results_path.read_text(encoding="utf-8"))
    sims = data.get("simulations", [])
    if not sims:
        return None

    rewards = []
    costs = []
    per_task = []

    for sim in sims:
        # Reward lives in reward_info.reward in τ²-Bench format
        reward_info = sim.get("reward_info", {})
        r = reward_info.get("reward", sim.get("reward", 0)) or 0
        rewards.append(r)
        agent_cost = sim.get("agent_cost", 0) or 0
        user_cost = sim.get("user_cost", 0) or 0
        costs.append(agent_cost + user_cost)
        per_task.append({
            "task_id": sim.get("task_id", "?"),
            "reward": r,
            "passed": r >= 1.0,
            "agent_cost": agent_cost,
            "user_cost": user_cost,
            "duration_s": sim.get("duration", 0),
            "reward_breakdown": reward_info.get("reward_breakdown", {}),
        })

    passed = sum(1 for r in rewards if r >= 1.0)
    avg_reward = sum(rewards) / len(rewards) if rewards else 0
    pass_at_1 = passed / len(rewards) if rewards else 0
    total_cost = sum(costs)

    return {
        "run_name": run_dir.name,
        "total_tasks": len(sims),
        "passed": passed,
        "failed": len(sims) - passed,
        "average_reward": round(avg_reward, 4),
        "pass_at_1": round(pass_at_1, 4),
        "pass_at_1_pct": f"{pass_at_1*100:.1f}%",
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_task_usd": round(total_cost / len(sims), 4) if sims else 0,
        "per_task": per_task,
    }


def main():
    runs = sorted([d for d in SIM_DIR.iterdir() if d.is_dir()])
    all_scores = []

    print("=" * 60)
    print("τ²-Bench Score Extraction")
    print("=" * 60)

    for run_dir in runs:
        scores = extract_run_scores(run_dir)
        if scores:
            all_scores.append(scores)
            print(f"\n{scores['run_name']}:")
            print(f"  Tasks: {scores['total_tasks']}")
            print(f"  Passed: {scores['passed']}/{scores['total_tasks']}")
            print(f"  Pass@1: {scores['pass_at_1_pct']}")
            print(f"  Avg Reward: {scores['average_reward']}")
            print(f"  Total Cost: ${scores['total_cost_usd']:.4f}")

    # Write score_log.json
    score_log_path = EVAL_DIR / "score_log.json"
    with score_log_path.open("w", encoding="utf-8") as f:
        json.dump(all_scores, f, indent=2)
    print(f"\nWrote {score_log_path} ({len(all_scores)} runs)")

    # Find the best run for baseline.md
    if all_scores:
        best = max(all_scores, key=lambda s: s["pass_at_1"])
        print(f"\nBest run: {best['run_name']}")
        print(f"  Pass@1: {best['pass_at_1_pct']}")
        print(f"  Avg Reward: {best['average_reward']}")

        # Write summary to outputs
        summary = {
            "best_run": best["run_name"],
            "pass_at_1": best["pass_at_1"],
            "pass_at_1_pct": best["pass_at_1_pct"],
            "average_reward": best["average_reward"],
            "total_tasks": best["total_tasks"],
            "total_runs": len(all_scores),
        }
        with (OUTPUT_DIR / "tau2_summary.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
