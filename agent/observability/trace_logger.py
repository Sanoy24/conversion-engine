"""
JSONL Trace Logger.
Writes structured traces to trace_log.jsonl for evidence-graph integrity.
Every numeric claim in the final memo must resolve to a trace here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.models import TraceRecord

logger = logging.getLogger(__name__)

_log_path: Path | None = None


def init_trace_logger(log_dir: str = "./traces"):
    """Initialize the trace logger with output directory."""
    global _log_path
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    _log_path = path / "trace_log.jsonl"
    logger.info("Trace logger initialized: %s", _log_path)


def log_trace(trace: TraceRecord):
    """Append a trace record to the JSONL log."""
    if _log_path is None:
        init_trace_logger()

    assert _log_path is not None  # guaranteed by init_trace_logger
    try:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(trace.model_dump_json() + "\n")
    except Exception as e:
        logger.error("Failed to write trace %s: %s", trace.trace_id, str(e))


def log_traces(traces: list[TraceRecord]):
    """Append multiple trace records."""
    for trace in traces:
        log_trace(trace)


def read_traces(
    event_type: str | None = None,
    prospect_company: str | None = None,
) -> list[TraceRecord]:
    """Read trace records from the log, optionally filtered."""
    if _log_path is None or not _log_path.exists():
        return []

    traces = []
    with _log_path.open(encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                record = TraceRecord(**data)
                if event_type and record.event_type != event_type:
                    continue
                if prospect_company and record.prospect_company != prospect_company:
                    continue
                traces.append(record)
            except (json.JSONDecodeError, Exception):
                continue

    return traces


def compute_metrics() -> dict:
    """Compute aggregate metrics from the trace log for the memo."""
    traces = read_traces()

    if not traces:
        return {"total_traces": 0}

    total_cost = sum(t.cost_usd or 0 for t in traces)
    latencies = [t.latency_ms for t in traces if t.latency_ms is not None]
    successes = sum(1 for t in traces if t.success)

    # Compute p50/p95 latency
    p50 = 0
    p95 = 0
    if latencies:
        sorted_lat = sorted(latencies)
        p50 = sorted_lat[len(sorted_lat) // 2]
        p95_idx = int(len(sorted_lat) * 0.95)
        p95 = sorted_lat[min(p95_idx, len(sorted_lat) - 1)]

    return {
        "total_traces": len(traces),
        "total_cost_usd": round(total_cost, 4),
        "success_rate": round(successes / len(traces), 4) if traces else 0,
        "latency_p50_ms": round(p50, 1),
        "latency_p95_ms": round(p95, 1),
        "llm_calls": sum(1 for t in traces if "llm" in t.event_type),
        "emails_sent": sum(1 for t in traces if "email" in t.event_type),
        "sms_sent": sum(1 for t in traces if "sms" in t.event_type),
        "hubspot_writes": sum(1 for t in traces if "hubspot" in t.event_type),
        "bookings": sum(1 for t in traces if "calcom" in t.event_type),
    }
