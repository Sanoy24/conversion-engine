---
name: tenacious-playwright-enrichment
description: Fetch public job posts, team pages, GitHub org activity, and press/funding signals for a Tenacious prospect via Playwright (or TinyFish), respecting robots.txt, the no-login rule, and the 200-company live-crawl cap for the challenge week. Use this skill whenever the user mentions scraping, Playwright, TinyFish, Browser Use, job-post velocity, BuiltIn / Wellfound / LinkedIn careers pages, layoffs.fyi CSV parsing, Crunchbase ODM lookups, enrichment pipeline plumbing, or the frozen April 2026 snapshot vs live crawl choice. This skill is the upstream data fetcher feeding tenacious-signal-brief — the brief skill consumes structured JSON; this skill produces it from messy public HTML while staying inside the challenge's access constraints.
---

# Tenacious Playwright Enrichment

The upstream data layer. `tenacious-signal-brief` consumes structured JSON; this skill produces it from public web sources. The two skills are deliberately split: enrichment failure modes (blocked scrapes, stale snapshots, robots.txt violations) are operational; brief failure modes (over-claiming, null-over-guess) are semantic. Different concerns.

## Access constraints (these are hard rules, not guidelines)

From the challenge's data-handling policy:

1. **Public pages only.** Do not log in to any site. Do not bypass a login wall by any mechanism.
2. **Do not bypass captchas.** If a page requires a captcha, record the URL and skip — treat it as a signal gap.
3. **Respect robots.txt.** Fetch `/robots.txt` first; honor disallow directives for the paths you want.
4. **Live-crawl cap: ≤ 200 companies during the challenge week.** The seed repo includes a frozen early-April 2026 snapshot; prefer that for any work that does not specifically need recency.
5. **Rate limit yourself.** 1 request per 2 seconds per domain is a safe default; back off to 1 per 5 seconds on any 429 or 503.

Violations are grounds for program removal. The kill-switch concept extends here: every scrape call goes through a single wrapper that checks the company count against the cap and aborts if exceeded.

## Tool choice — Playwright vs TinyFish

The updated challenge stack lists **TinyFish OR Playwright + FastAPI wrapper** as acceptable. Decision rule:

- **Playwright is the default.** Free, scriptable, no API key, full control over rate limiting and headers. Everything in this skill assumes Playwright unless stated otherwise.
- **TinyFish is worth reaching for** if you already have credit and want to skip the boilerplate of cookie handling, retries, and headless-browser lifecycle management. It is a hosted browser-agent service; you describe what to fetch and it returns structured output.
- **Browser Use** is a third acceptable option (mentioned in the supporting scenario) — same category as TinyFish.

Whichever tool you pick, the access constraints above do NOT change. TinyFish respects robots.txt too, but you are still on the hook for the 200-company live-crawl cap, the no-login rule, and the no-captcha-bypass rule. A hosted tool does not launder a policy violation.

One practical note: if you switch from Playwright to TinyFish mid-week, keep the output schema identical (the `enrichment_bundle` JSON below). The signal-brief skill and market-space-map skill consume that schema; the scraper underneath is swappable.

## The five data sources

| Source                                                   | What you get                                                                                 | Access pattern                                                                                       |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Crunchbase ODM sample (seed repo)                        | Firmographics, funding events, founders, industry, location for 1,001 companies (Apache 2.0) | Static JSON in repo — no network call                                                                |
| layoffs.fyi CSV                                          | Layoff events, headcount %, source URL (CC-BY)                                               | Download CSV once, parse locally                                                                     |
| BuiltIn / Wellfound / company careers pages              | Open job postings, titles, posting dates                                                     | Playwright, public pages, respect robots.txt                                                         |
| GitHub org pages                                         | Recent commit activity on AI/ML repos                                                        | GitHub public API (rate limit: 60/hr unauth, 5000/hr with a PAT — use PAT) or Playwright as fallback |
| Press / blog (company site + Crunchbase linked articles) | Funding press releases, leadership announcements, exec commentary                            | Playwright, public pages                                                                             |

## Snapshot vs. live crawl

The seed repo has a frozen early-April 2026 snapshot. Decision rule:

- **Use the snapshot** for Act I, Act III probe construction, and any Act IV ablation that does not depend on signal recency. This is most of the week.
- **Use a live crawl** only when the brief requires genuinely current signal (e.g., a leadership change announced this week, a layoff in the last 30 days). Live crawls eat into the 200-company cap.
- **Always record which source was used** in the output: `source: "snapshot_2026-04-06" | "live_2026-04-20"`. Downstream consumers need to know.

## Output — the enrichment bundle

This skill writes one JSON file per company, consumed by `tenacious-signal-brief`:

```json
{
  "company": "string",
  "domain": "string",
  "crunchbase_id": "string",
  "fetched_at": "ISO-8601",
  "source_mode": "snapshot_2026-04-06 | live_2026-04-20",
  "firmographics": {
    "industry": "string",
    "employee_count_band": "15-80 | 80-200 | 200-2000 | 2000+",
    "hq_location": { "country": "string", "city": "string", "timezone": "string" },
    "source": "crunchbase_odm"
  },
  "funding": {
    "last_round": { "stage": "Series B", "amount_usd": 14000000, "closed_at": "2026-02-14" },
    "sources": ["url", "url"],
    "scrape_success": true
  },
  "job_posts": {
    "total_open": 42,
    "engineering_open": 27,
    "ai_adjacent": 8,
    "titles_sample": ["Senior ML Engineer", "Data Platform Engineer", "..."],
    "delta_60d": "+18",
    "delta_method": "compared against snapshot 2026-02-20",
    "sources": ["builtin.com/...", "wellfound.com/..."],
    "scrape_success": true,
    "scrape_failures": []
  },
  "layoffs": {
    "events_last_120d": [{ "date": "2026-01-10", "pct": 12, "count": 80, "source": "layoffs.fyi" }],
    "source": "layoffs_fyi_csv_2026-04-15"
  },
  "leadership": {
    "changes_last_90d": [
      {
        "role": "VP Engineering",
        "name": "...",
        "announced_at": "2026-03-01",
        "source": "press_url"
      }
    ]
  },
  "github_org": {
    "org_name": "string | null",
    "ai_ml_repo_activity": { "commits_last_30d": 3, "active_repos": ["inference-toolkit"] },
    "scrape_success": true
  },
  "team_page": {
    "ai_leadership_named": false,
    "evidence": null,
    "url_fetched": "string",
    "scrape_success": true
  },
  "exec_commentary": {
    "signals": [
      { "type": "keynote", "title": "AI in production", "date": "2025-11-10", "source_url": "..." }
    ]
  },
  "scrape_log": {
    "robots_allowed": true,
    "pages_fetched": 7,
    "pages_blocked": 1,
    "blocked_urls": ["linkedin.com/..."],
    "total_duration_ms": 14200
  }
}
```

## Failure handling — the part that breaks pipelines

Scrapers fail more than their authors expect. The honesty rule from `tenacious-signal-brief` extends here: **a failed scrape is null, not a guess.**

Rules:

- **Blocked by robots.txt.** Set the relevant field to null, log the block in `scrape_log.blocked_urls`, do not retry from a different user agent.
- **Captcha or login wall encountered.** Same — null + log.
- **Timeout (>30s per page).** One retry with exponential backoff. On second failure: null + log.
- **HTTP 429.** Back off to 1 request per 5 seconds on that domain for the remainder of the run.
- **HTML structure changed** (selector returns nothing). Null + log with a `selector_miss: true` flag so you can fix the selector separately.

Critical: never substitute a snapshot value for a failed live scrape without flagging it. If a live scrape was requested and failed, either explicitly fall back to the snapshot with `fallback: "snapshot"` in the output, or return null. Silent fallback corrupts the recency signal.

## 60-day velocity — the non-trivial computation

Job-post velocity is `current_count - count_60d_ago`. You need two snapshots:

- **Snapshot A:** the seed repo's frozen snapshot (early April 2026) or an earlier one if provided.
- **Snapshot B:** the current scrape.

If you only have Snapshot B (live crawl with no historical baseline), emit `delta_60d: null` with `delta_method: "no_baseline"`. Do NOT estimate from "typical" hiring rates. Downstream consumers will interpret null correctly.

If the company was not in Snapshot A (too small at the time, just launched), emit `delta_60d: "new_in_period"` with the current count. This is different from null and downstream treats it differently.

## Crunchbase ODM sample — the static layer

The Crunchbase sample is 1,001 records, Apache 2.0, in the seed repo. It is a dataset, not a live API. Rules:

- Look up by `crunchbase_id` or exact company name match; do not fuzzy-match (false positives on firmographics corrupt everything downstream).
- If the company is not in the sample, emit `crunchbase_id: null` and the signal brief will flag the prospect as ineligible (every lead in HubSpot must reference a Crunchbase record per the challenge rules).
- Funding events older than 180 days are not relevant for the buying-window signal; still include them for completeness but mark `relevant_for_signal: false`.

## layoffs.fyi — the simplest source

Download the CSV once per week. Parse with pandas or equivalent. Filter by:

- Company name exact match (case-insensitive).
- Date within last 120 days (the signal-brief's cutoff).

The CSV is CC-BY; credit layoffs.fyi in any external output that quotes layoff numbers.

## GitHub — the rate-limit trap

Unauthenticated GitHub API: 60 requests/hour. You will blow through this on the first ten companies.

Options:

- **With PAT:** 5000/hour. The PAT is user-scoped and read-only for public data; no policy conflict.
- **Scraped from github.com/{org} pages:** slower, but no API rate limit. Use as fallback.

What to fetch:

- Repos matching patterns: `*llm*`, `*inference*`, `*model*`, `*training*`, `*rag*`, `*agent*`.
- For each matching repo: commit count in last 30 days (public).
- Do not clone repos. Metadata only.

## What NOT to scrape

- **LinkedIn member pages (personal profiles).** Hard block. Public employee counts on company pages are fine if reachable without login; individual profile scraping is not.
- **Email addresses from any source.** Tenacious prospects are synthetic for the challenge week; real contact data must not enter your system.
- **Anything behind an auth wall.** If a data point requires a login to see, it does not exist for this pipeline.

## Integration points

- **Input to `tenacious-signal-brief`.** The brief skill reads this JSON and produces the confidence-scored hiring signal brief + competitor gap brief.
- **Input to `tenacious-market-space-map`.** The market-space map runs the AI-maturity scoring over the whole Crunchbase sample; it needs the enrichment bundle for each company, batched.
- **Langfuse observability.** Every scrape run emits a trace with page-count, duration, success/failure breakdown. These traces feed into the cost-per-lead math in the memo.

## Cost envelope

This is the cheap part of the pipeline. Playwright is free; GitHub PAT is free. The costs to watch:

- Time budget (you are capped at 200 live-crawl companies, not by dollars).
- Egress if running on a cloud VM — usually negligible.
- Re-run cost if a scraper fails silently and you do not notice for a day.

## Never do this

- Never log in, even if the site "allows" it.
- Never bypass a captcha, even with a paid service.
- Never fuzzy-match Crunchbase names.
- Never substitute a snapshot value for a failed live scrape without an explicit `fallback` flag.
- Never exceed the 200-company live-crawl cap; the wrapper aborts if counted.
- Never scrape personal LinkedIn profiles.
