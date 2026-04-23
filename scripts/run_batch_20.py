"""
Run the full-thread demo across 20 synthetic prospects to produce the
p50/p95 latency sample required by the interim PDF.

Traces are appended to `outputs/full_thread_traces.jsonl` across runs.
After the batch completes, `scripts/compute_latency.py` reports per-stage
and end-to-end percentiles.

Usage:
    # run all 20 prospects
    uv run python -m scripts.run_batch_20

    # skip the first N if you've already run them
    uv run python -m scripts.run_batch_20 --start 1     # skip prospect 1
    uv run python -m scripts.run_batch_20 --start 5     # skip 1..5

    # run a smaller slice for testing
    uv run python -m scripts.run_batch_20 --start 0 --count 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from scripts.run_full_thread_demo import run

logger = logging.getLogger("batch_20")


# 20 synthetic prospects — mix of company sizes, titles, regions.
# The first row is the one you already ran ("Consolety / Alex Demo"), so
# --start 1 skips it by default.
PROSPECTS: list[dict[str, str]] = [
    {"company": "Consolety",   "contact_name": "Alex Demo",   "contact_email": "alex1@example.com",   "contact_phone": "+254700000001", "contact_title": "CTO"},
    {"company": "Northwind",   "contact_name": "Beth Kim",    "contact_email": "beth2@example.com",   "contact_phone": "+254700000002", "contact_title": "VP Engineering"},
    {"company": "Orbital",     "contact_name": "Carlos Ruiz", "contact_email": "carlos3@example.com", "contact_phone": "+254700000003", "contact_title": "Head of Data"},
    {"company": "Lumen Labs",  "contact_name": "Dana Shah",   "contact_email": "dana4@example.com",   "contact_phone": "+254700000004", "contact_title": "CTO"},
    {"company": "Fernwood",    "contact_name": "Eli Park",    "contact_email": "eli5@example.com",    "contact_phone": "+254700000005", "contact_title": "VP Engineering"},
    {"company": "Greenleaf",   "contact_name": "Fatima N.",   "contact_email": "fatima6@example.com", "contact_phone": "+254700000006", "contact_title": "CTO"},
    {"company": "Hightide",    "contact_name": "Gabe Liu",    "contact_email": "gabe7@example.com",   "contact_phone": "+254700000007", "contact_title": "Head of ML"},
    {"company": "Ironbark",    "contact_name": "Hana Ito",    "contact_email": "hana8@example.com",   "contact_phone": "+254700000008", "contact_title": "CTO"},
    {"company": "Junction",    "contact_name": "Idris Bah",   "contact_email": "idris9@example.com",  "contact_phone": "+254700000009", "contact_title": "VP Engineering"},
    {"company": "Kestrel",     "contact_name": "Juno Park",   "contact_email": "juno10@example.com",  "contact_phone": "+254700000010", "contact_title": "Head of Data"},
    {"company": "Linden",      "contact_name": "Kai Chen",    "contact_email": "kai11@example.com",   "contact_phone": "+254700000011", "contact_title": "CTO"},
    {"company": "Meridian",    "contact_name": "Lena Weiss",  "contact_email": "lena12@example.com",  "contact_phone": "+254700000012", "contact_title": "VP Engineering"},
    {"company": "Nightingale", "contact_name": "Mo Salim",    "contact_email": "mo13@example.com",    "contact_phone": "+254700000013", "contact_title": "Head of AI"},
    {"company": "Oakridge",    "contact_name": "Nina Vega",   "contact_email": "nina14@example.com",  "contact_phone": "+254700000014", "contact_title": "CTO"},
    {"company": "Palladium",   "contact_name": "Omar Zaid",   "contact_email": "omar15@example.com",  "contact_phone": "+254700000015", "contact_title": "VP Engineering"},
    {"company": "Quicksilver", "contact_name": "Priya Rao",   "contact_email": "priya16@example.com", "contact_phone": "+254700000016", "contact_title": "Head of ML"},
    {"company": "Redstone",    "contact_name": "Quinn Lee",   "contact_email": "quinn17@example.com", "contact_phone": "+254700000017", "contact_title": "CTO"},
    {"company": "Sablecraft",  "contact_name": "Raj Patel",   "contact_email": "raj18@example.com",   "contact_phone": "+254700000018", "contact_title": "VP Engineering"},
    {"company": "Timberline",  "contact_name": "Sofia Cruz",  "contact_email": "sofia19@example.com", "contact_phone": "+254700000019", "contact_title": "Head of Data"},
    {"company": "Umbra",       "contact_name": "Tomás Diaz",  "contact_email": "tomas20@example.com", "contact_phone": "+254700000020", "contact_title": "CTO"},
]


async def main(start: int, count: int) -> None:
    selected = PROSPECTS[start : start + count]
    if not selected:
        print(f"Nothing to run (start={start}, count={count}, total={len(PROSPECTS)})")
        return

    print(f"\n=== Batch: {len(selected)} prospects (index {start}..{start + len(selected) - 1}) ===\n")

    results: list[dict] = []
    batch_t0 = time.monotonic()

    for i, prospect in enumerate(selected, start=start + 1):
        run_t0 = time.monotonic()
        label = f"[{i}/{len(PROSPECTS)}] {prospect['company']}"
        print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
        try:
            summary = await run(
                company_name=prospect["company"],
                contact_name=prospect["contact_name"],
                contact_email=prospect["contact_email"],
                contact_phone=prospect["contact_phone"],
                contact_title=prospect["contact_title"],
            )
            run_elapsed = (time.monotonic() - run_t0) * 1000
            results.append({
                "company": prospect["company"],
                "ok": summary.get("ok", False),
                "stages_ok": len([s for s in summary.get("stages", []) if s.get("ok")]),
                "stages_failed": [s["stage"] for s in summary.get("stages", []) if not s.get("ok")],
                "wall_ms": round(run_elapsed, 0),
            })
            status = "OK" if summary.get("ok") else "PARTIAL"
            print(f"\n→ {label} {status} ({run_elapsed:.0f}ms wall)")
        except Exception as e:
            run_elapsed = (time.monotonic() - run_t0) * 1000
            logger.exception("%s FAILED: %s", label, e)
            results.append({
                "company": prospect["company"],
                "ok": False,
                "error": str(e),
                "wall_ms": round(run_elapsed, 0),
            })
            print(f"\n→ {label} FAILED: {e}")

    total_wall = (time.monotonic() - batch_t0) / 60
    ok_count = sum(1 for r in results if r.get("ok"))
    partial_count = len(results) - ok_count

    print(f"\n\n{'=' * 70}")
    print(f"Batch complete: {ok_count} fully OK, {partial_count} partial/failed, "
          f"{total_wall:.1f} min wall time")
    print(f"{'=' * 70}\n")
    for r in results:
        marker = "✓" if r.get("ok") else "✗"
        extras = r.get("stages_failed") or r.get("error") or ""
        print(f"  {marker} {r['company']:14s}  {r['wall_ms']:>7.0f}ms  {extras}")

    print(
        f"\nNext: uv run python -m scripts.compute_latency\n"
        f"(reads outputs/full_thread_traces.jsonl, writes outputs/latency_report.json)\n"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1,
                    help="0-based index of the first prospect to run (default: 1 — skip Consolety)")
    ap.add_argument("--count", type=int, default=len(PROSPECTS),
                    help=f"How many prospects to run (default: all {len(PROSPECTS)})")
    a = ap.parse_args()
    asyncio.run(main(a.start, a.count))
