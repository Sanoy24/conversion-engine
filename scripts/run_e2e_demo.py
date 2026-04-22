"""
End-to-end demo: run the full Conversion Engine pipeline against N synthetic
prospects drawn from the real Crunchbase ODM sample.

Every prospect:
  enrich (Crunchbase + layoffs + job-posts + leadership + AI maturity)
  → ICP classify
  → draft outbound email (honesty rule + confidence-aware phrasing + bench-gated)
  → record conversation thread

Outputs:
  outputs/e2e_traces.jsonl    — one record per prospect with timing + cost
  outputs/e2e_summary.json    — aggregate p50/p95 latency and cost table

Kill switch (LIVE_OUTBOUND_ENABLED) defaults to false, so no real email/SMS is
dispatched — the pipeline latency covers the enrichment + LLM drafting only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from pathlib import Path
from statistics import median

from agent.config import settings
from agent.core.orchestrator import process_new_prospect

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"

logger = logging.getLogger("e2e_demo")


def _load_candidates(n: int, seed: int) -> list[dict]:
    data = json.loads(Path(settings.crunchbase_data_path).read_text(encoding="utf-8"))
    rng = random.Random(seed)
    # Prefer companies with a real uuid and an industries array; shuffle then take n.
    eligible = [
        r for r in data
        if r.get("uuid") and r.get("name") and isinstance(r.get("industries"), list)
    ]
    rng.shuffle(eligible)
    return eligible[:n]


async def _run_one(record: dict) -> dict:
    start = time.monotonic()
    fake_email = f"test-{(record.get('uuid') or record.get('id') or 'x')[:8]}@staff-sink.local"
    try:
        result = await process_new_prospect(
            company_name=record.get("name"),
            domain=record.get("website") or record.get("url"),
            crunchbase_id=record.get("uuid") or record.get("id"),
            contact_name="Synthetic Contact",
            contact_email=fake_email,
            contact_title="CTO",
        )
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "company": record.get("name"),
            "uuid": record.get("uuid"),
            "ok": True,
            "latency_ms": round(latency_ms, 1),
            "pipeline_latency_ms": round(result.get("pipeline_latency_ms", 0), 1),
            "segment": result["classification"]["segment"],
            "segment_confidence": result["classification"]["confidence"],
            "secondary_segment": result["classification"].get("secondary_segment"),
            "evidence_count": len(result["classification"].get("evidence", [])),
            "email_subject": result["email_draft"]["subject"],
            "email_body_chars": len(result["email_draft"]["body"]),
            "requires_human_review": result["requires_human_review"],
            "total_cost_usd": round(result.get("total_cost_usd", 0), 6),
            "trace_count": result.get("trace_count", 0),
            "thread_id": result.get("thread_id"),
            "gap_brief_present": bool(result.get("gap_brief")),
        }
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        logger.exception("Prospect pipeline failed for %s", record.get("name"))
        return {
            "company": record.get("name"),
            "uuid": record.get("uuid"),
            "ok": False,
            "error": str(e)[:500],
            "latency_ms": round(latency_ms, 1),
        }


def _pcts(values: list[float], decimals: int = 1) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "mean": 0.0}
    sorted_v = sorted(values)
    p95_idx = min(int(len(sorted_v) * 0.95), len(sorted_v) - 1)
    return {
        "p50": round(median(sorted_v), decimals),
        "p95": round(sorted_v[p95_idx], decimals),
        "min": round(sorted_v[0], decimals),
        "max": round(sorted_v[-1], decimals),
        "mean": round(sum(sorted_v) / len(sorted_v), decimals),
    }


async def main(n: int, seed: int, max_parallel: int) -> None:
    OUTPUTS.mkdir(exist_ok=True)
    traces_path = OUTPUTS / "e2e_traces.jsonl"
    summary_path = OUTPUTS / "e2e_summary.json"
    traces_path.unlink(missing_ok=True)

    candidates = _load_candidates(n, seed)
    logger.info("Running %d prospects (kill_switch=%s, max_parallel=%d)",
                len(candidates), settings.live_outbound_enabled, max_parallel)

    sem = asyncio.Semaphore(max_parallel)

    async def _bounded(rec: dict) -> dict:
        async with sem:
            return await _run_one(rec)

    results = await asyncio.gather(*[_bounded(r) for r in candidates])

    with traces_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    successes = [r for r in results if r.get("ok")]
    latencies = [r["pipeline_latency_ms"] for r in successes]
    costs = [r["total_cost_usd"] for r in successes]

    segments: dict[str, int] = {}
    for r in successes:
        segments[r["segment"]] = segments.get(r["segment"], 0) + 1

    summary = {
        "n_total": len(results),
        "n_success": len(successes),
        "n_failed": len(results) - len(successes),
        "kill_switch_enabled": not settings.live_outbound_enabled,
        "pipeline_latency_ms": _pcts(latencies),
        "cost_usd": {
            "total": round(sum(costs), 4),
            "per_prospect": _pcts(costs, decimals=6),
        },
        "segment_distribution": segments,
        "gap_brief_coverage": sum(1 for r in successes if r.get("gap_brief_present")),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=25, help="Number of prospects to run")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-parallel", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(main(args.n, args.seed, args.max_parallel))
