---
name: tenacious-icp-classifier
description: Classify a Tenacious prospect into one of four ICP segments (recently-funded / mid-market-restructuring / leadership-transition / capability-gap) with a confidence score and an abstention option. Use this skill whenever the user needs to assign a prospect to a segment, decide which pitch variant to send, handle ambiguous cases like a post-layoff company that also raised funding, or build the Act IV mechanism-design variant that gates pitch specificity on classifier confidence. The abstention mechanism ("if confidence is below threshold, send a generic exploratory email rather than a segment-specific pitch") is a named mechanism direction in the challenge — use this skill when implementing or evaluating it.
---

# Tenacious ICP Classifier

A lightweight classifier that maps a `hiring_signal_brief.json` to one of four fixed segment labels (or abstains). The segment names are fixed for grading — do not rename them.

## The four segments

| Label | Qualifying signals | Disqualifying signals |
| --- | --- | --- |
| `segment_1_recently_funded` | `funding.event` in {Series A, Series B} AND `funding.closed_at` within 180 days AND headcount 15–80 | Layoff in last 120 days; headcount > 200 |
| `segment_2_mid_market_restructuring` | Headcount 200–2000 AND (`layoffs.event == true` in last 120 days OR public restructure) | Recent Series A/B fundraise with no cost signal |
| `segment_3_leadership_transition` | `leadership.change == true` for CTO or VP Engineering in last 90 days | None from a classifier standpoint — this segment can overlap with 1 or 2 |
| `segment_4_capability_gap` | `ai_maturity.score >= 2` AND a specific build signal (ML migration, agentic, data contracts) in job-post titles | `ai_maturity.score < 2`; no capability-specific job postings |

## The abstention rule

The classifier MUST emit one of:
- A single segment label with `confidence` in {high, medium, low}
- `segment_overlap` with an ordered list of candidate segments (when multiple qualify)
- `abstain` (when no segment meets the minimum qualifying signals at confidence ≥ medium)

Abstention is not failure — it is the correct output when public signal is thin. The downstream email drafter reads `abstain` and sends a generic exploratory email rather than a segment-specific pitch. A false-confident classification is worse than an abstention: the wrong pitch burns the contact and damages the brand.

## Overlap resolution

Two overlaps are common and the resolution rules are specific:

### Overlap 1: Recent funding + recent layoffs

A company that closed a Series B four months ago AND cut 15% of staff last month is NOT Segment 1. It is Segment 2. Cost pressure overrides fresh-budget optimism because the pitch language must acknowledge the cost constraint. Sending a "you have fresh budget" pitch to a company mid-restructure is a tracked failure mode in the challenge.

Output in this case:
```json
{"segment": "segment_2_mid_market_restructuring", "confidence": "high", "overlap_notes": "Funding within 180d overridden by more recent layoffs."}
```

### Overlap 2: Leadership transition + (Segment 1 or Segment 2)

New CTO at a recently-funded company: Segment 3 is the primary pitch because the 90-day vendor-reassessment window is narrower and more actionable. Emit Segment 3 as primary with Segment 1/2 as secondary.

```json
{"segment": "segment_3_leadership_transition", "secondary_segment": "segment_1_recently_funded", "confidence": "high"}
```

## Confidence calibration

Confidence is a function of signal count AND signal weight, not just count:

- **High:** Two or more HIGH-weight signals from the brief are non-null with `confidence: high`.
- **Medium:** One HIGH-weight signal at `confidence: high`, OR two MEDIUM-weight signals at `confidence: high`.
- **Low:** All signals are `confidence: medium` or lower, or any qualifying signal has `confidence: low`.

A `confidence: low` classification triggers the abstention pathway downstream — the email drafter treats low-confidence classifications as abstentions unless the operator explicitly overrides.

## Output schema

```json
{
  "prospect": {"company": "string", "crunchbase_id": "string"},
  "segment": "segment_1_recently_funded | segment_2_mid_market_restructuring | segment_3_leadership_transition | segment_4_capability_gap | abstain",
  "secondary_segment": "string | null",
  "confidence": "high | medium | low",
  "evidence": [
    {"signal": "funding", "value": "Series B, $14M, 2026-02-14", "weight": "qualifying"},
    {"signal": "hiring.delta_60d", "value": "+18 open roles", "weight": "supporting"}
  ],
  "disqualifiers_checked": ["layoffs_last_120d", "headcount_bound"],
  "overlap_notes": "string | null",
  "pitch_guidance_ref": "copy from signal brief pitch_guidance block"
}
```

## The mechanism-design hook (Act IV)

One of the named mechanism directions in the challenge is: **ICP classifier with abstention.** A lightweight classifier that scores segment confidence; if confidence is below threshold, the agent sends a generic exploratory email rather than a segment-specific pitch.

When building the Act IV mechanism:
- Threshold tuning is the knob. Start at "abstain if confidence < medium."
- Ablation: report pass@1 on the held-out slice at three thresholds (high-only / medium-or-better / any-confidence) so you can show the confidence-threshold trade-off curve.
- The thing you are paying for: fewer wrong-pitch emails (brand protection) at the cost of fewer segment-specific hooks (lower reply rate on marginal classifications). The memo math should show both sides.

## Failure modes to probe

These map directly to the probe library the challenge requires:

- **Post-layoff-plus-funding collision.** Seed a prospect with both signals; confirm classifier emits Segment 2, not Segment 1.
- **Ambiguous leadership transition.** A new VP Engineering from within (promotion, not external hire). Many agents over-index on the title change; the 90-day vendor-reassessment heuristic is weaker for internal promotions. Flag this in `overlap_notes` and downgrade confidence.
- **Segment 4 over-reach at AI maturity 1.** A job post mentioning "AI" does not make a score-1 prospect Segment 4. Require `ai_maturity.score >= 2` AND a specific build signal.
- **Headcount edge cases.** A 180-person company with a recent Series A — Segment 1 bound is 15–80. This disqualifies; the classifier must not stretch the bound. Emit `abstain` with a disqualifier note.

## Never do this

- Do not emit a segment label with `confidence: low` AND `abstain: false`. Low confidence means abstain.
- Do not invent a fifth segment. The four are fixed for grading.
- Do not commit bench capacity in the classifier output. Capacity-matching is the email drafter's job, informed by `bench_summary.md`.
