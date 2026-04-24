"""
Held-out evaluation orchestrator for Act IV.

Runs all six conditions on the sealed held-out slice (20 retail tasks × N
trials) and writes `eval/ablation_results.json` + `eval/held_out_traces.jsonl`.

Conditions:
  baseline         : stock tau2 agent, no SCAP postscript (control)
  scap_full        : full SCAP postscript (main method, Delta A endpoint)
  scap_ablation_a  : SCAP rule 1 only — identity authentication
  scap_ablation_b  : SCAP rule 2 only — echo-then-confirm
  scap_ablation_c  : SCAP rule 3 only — ask for missing parameters
  gepa_fewshot     : automated-optimisation baseline — 2 policy-adherence
                     few-shot examples injected as a postscript (Delta B)

The held-out slice is defined once and committed in `eval/heldout_slice.json`;
this script is the single caller of tau2-bench for final evaluation. Per-sim
cost on DeepSeek V3 ≈ $0.02, so full-sweep spend is ≈ $12 at n_trials=5 or
≈ $9.6 when ablations run at n_trials=3 (default).

Usage:
    python -m eval.run_heldout                 # full sweep (6 conditions)
    python -m eval.run_heldout --conditions baseline scap_full  # subset
    python -m eval.run_heldout --smoke         # 1 task × 1 trial per condition
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from eval.harness import run_baseline

EVAL_DIR = Path(__file__).parent
HELDOUT_SLICE_PATH = EVAL_DIR / "heldout_slice.json"
ABLATION_RESULTS_PATH = EVAL_DIR / "ablation_results.json"
HELDOUT_TRACES_PATH = EVAL_DIR / "held_out_traces.jsonl"

logger = logging.getLogger("run_heldout")


# ── SCAP postscripts ──────────────────────────────────────────────────
#
# The full postscript and its ablation slices. Each is the full content of
# the SCAP_POSTSCRIPT env var for the corresponding condition.

SCAP_FULL = """\
<signal_confidence_aware_phrasing>
Ask-not-assert rule: when evidence is weak or incomplete, ASK rather than ACT.

Before calling any tool that modifies state (cancel_order, exchange_order,
modify_user_address, modify_pending_order, refund, update_order, return_items),
you MUST:
1. Confirm the user's identity has been authenticated in this conversation.
   If authenticate_user_by_email / find_user_id_by_email has not been called
   with a confirmed match in this thread, call it first or ask the user for
   the email or last-order-id needed.
2. Echo back the specific action and the specific parameters (order id, item
   id, amount) and wait for an explicit affirmative confirmation. A plain
   "yes" or "ok" counts; a restatement of an earlier request does not.
3. If the user has not given you the exact value for a parameter, ASK. Do
   not guess, default, or fabricate any id or amount.

This rule is a precondition for modify-type tool calls only. Read-only tools
(get_order_details, get_user_details, find_user_id_by_email) may be called
freely to resolve the above.
</signal_confidence_aware_phrasing>
""".strip()


SCAP_ABLATION_A = """\
<signal_confidence_aware_phrasing>
Ask-not-assert rule: authenticate before modifying.

Before calling any tool that modifies state (cancel_order, exchange_order,
modify_user_address, modify_pending_order, refund, update_order, return_items),
you MUST confirm the user's identity has been authenticated in this
conversation. If authenticate_user_by_email / find_user_id_by_email has not
been called with a confirmed match, call it first or ask the user for the
email or last-order-id needed.

Read-only tools may be called freely.
</signal_confidence_aware_phrasing>
""".strip()


SCAP_ABLATION_B = """\
<signal_confidence_aware_phrasing>
Ask-not-assert rule: echo-then-confirm before modifying.

Before calling any tool that modifies state (cancel_order, exchange_order,
modify_user_address, modify_pending_order, refund, update_order, return_items),
you MUST echo back the specific action and the specific parameters (order id,
item id, amount) and wait for an explicit affirmative confirmation. A plain
"yes" or "ok" counts; a restatement of an earlier request does not.

Read-only tools may be called freely.
</signal_confidence_aware_phrasing>
""".strip()


SCAP_ABLATION_C = """\
<signal_confidence_aware_phrasing>
Ask-not-assert rule: ask for missing parameters.

When calling any tool, if the user has not given you the exact value for a
parameter (order id, item id, amount), ASK them. Do not guess, default, or
fabricate any id or amount.

Read-only tools may be called freely.
</signal_confidence_aware_phrasing>
""".strip()


# Automated-optimisation baseline: two in-context policy-adherence examples.
# This is the cheapest defensible GEPA-style proxy (prompt-engineered few-shot)
# at the same token budget as SCAP_FULL, giving a fair Delta B comparison.
GEPA_FEWSHOT = """\
<policy_adherence_examples>
Example 1 (desired behavior):
User: "Cancel my last order."
Agent: "To confirm, I'll cancel your most recent order — could you share the
email on the account so I can authenticate and look it up?"

Example 2 (desired behavior):
User: "Change my delivery address."
Agent: "Happy to help. Before I make any changes, I'll need to verify your
identity. Could you share the email address on your account?"

Follow the pattern above: authenticate first, echo the action, ask for any
missing parameter. Do not modify account state without confirmation.
</policy_adherence_examples>
""".strip()


CONDITIONS: dict[str, dict] = {
    "baseline":        {"postscript": None,            "entry_type": "heldout_baseline"},
    "scap_full":       {"postscript": SCAP_FULL,       "entry_type": "heldout_scap_full"},
    "scap_ablation_a": {"postscript": SCAP_ABLATION_A, "entry_type": "heldout_scap_ablation_a"},
    "scap_ablation_b": {"postscript": SCAP_ABLATION_B, "entry_type": "heldout_scap_ablation_b"},
    "scap_ablation_c": {"postscript": SCAP_ABLATION_C, "entry_type": "heldout_scap_ablation_c"},
    "gepa_fewshot":    {"postscript": GEPA_FEWSHOT,    "entry_type": "heldout_gepa_fewshot"},
}


def _load_heldout_slice() -> dict:
    if not HELDOUT_SLICE_PATH.exists():
        raise FileNotFoundError(
            f"Held-out slice not found at {HELDOUT_SLICE_PATH}. "
            "Commit it before running the Act IV eval."
        )
    return json.loads(HELDOUT_SLICE_PATH.read_text(encoding="utf-8"))


def run_condition(
    *,
    condition: str,
    task_ids: list[str],
    n_trials: int,
    model: str,
    temperature: float,
    max_concurrency: int,
    timeout_s: int,
) -> dict:
    """Invoke the harness for one condition and return the aggregate record."""
    cfg = CONDITIONS[condition]
    extra_env: dict[str, str] | None = None
    if cfg["postscript"] is not None:
        extra_env = {"SCAP_POSTSCRIPT": cfg["postscript"]}
    logger.info("=== Running condition: %s (n_tasks=%d, n_trials=%d) ===",
                condition, len(task_ids), n_trials)
    t0 = time.monotonic()
    agg = run_baseline(
        model=model,
        domain="retail",
        n_tasks=len(task_ids),
        n_trials=n_trials,
        temperature=temperature,
        entry_type=cfg["entry_type"],
        task_ids=task_ids,
        extra_env=extra_env,
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        auto_resume=True,
    )
    elapsed = time.monotonic() - t0
    logger.info(
        "condition=%s pass@1=%.4f ci=%.4f cost=$%.4f wall=%ds",
        condition, agg["pass_at_1"], agg["ci_95"], agg["total_cost"], int(elapsed),
    )
    return {"condition": condition, **agg}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--conditions", nargs="+", default=list(CONDITIONS.keys()),
        choices=list(CONDITIONS.keys()),
        help="Subset of conditions to run (default: all six)",
    )
    parser.add_argument("--n-trials", type=int, default=5,
                        help="Trials per task (default 5 for main conditions)")
    parser.add_argument("--n-trials-ablation", type=int, default=3,
                        help="Trials per task for scap_ablation_* (default 3 to save cost)")
    parser.add_argument("--model", default="openrouter/deepseek/deepseek-chat-v3-0324")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--smoke", action="store_true",
                        help="Run 1 task × 1 trial per condition for a cheap sanity check")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    slice_ = _load_heldout_slice()
    task_ids = slice_["task_ids"]
    if args.smoke:
        task_ids = task_ids[:1]
        n_trials = 1
        n_trials_ablation = 1
        logger.warning("SMOKE MODE: 1 task × 1 trial per condition")
    else:
        n_trials = args.n_trials
        n_trials_ablation = args.n_trials_ablation

    started = datetime.now(UTC).isoformat()
    t_run = time.monotonic()

    records: list[dict] = []
    total_cost = 0.0
    for cond in args.conditions:
        trials = n_trials_ablation if cond.startswith("scap_ablation_") else n_trials
        rec = run_condition(
            condition=cond,
            task_ids=task_ids,
            n_trials=trials,
            model=args.model,
            temperature=args.temperature,
            max_concurrency=args.max_concurrency,
            timeout_s=args.timeout_s,
        )
        records.append(rec)
        total_cost += rec.get("total_cost", 0.0)

    payload = {
        "run_id": f"heldout_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "wall_clock_s": round(time.monotonic() - t_run, 2),
        "heldout_slice": {
            "task_ids": task_ids,
            "n_tasks": len(task_ids),
        },
        "model": args.model,
        "temperature": args.temperature,
        "n_trials_main": n_trials,
        "n_trials_ablation": n_trials_ablation,
        "smoke_mode": args.smoke,
        "conditions": records,
        "total_llm_cost_usd": round(total_cost, 4),
    }
    ABLATION_RESULTS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Wrote %s (total cost $%.2f across %d conditions)",
                ABLATION_RESULTS_PATH, total_cost, len(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
