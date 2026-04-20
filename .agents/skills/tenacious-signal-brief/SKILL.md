---
name: tenacious-signal-brief
description: Generate a hiring signal brief and competitor gap brief for a Tenacious Consulting prospect from public data. Use this skill whenever the user mentions building outbound for Tenacious, enriching a prospect, the "hiring signal brief," "competitor gap brief," scoring AI maturity (0-3), combining Crunchbase + layoffs.fyi + job-post + leadership signals, or any phrasing like "research this company before we email them." Always use this instead of ad-hoc enrichment — the honesty and confidence-language rules (no "aggressive hiring" below five open roles, confidence-weighted phrasing, no fabricated numbers) are Tenacious brand constraints, not style preferences, and getting them wrong damages the client's reputation.
---

# Tenacious Hiring Signal Brief

A prospect-enrichment skill that produces two JSON artifacts before any outbound:

1. `hiring_signal_brief.json` — a per-prospect view of the five signals that define their buying window.
2. `competitor_gap_brief.json` — the prospect's AI-maturity position against 5–10 top-quartile peers in their sector.

Together these convert outbound from "you might need offshore engineering" into a grounded research finding the prospect cannot object to without contradicting their own public record.

## Why the honesty rule is load-bearing

Tenacious's reputation is the moat. Over-claiming a signal ("you are scaling aggressively" when the prospect has three open roles) costs more than silence. Every field in the output carries a confidence score, and the agent that consumes this brief MUST shift its language when confidence is low — asking rather than asserting. If you cannot ground a claim in the brief, leave the field null. A null field is a signal to the downstream agent to omit that line, not to improvise.

## Input

Either:
- A company name + domain, OR
- A Crunchbase ODM record ID

Seed data available (from the challenge seed repo):
- Crunchbase ODM sample (1,001 companies, Apache 2.0)
- layoffs.fyi CSV (CC-BY)
- Public job-post snapshot (early April 2026) or small live crawl (≤200 companies, no login, respect robots.txt)

## The five signals

| Signal | Source | Output field |
| --- | --- | --- |
| Funding event (Series A/B in last 180 days) | Crunchbase ODM + press | `funding.event`, `funding.amount_usd`, `funding.closed_at`, `funding.confidence` |
| Job-post velocity (60-day delta) | BuiltIn, Wellfound, LinkedIn careers pages | `hiring.open_eng_roles`, `hiring.delta_60d`, `hiring.confidence` |
| Layoffs (last 120 days) | layoffs.fyi | `layoffs.event`, `layoffs.headcount_pct`, `layoffs.closed_at`, `layoffs.confidence` |
| Leadership change (CTO/VP Eng in last 90 days) | Crunchbase + press | `leadership.change`, `leadership.role`, `leadership.appointed_at`, `leadership.confidence` |
| AI maturity score (0–3) | Job posts + team page + GitHub org + exec talks + tech stack | `ai_maturity.score`, `ai_maturity.inputs[]`, `ai_maturity.confidence` |

## AI maturity scoring — the part most agents get wrong

The score is a 0–3 integer with per-input justification. Weights:

- **High:** AI-adjacent open roles as a fraction of total eng openings; named AI/ML leadership on public team page.
- **Medium:** Public GitHub org commits on model/inference repos; CEO/CTO posts or keynotes in last 12 months naming AI as strategic.
- **Low:** Modern data/ML stack signal (dbt, Snowflake, Databricks, W&B, Ray, vLLM via BuiltWith/Wappalyzer); annual-report or fundraising-press AI positioning.

Rules:
- **Absence is not proof of absence.** Many companies keep AI work private. A score of 0 with low input count is "no public signal," not "no AI work."
- **Score 2 from weak inputs ≠ score 2 from strong inputs.** Emit a `confidence` field (low/medium/high) alongside the score.
- A readiness of 2 inferred from one medium-weight input must be flagged low-confidence so the downstream agent softens the language.

## Competitor gap brief

Once AI maturity is scored:

1. Identify 5–10 top-quartile competitors in the prospect's sector and size band using Crunchbase industry + employee-count filters.
2. Run the same AI-maturity scoring on each.
3. Compute the prospect's position in the sector's distribution (percentile + rank).
4. Extract 2–3 specific public-signal practices the top quartile shows that the prospect does not (e.g., "three of five top-quartile peers have a Head of AI on the public team page; prospect does not").

The output is a research finding, not a pitch. The downstream agent uses it to lead with a gap observation, then pivots to Tenacious capability only if the prospect engages.

## How AI maturity changes the pitch (downstream consumers care)

Emit a `pitch_guidance` field the email drafter reads:

- **Segment 4 (capability gap / ML platform / agentic):** Only viable at `ai_maturity.score >= 2`. Below that, flag `segment_4_viable: false` so the email drafter does not reach for it.
- **Segment 1 (recently funded):** Always viable, but language shifts. High readiness → "scale your AI team faster than in-house hiring supports." Low readiness → "stand up your first AI function with a dedicated squad."
- **Segment 2 (mid-market restructuring):** Same shift as Segment 1.
- **Segment 3 (leadership transition):** Unaffected — the new leader's stance is the variable, not prior state.

## Output schema

Emit two files. Exact schema:

```json
// hiring_signal_brief.json
{
  "prospect": {"company": "string", "domain": "string", "crunchbase_id": "string"},
  "enriched_at": "ISO-8601 timestamp",
  "funding": {
    "event": "Series A | Series B | null",
    "amount_usd": 14000000,
    "closed_at": "2026-02-14",
    "confidence": "high | medium | low",
    "sources": ["url1", "url2"]
  },
  "hiring": {
    "open_eng_roles": 27,
    "ai_adjacent_eng_roles": 8,
    "delta_60d": "+18",
    "confidence": "high | medium | low",
    "sources": ["url1"]
  },
  "layoffs": {
    "event": true,
    "headcount_pct": 12,
    "closed_at": "2026-01-10",
    "confidence": "high",
    "sources": ["layoffs.fyi/..."]
  },
  "leadership": {
    "change": true,
    "role": "VP Engineering",
    "name": "string",
    "appointed_at": "2026-03-01",
    "confidence": "medium",
    "sources": ["url"]
  },
  "ai_maturity": {
    "score": 2,
    "confidence": "low",
    "inputs": [
      {"type": "ai_adjacent_roles", "weight": "high", "evidence": "8 of 27 eng roles are ML/AI", "url": "..."},
      {"type": "named_leadership", "weight": "high", "evidence": null},
      {"type": "github_activity", "weight": "medium", "evidence": "3 commits last 30d on inference repo", "url": "..."}
    ]
  },
  "pitch_guidance": {
    "segment_4_viable": true,
    "tone_for_segment_1": "scale_existing | stand_up_first",
    "language_notes": "Low confidence on ai_maturity=2 — prefer ASK over ASSERT for AI claims."
  }
}
```

```json
// competitor_gap_brief.json
{
  "prospect": {"company": "string", "sector": "string", "size_band": "50-200"},
  "cohort": [
    {"company": "string", "ai_maturity": 3, "source_urls": ["..."]}
  ],
  "prospect_position": {"percentile": 35, "rank": "7 of 10"},
  "gaps": [
    {
      "practice": "Named Head of AI on public team page",
      "cohort_adoption": "3 of 5 top-quartile peers",
      "prospect_has_it": false,
      "confidence": "high"
    }
  ]
}
```

## Output discipline

- **Null over guess.** A field you cannot ground goes null with no fallback text.
- **Every non-null field carries a `confidence` value and a `sources` array.** No exceptions. The email drafter refuses to quote uncited claims.
- **No fabricated numbers.** Making up a funding amount or headcount is a disqualifying violation per the evidence-graph grading rule.
- **Stamp `enriched_at`.** HubSpot records require current enrichment timestamps; stale briefs are a tracked failure mode.

## Probe-triggering failure modes to avoid

These are the exact adversarial probes the challenge will grade you on. Fail them here and you fail them downstream:

- **ICP misclassification.** A company with both a recent layoff AND recent funding goes into Segment 2 (restructuring), not Segment 1 (freshly funded). Flag both signals; let the ICP classifier decide.
- **Signal over-claiming.** Fewer than 5 open roles → do NOT emit `hiring.delta_60d` as "aggressive." Emit the raw number; let the downstream agent decide the language.
- **AI-maturity false positive.** A "loud but shallow" company (one exec keynote, no team, no roles) must not score 3. Require at least one HIGH-weight input for any score ≥ 2.
- **AI-maturity false negative.** A "quiet but sophisticated" company (no public signal but real AI work) will score 0. Note this in `ai_maturity.language_notes` so the email drafter asks rather than asserts absence.
- **Gap over-claiming.** Do not emit a gap unless ≥ 3 of the cohort show the practice publicly. Sector-universal practices only.

## When to hand off to a human

If any of these trigger, set `requires_human_review: true` and stop:

- Prospect appears in layoffs.fyi within last 30 days with ≥25% headcount cut (sensitive; tone risk).
- Leadership change is a founder departure or a publicly announced restructure (brand risk).
- Competitor gap brief would name a direct Tenacious client (from `delivery_bench_summary.md` → do not use client names in a gap brief under any circumstance).

## Bench-to-brief match

Before closing the brief, cross-reference the prospect's implied need against `bench_summary.md`:

- If hiring signals point to "Python data engineers" and the bench has zero available, emit `bench_match: {matched: false, gap: "python_data"}`. The email drafter will then avoid specific capacity commitments.
- Never hallucinate bench capacity. The bench summary is ground truth.
