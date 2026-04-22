"""
Fetch real datasets for the Conversion Engine enrichment pipeline.

Downloads:
1. Crunchbase ODM sample (1,001 companies) from github.com/luminati-io — converts
   the CSV (with JSON-encoded nested columns) into the JSON shape the parser
   expects.
2. layoffs.fyi mirror CSV — normalizes column names/date format for the parser.
3. Synthetic job_posts_snapshot.json — seeded from the top-N Crunchbase
   companies. Clearly labeled synthetic; the challenge permits either a frozen
   snapshot or a live crawl, and live crawling BuiltIn/Wellfound/LinkedIn is
   captcha-gated under the no-login rule.

Idempotent: re-runs overwrite existing files. Safe to call repeatedly.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import random
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

CRUNCHBASE_CSV_URL = (
    "https://raw.githubusercontent.com/luminati-io/"
    "Crunchbase-dataset-samples/main/crunchbase-companies-information.csv"
)
LAYOFFS_CSV_URL = (
    "https://raw.githubusercontent.com/bigyaa/"
    "Layoff-Prediction-Model/master/layoffs_data_fyi.csv"
)

JSON_COLUMNS = {
    "industries", "social_media_links", "featured_list", "builtwith_tech",
    "similar_companies", "location", "contacts", "current_employees",
    "semrush_location_list", "siftery_products", "funding_rounds_list",
    "bombora", "investors", "event_appearances", "acquisitions",
    "funds_raised", "investments", "apptopia", "current_advisors", "exits",
    "leadership_hire", "sub_organizations", "alumni", "diversity_investments",
    "funds_list", "layoff", "news", "headquarters_regions",
    "financials_highlights", "ipo_fields", "ipqwery", "overview_highlights",
    "people_highlights", "technology_highlights", "founders", "funds_total",
    "acquired_by", "investor_type", "investment_stage", "sub_organization_of",
}

AI_ROLE_TITLES = [
    "Senior ML Engineer", "Applied Scientist", "LLM Engineer",
    "AI Product Manager", "Data Platform Engineer", "MLOps Engineer",
]
ENG_ROLE_TITLES = [
    "Senior Backend Engineer", "Staff Software Engineer",
    "Site Reliability Engineer", "Full-Stack Engineer",
    "Engineering Manager", "Senior Data Engineer",
]

logger = logging.getLogger(__name__)


def fetch_crunchbase() -> Path:
    """Download the Crunchbase ODM CSV and convert to the parser's JSON shape."""
    out = DATA_DIR / "crunchbase_odm_sample.json"
    logger.info("Fetching Crunchbase ODM sample from %s", CRUNCHBASE_CSV_URL)
    csv_text = httpx.get(CRUNCHBASE_CSV_URL, timeout=60, follow_redirects=True).text

    records: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        rec = dict(row)
        for col in JSON_COLUMNS:
            raw = rec.get(col)
            if raw and isinstance(raw, str) and raw.strip():
                try:
                    rec[col] = json.loads(raw)
                except json.JSONDecodeError:
                    # Leave as-is if it isn't parseable (some rows have truncated JSON)
                    pass
        # Populate uuid from id if absent (parser checks both).
        if not rec.get("uuid"):
            rec["uuid"] = rec.get("id")
        records.append(rec)

    out.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote %d Crunchbase records → %s (%d KB)",
                len(records), out, out.stat().st_size // 1024)
    return out


def fetch_layoffs() -> Path:
    """Download a layoffs.fyi mirror and normalize to parser's column schema."""
    out = DATA_DIR / "layoffs.csv"
    logger.info("Fetching layoffs.fyi mirror from %s", LAYOFFS_CSV_URL)
    csv_text = httpx.get(LAYOFFS_CSV_URL, timeout=60, follow_redirects=True).text

    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict] = []
    for raw in reader:
        pct = raw.get("Percentage") or ""
        rows.append({
            "company": raw.get("Company", ""),
            "location": raw.get("Location_HQ", ""),
            "industry": raw.get("Industry", ""),
            "total_laid_off": raw.get("Laid_Off_Count", ""),
            "percentage_laid_off": pct,
            "date": raw.get("Date", ""),  # already YYYY-MM-DD
            "stage": raw.get("Stage", ""),
            "country": raw.get("Country", ""),
            "funds_raised_millions": raw.get("Funds_Raised", ""),
        })

    fieldnames = [
        "company", "location", "industry", "total_laid_off",
        "percentage_laid_off", "date", "stage", "country",
        "funds_raised_millions",
    ]
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d layoff records → %s", len(rows), out)
    return out


def synthesize_job_posts_snapshot(top_n: int = 60, seed: int = 42) -> Path:
    """Build a synthetic job-posts snapshot from the top-N Crunchbase companies.

    The challenge explicitly permits either a frozen snapshot or a live crawl
    (≤200 companies, no login, robots.txt respected). Since public job boards
    require login or captcha solving to pull sufficient detail, we seed
    deterministically from the Crunchbase cb_rank ordering so the snapshot is
    reproducible. Each record is clearly marked ``synthetic: true``.
    """
    rng = random.Random(seed)
    cb_file = DATA_DIR / "crunchbase_odm_sample.json"
    records = json.loads(cb_file.read_text(encoding="utf-8"))
    # Prefer active, for-profit companies with a cb_rank present; take top-N.
    def rank(r: dict) -> int:
        try:
            return int(r.get("cb_rank") or 10**9)
        except (TypeError, ValueError):
            return 10**9
    companies = sorted(records, key=rank)[:top_n]

    snapshot = []
    for i, c in enumerate(companies):
        # Scale hiring velocity with cb_rank buckets to create a realistic
        # spread of "hot" and "cold" companies.
        base = max(1, 25 - (i // 5))
        n_ai = rng.randint(0, max(1, base // 3))
        n_eng = rng.randint(max(1, base // 2), base)
        jobs = (
            [{"title": rng.choice(AI_ROLE_TITLES)} for _ in range(n_ai)]
            + [{"title": rng.choice(ENG_ROLE_TITLES)} for _ in range(n_eng)]
        )
        delta_60d = rng.choice(["+3", "+5", "+7", "+12", "+18", "-2", "0"])
        snapshot.append({
            "company": c.get("name"),
            "jobs": jobs,
            "delta_60d": delta_60d,
            "source_url": c.get("website") or c.get("url"),
            "synthetic": True,
            "seed_cb_rank": c.get("cb_rank"),
        })

    out = DATA_DIR / "job_posts_snapshot.json"
    out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote %d synthetic job-post entries → %s", len(snapshot), out)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(exist_ok=True)
    fetch_crunchbase()
    fetch_layoffs()
    synthesize_job_posts_snapshot()
    print("\nAll datasets ready in:", DATA_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
