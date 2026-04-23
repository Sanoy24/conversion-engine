"""
Compute p50/p95 latency from the aggregated full-thread trace log.

Reads outputs/full_thread_traces.jsonl (appended across every run of
run_full_thread_demo.py) and reports:
  - Total runs observed (count of distinct thread_ids seen)
  - Per-stage p50/p95/mean/max latency
  - Overall end-to-end p50/p95 (sum of stage latencies per run)

Usage:
    uv run python -m scripts.compute_latency
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "outputs" / "full_thread_traces.jsonl"


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((pct / 100.0) * (len(s) - 1)))
    return s[k]


def main() -> None:
    if not TRACES.exists():
        print(f"No trace file at {TRACES}")
        return

    by_stage: dict[str, list[float]] = defaultdict(list)
    by_thread: dict[str, float] = defaultdict(float)
    thread_ids: set[str] = set()

    with TRACES.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            stage = rec.get("stage", "unknown")
            latency = float(rec.get("latency_ms", 0.0))
            by_stage[stage].append(latency)
            tid = rec.get("thread_id")
            if tid:
                thread_ids.add(tid)
                by_thread[tid] += latency

    runs = len(thread_ids)
    print(f"\n=== Latency report — {runs} runs, {sum(len(v) for v in by_stage.values())} stage events ===\n")

    print(f"{'stage':45s}  {'n':>4s}  {'p50':>8s}  {'p95':>8s}  {'mean':>8s}  {'max':>8s}")
    print("-" * 90)
    for stage in sorted(by_stage):
        vals = by_stage[stage]
        print(
            f"{stage:45s}  {len(vals):>4d}  "
            f"{_pct(vals, 50):>8.1f}  {_pct(vals, 95):>8.1f}  "
            f"{statistics.mean(vals):>8.1f}  {max(vals):>8.1f}"
        )

    if by_thread:
        totals = list(by_thread.values())
        print("-" * 90)
        print(
            f"{'END-TO-END (per run)':45s}  {len(totals):>4d}  "
            f"{_pct(totals, 50):>8.1f}  {_pct(totals, 95):>8.1f}  "
            f"{statistics.mean(totals):>8.1f}  {max(totals):>8.1f}"
        )

    out = {
        "runs": runs,
        "per_stage": {
            s: {
                "n": len(v),
                "p50_ms": _pct(v, 50),
                "p95_ms": _pct(v, 95),
                "mean_ms": statistics.mean(v),
                "max_ms": max(v),
            }
            for s, v in by_stage.items()
        },
        "end_to_end": {
            "n": len(by_thread),
            "p50_ms": _pct(list(by_thread.values()), 50) if by_thread else 0,
            "p95_ms": _pct(list(by_thread.values()), 95) if by_thread else 0,
            "mean_ms": statistics.mean(list(by_thread.values())) if by_thread else 0,
        },
    }
    (ROOT / "outputs" / "latency_report.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print(f"\nWrote outputs/latency_report.json")


if __name__ == "__main__":
    main()
