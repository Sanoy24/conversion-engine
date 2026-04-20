---
name: tenacious-market-space-map
description: Build the population-level market-space map over the full Crunchbase ODM sample — sector × size × AI-readiness cells, with hand-labeled precision/recall validation and top-cell ranking for outbound allocation. Use this skill whenever the user mentions the market-space map, the Day 6 stretch, the distinguished-tier deliverable, market_space.csv, top_cells.md, methodology.md, hand-labeling validation, or any phrasing like "where should Tenacious point the system at the sector level." The underlying AI-maturity scoring primitive already exists in tenacious-signal-brief; this skill is the population-level extension with the validation discipline that keeps it honest — a superficial map is worse than none because it misdirects strategy with false confidence.
---

# Tenacious Market-Space Map

The Day 6 distinguished-tier stretch. This skill is **not** required to pass the week — attempt it only if the five-act core deliverables are in good shape. A superficial market map damages the memo because it adds confident-sounding strategy claims on shaky statistical ground.

## What this skill is and is not

- **Is:** a population-level segmentation of the Crunchbase ODM sample (1,001 companies) into (sector × size-band × AI-readiness) cells, with a rank of the top cells for outbound allocation, validated on a hand-labeled sub-sample with reported precision and recall.
- **Is not:** a generic sector analysis. The output must change how Tenacious allocates outbound effort — that is the distinguished-tier bar. "SaaS companies at 50–200 are promising" fails. "Dev-tools-for-engineers companies in the 50–200 band at AI-readiness 2, with ≥20 open engineering roles and no public Head of AI, are Tenacious's highest-return Segment 1 cell — 23 candidates, median bench-match score 0.81" passes.

## The core primitive already exists

`tenacious-signal-brief` scores AI maturity (0–3) for one company. This skill runs that scoring over the full sample and aggregates. Do not re-invent the scoring rubric; import it. If you find a scoring bug during the population run, fix it in the signal-brief skill and propagate, do not fork.

The hand-labeling is the new work. The scoring rubric is the old work.

## The work, in order

### Step 1 — Enrich the population

Run the `tenacious-playwright-enrichment` pipeline over the Crunchbase ODM sample in batches. Use the frozen snapshot, not live crawls — 1,001 companies exceeds the 200 live-crawl cap and you do not need fresh signal at population scale.

Expected outputs:
- 1,001 enrichment bundles (`enrichment_bundles.jsonl`).
- A `coverage_report.md` naming how many companies had (a) a Crunchbase match, (b) a job-post page reachable in the snapshot, (c) a resolvable GitHub org, (d) complete enough data for AI-maturity scoring.

If coverage is < 70% on any dimension, flag it in the methodology — the map is lossy by that much and the precision/recall numbers should be reported conditional on complete records only.

### Step 2 — Score AI maturity across the population

Run the signal-brief's AI-maturity scoring over every enriched company. Emit a per-company `ai_maturity_score.json` containing score, confidence, and per-input evidence.

### Step 3 — Define sectors and size bands

**Sectors:** use the Crunchbase `industry` field as the starting point, but consolidate. Crunchbase has hundreds of labels; you want 10–15 meaningful sectors (e.g., "fintech infra", "vertical SaaS — healthcare", "dev tools", "data / ML platforms", "horizontal SaaS — marketing"). Document the consolidation in `methodology.md`.

**Size bands:** align with the ICP classifier's bands for cross-skill consistency:
- 15–80 (Segment 1 range)
- 80–200
- 200–2000 (Segment 2 range)
- 2000+

**Readiness bands:** 0, 1, 2, 3 (the AI-maturity score itself, not re-binned).

Cells: sector × size × readiness. Expected ~150 populated cells from 1,001 companies; most will have < 5 members (too sparse to act on).

### Step 4 — Hand-label a validation sample

**This is the step that separates a defensible map from a superficial one.**

Sampling:
- Random sample 50 companies stratified across the score bands (roughly 12–13 per score 0/1/2/3).
- For each, spend ~5 minutes reading the team page, careers page, and any linked blog/press. Assign a human-judged AI-maturity score 0–3 with a one-sentence justification.
- This takes 4–5 hours. Budget it on Day 6 morning before touching the cell ranking.

Outputs:
- `validation_set.jsonl` with `{company, automated_score, human_score, human_justification}`.
- Precision/recall per score band:
  - For each band B, precision = (companies where automated and human agree on B) / (companies automated scored B).
  - Recall = (companies where automated and human agree on B) / (companies human scored B).
- Confusion matrix, 4×4.

If precision or recall on any band is < 0.6, the map is not trustworthy enough to ship. Options: (a) narrow the claim to bands where numbers are defensible, (b) fix the scoring primitive and re-run, (c) cut the stretch and use the Day 6 time on core deliverables. Explicitly pick one in the methodology.

### Step 5 — Compute cell-level features

For each (sector × size × readiness) cell:

```json
{
  "cell_id": "devtools__50-200__readiness_2",
  "sector": "dev tools",
  "size_band": "80-200",
  "readiness_band": 2,
  "population": 23,
  "avg_funding_last_12mo_usd": 18400000,
  "avg_hiring_velocity_delta_60d": 12.4,
  "bench_match_score": 0.81,
  "combined_score": 0.74,
  "confidence": "medium"
}
```

- **Population:** companies in the cell.
- **Avg funding:** mean last-round amount for companies with a funding event in last 12 months; null if < 3 companies in cell have one.
- **Avg hiring velocity:** mean `delta_60d` across cell members with complete data.
- **Bench-match score:** 0–1 score of how well the cell's inferred stack needs match `bench_summary.md`. A Python-heavy cell with a Python-heavy bench scores 1.0; a Rust-heavy cell with no Rust bench scores 0.1.
- **Combined score:** weighted sum of (funding recency, hiring velocity, bench match, readiness fit). Document the weights in methodology.md. Do not tune the weights to make the top cells look prettier — the weighting is a prior, not an outcome.
- **Confidence:** derived from cell population. < 5 companies → low regardless of other metrics; 5–15 → medium; 15+ → high.

### Step 6 — Rank and write outputs

Three files:

**`market_space.csv`** — one row per cell. Columns match the JSON schema above. Order by `combined_score` descending. Include cells with `population = 0` at the bottom for completeness; someone reading the CSV should see the whole grid, not just the populated bits.

**`top_cells.md`** — the 3 to 5 top-ranked cells with `confidence: high`. For each:
- One-paragraph profile (what these companies look like, what their hiring pattern is, what they do not have that the top quartile does).
- Specific outbound allocation recommendation (e.g., "Allocate 40% of Segment 1 weekly outbound here; expected yield 3–4 discovery calls per 25 touches based on signal-grounded reply-rate benchmark").
- Explicit risk notes — what could make this cell a bad bet (niche saturation, recent sector-wide layoffs, publicly visible competitor plays).

**`methodology.md`** — the methodology and the honesty register:
- Sector consolidation rules (the Crunchbase-label → sector mapping).
- Size-band boundaries and rationale.
- Scoring weights in the combined score.
- Hand-labeling protocol — sample size, stratification, labeler identity (probably you), inter-rater agreement if multiple labelers (usually N/A for a solo week).
- Precision and recall per readiness band, with CIs or at least the denominator.
- Known false positives and false negatives in the scoring — the "quietly sophisticated, publicly silent" and "loud but shallow" patterns from the signal-brief honesty rules apply at population scale and must be named here.
- What a reasonable user should NOT conclude from the map.

## Honesty at population scale

Every population-level claim extends the per-company honesty rule. Specifically:

- **Publicly silent companies are over-represented in score 0.** If your map says "only 8% of the fintech-infra 50–200 band is at readiness 2+," caveat with "of companies with public signal — silent sophisticated companies are invisible to this measurement."
- **Loud shallow companies are over-represented in score 2+.** The validation set's confusion matrix will show this; quote the number.
- **Sector consolidation masks heterogeneity.** "Dev tools" covers both infra-for-AI and JS build tooling; readiness distributions differ meaningfully. If your cells aggregate across these, flag the heterogeneity.
- **The Crunchbase ODM sample is not a random sample of the population.** It is 1,001 companies selected for some reason by whoever assembled the sample. The map's generalizability is bounded by the sample's sampling method. Name this in methodology.md.

## Why this is not automatically distinguished-tier

The rubric says distinguished-tier credit goes to a map that "would change how Tenacious allocates outbound effort." A technically-correct map that reproduces the intuition of a Tenacious partner (e.g., "post-Series-A SaaS is where we fish") does not earn it, even if the precision and recall are pristine. The originality credit lives in the specific cells you surface — the non-obvious ones with defensible evidence. If all your top cells are the ones Tenacious would have guessed, the output is sound but not distinguished.

One way to surface non-obvious cells: look for cells where `bench_match_score` is high AND `combined_score` is moderate AND `population` is adequate. These are the "underfished" cells — plausibly high-return but not the obvious pick. Name them in `top_cells.md` explicitly as "non-obvious but defensible."

## When to stop and cut the stretch

Day 6 morning check:
- If precision or recall on any readiness band < 0.6 after hand-labeling, AND you cannot fix the scoring primitive in 2 hours, **cut the stretch.** Use the remaining time on Act V memo polish.
- If coverage of the enriched population < 60%, **cut the stretch.** The map is too lossy to claim strategic findings.
- If the top 5 cells all have population < 5 companies, **cut the stretch.** There is nothing to act on.

Cutting is not failure — a superficial map in the memo is explicitly penalized. The Act V memo with only the core five-act deliverables is a stronger submission than an Act V memo with a shaky market map.

## What goes in the memo

If the map ships, it goes in the Skeptic's Appendix — not Page 1. One paragraph referencing the top cells + methodology file, with an explicit precision/recall statement. The memo's main decision argument stays grounded in the per-lead traces from Act IV; the market map is contextual evidence that informs pilot scope, not the core claim.

If the map does not ship, remove every reference to it from the repo. An unfinished `market_space.csv` in the deliverables folder will be read as a failure artifact.

## Never do this

- Never ship without hand-labeled validation. Unvalidated scores are false confidence.
- Never tune combined-score weights post-hoc to make the top cells match intuition. That inverts the analysis.
- Never claim a cell-level finding with population < 5. Low-N cells go in the CSV for completeness but stay out of `top_cells.md`.
- Never reference a named Tenacious client as an example of a cell archetype. The case studies are redacted for a reason.
- Never let the stretch compromise Acts I–V. If Day 6 is eating into Act V memo time, stop.
