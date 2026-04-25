"""
layoffs.fyi data parser.

Loads the CC-BY layoff dataset and checks for recent layoff events.
Signal: layoff in last 120 days → Segment 2 (restructuring) candidate.

Dataset fields (source: AlexTheAnalyst/MySQL-YouTube-Series):
  company, location, industry, total_laid_off, percentage_laid_off,
  date, stage, country, funds_raised_millions
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path

from agent.config import settings
from agent.models import Confidence, LayoffSignal, SourceRef

logger = logging.getLogger(__name__)

_layoffs_cache: list[dict] | None = None


def _load_layoffs_data() -> list[dict]:
    """Load and parse the layoffs.fyi CSV."""
    global _layoffs_cache
    if _layoffs_cache is not None:
        return _layoffs_cache

    data_path = Path(settings.layoffs_data_path)
    if not data_path.exists():
        logger.warning("Layoffs data not found at %s. Using empty dataset.", data_path)
        _layoffs_cache = []
        return _layoffs_cache

    records: list[dict] = []
    with data_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)

    _layoffs_cache = records
    logger.info("Loaded %d layoff records from layoffs.fyi.", len(records))
    return _layoffs_cache


def check_layoffs(
    company_name: str,
    lookback_days: int = 120,
) -> LayoffSignal:
    """
    Check if a company has layoffs in the last *lookback_days* days.

    Returns ``LayoffSignal(event=True)`` if found, ``event=False`` otherwise.
    Per the ICP classifier: layoff in last 120 days overrides fresh-budget optimism.
    """
    data = _load_layoffs_data()
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    name_lower = company_name.lower().strip()

    matching_events: list[dict] = []
    for record in data:
        rec_company = (record.get("company") or "").lower().strip()
        if name_lower not in rec_company and rec_company not in name_lower:
            continue

        # Parse date — dataset uses M/D/YYYY format
        date_str = record.get("date")
        event_date = _parse_date(date_str)
        if event_date and event_date >= cutoff:
            matching_events.append(
                {
                    "date": date_str,
                    "total_laid_off": record.get("total_laid_off"),
                    "percentage": record.get("percentage_laid_off"),
                    "industry": record.get("industry"),
                    "stage": record.get("stage"),
                    "country": record.get("country"),
                }
            )

    if not matching_events:
        return LayoffSignal(
            event=False,
            observed_at=datetime.utcnow().isoformat(),
            confidence=Confidence.HIGH,  # Confident that NO layoff was found in dataset
        )

    # Use the most recent event
    latest = matching_events[0]
    headcount_pct = _parse_percentage(latest.get("percentage"))

    return LayoffSignal(
        event=True,
        headcount_pct=headcount_pct,
        closed_at=latest.get("date"),
        observed_at=datetime.utcnow().isoformat(),
        confidence=Confidence.HIGH,
        sources=[
            SourceRef(
                url="https://layoffs.fyi",
                description=f"Layoff event: {latest.get('total_laid_off', 'unknown')} affected",
            )
        ],
    )


def check_layoffs_from_crunchbase(record: dict) -> LayoffSignal:
    """
    Check the Crunchbase record's built-in ``layoff`` field.

    Some Crunchbase records embed layoff data directly — use this as a
    secondary signal alongside the layoffs.fyi CSV lookup.
    """
    import json

    layoff_raw = record.get("layoff")
    if not layoff_raw:
        return LayoffSignal(
            event=False,
            observed_at=datetime.utcnow().isoformat(),
            confidence=Confidence.LOW,
        )

    # Parse the JSON field
    try:
        if isinstance(layoff_raw, str):
            layoff_data = json.loads(layoff_raw)
        else:
            layoff_data = layoff_raw
    except (json.JSONDecodeError, TypeError):
        return LayoffSignal(
            event=False,
            observed_at=datetime.utcnow().isoformat(),
            confidence=Confidence.LOW,
        )

    if isinstance(layoff_data, list) and layoff_data:
        latest = layoff_data[0]
        return LayoffSignal(
            event=True,
            headcount_pct=_parse_percentage(latest.get("percentage")),
            closed_at=latest.get("date"),
            observed_at=datetime.utcnow().isoformat(),
            confidence=Confidence.MEDIUM,
            sources=[
                SourceRef(
                    url=latest.get("url"),
                    description="Crunchbase layoff field",
                )
            ],
        )

    return LayoffSignal(
        event=False,
        observed_at=datetime.utcnow().isoformat(),
        confidence=Confidence.LOW,
    )


def _parse_date(date_str: str | None) -> datetime | None:
    """Try multiple date formats."""
    if not date_str:
        return None
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_percentage(pct_str: str | None) -> float | None:
    """Parse percentage strings like '0.05' → 5.0 or '12%' → 12.0."""
    if not pct_str:
        return None
    pct_str = str(pct_str).strip().replace("%", "")
    try:
        val = float(pct_str)
        # Dataset stores as decimal (0.05 = 5%), convert to percentage
        if 0 < val <= 1:
            return round(val * 100, 1)
        return val
    except ValueError:
        return None
