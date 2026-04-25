"""
Backfill numeric placeholders in `report/memo.md` from
`eval/ablation_results.json` after the held-out sweep + scap_stats run.

Replaces the `__NAME__` placeholders with values derived from the per-condition
aggregates and the paired-bootstrap stats block. Idempotent — re-running with
the same inputs produces the same output. Verifies that no `__*__` placeholder
remains in the rendered file before exit.

Usage:
    python -m report.backfill_memo            # writes memo.md (in place)
    python -m report.backfill_memo --check    # exit non-zero if any placeholder remains
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("backfill_memo")

REPO = Path(__file__).parent.parent
ABLATION_RESULTS = REPO / "eval" / "ablation_results.json"
MEMO_PATH = REPO / "report" / "memo.md"


def _by_cond(records: list[dict]) -> dict[str, dict]:
    return {r["condition"]: r for r in records}


def _fmt_pp(x: float) -> str:
    """Format a delta as a percentage-point string with sign."""
    pp = x * 100
    sign = "+" if pp >= 0 else ""
    return f"{sign}{pp:.2f}"


def _fmt_pass(x: float) -> str:
    return f"{x:.4f}"


def _fmt_cost(x: float) -> str:
    return f"{x:.4f}"


def build_substitutions() -> dict[str, str]:
    payload = json.loads(ABLATION_RESULTS.read_text(encoding="utf-8"))
    cond = _by_cond(payload["conditions"])
    stats = payload.get("stats", {})
    deltas = stats.get("deltas", {})

    s: dict[str, str] = {}

    if "baseline" in cond:
        c = cond["baseline"]
        s["BASELINE_HELDOUT_PASS"] = _fmt_pass(c["pass_at_1"])
        lo, hi = c["ci_95_range"]
        s["BASELINE_HELDOUT_CI_LO"] = _fmt_pass(lo)
        s["BASELINE_HELDOUT_CI_HI"] = _fmt_pass(hi)
        s["BASELINE_HELDOUT_COST_PER_SIM"] = _fmt_cost(c["cost_per_run"])

    if "scap_full" in cond:
        c = cond["scap_full"]
        s["SCAP_PASS_AT_1"] = _fmt_pass(c["pass_at_1"])
        lo, hi = c["ci_95_range"]
        s["SCAP_CI_LO"] = _fmt_pass(lo)
        s["SCAP_CI_HI"] = _fmt_pass(hi)
        s["SCAP_COST_PER_SIM"] = _fmt_cost(c["cost_per_run"])

    if "gepa_fewshot" in cond:
        c = cond["gepa_fewshot"]
        s["GEPA_PASS_AT_1"] = _fmt_pass(c["pass_at_1"])
        lo, hi = c["ci_95_range"]
        s["GEPA_CI_LO"] = _fmt_pass(lo)
        s["GEPA_CI_HI"] = _fmt_pass(hi)
        s["GEPA_COST_PER_SIM"] = _fmt_cost(c["cost_per_run"])

    da = deltas.get("delta_a_scap_vs_baseline", {})
    if da and "paired_bootstrap" in da:
        b = da["paired_bootstrap"]
        s["DELTA_A_PP"] = _fmt_pp(b["mean_delta"])
        s["DELTA_A_CI_LO"] = _fmt_pp(b["ci_95_low"])
        s["DELTA_A_CI_HI"] = _fmt_pp(b["ci_95_high"])
        s["DELTA_A_P"] = f"{b['p_one_sided']:.4f}"

    db = deltas.get("delta_b_scap_vs_gepa", {})
    if db and "paired_bootstrap" in db:
        s["DELTA_B_PP"] = _fmt_pp(db["paired_bootstrap"]["mean_delta"])

    dc = deltas.get("delta_c_scap_vs_published", {})
    if dc:
        s["DELTA_C_PP"] = _fmt_pp(dc["delta_pp"])

    return s


def apply(memo_text: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        memo_text = memo_text.replace(f"__{k}__", v)
    return memo_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if any __NAME__ placeholder remains")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not ABLATION_RESULTS.exists():
        logger.error("ablation_results.json missing; run eval.run_heldout + eval.scap_stats first")
        return 1
    text = MEMO_PATH.read_text(encoding="utf-8")

    subs = build_substitutions()
    if not subs:
        logger.warning("no substitutions resolved from ablation_results.json")

    new = apply(text, subs)
    remaining = re.findall(r"__[A-Z_]+__", new)

    if args.check:
        if remaining:
            logger.error("Unresolved placeholders: %s", sorted(set(remaining)))
            return 1
        logger.info("All placeholders resolved; memo.md is final.")
        return 0

    MEMO_PATH.write_text(new, encoding="utf-8")
    logger.info("Backfilled %d substitutions into %s", len(subs), MEMO_PATH)
    if remaining:
        logger.warning("Still unresolved: %s", sorted(set(remaining)))
    return 0 if not remaining else 2


if __name__ == "__main__":
    raise SystemExit(main())
