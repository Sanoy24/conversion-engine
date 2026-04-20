---
name: tenacious-decision-memo
description: Draft the two-page Tenacious decision memo (memo.pdf) addressed to the Tenacious CEO and CFO, with every numeric claim traceable to a trace file or published source via evidence_graph.json. Use this skill whenever the user mentions Act V, the final memo, the decision memo, the skeptic's appendix, the evidence graph, pilot scope recommendation, or any phrasing like "write up the CEO memo" or "draft the two-page report." The memo is exactly two pages, no more, no less. Every number must map to a source. Fabricated Tenacious numbers are a disqualifying violation — separate from the standard penalty.
---

# Tenacious Decision Memo (Act V)

The single highest-stakes deliverable. It is the artifact the Tenacious CEO reads to decide whether to point the system at live revenue. The grading rule is unusual: **fabricated Tenacious numbers are a disqualifying violation, separate from the standard penalty.** Treat every number as if a CFO with four quarters of internal data will check it.

## Hard constraints

- **Exactly two pages.** Not 1.8, not 2.1. Two.
- **Page 1 is The Decision.** Page 2 is The Skeptic's Appendix. The order is fixed.
- **Every number cites a source.** Either a trace ID from `held_out_traces.jsonl`, a line from `invoice_summary.json`, a field in `bench_summary.md`, or a published benchmark with URL. Uncited numbers are a failure.
- **`evidence_graph.json` ships alongside the memo** mapping every numeric claim to its source. The grader runs automated checks against this file.
- **No fabricated Tenacious numbers.** The seed repo's ACV ranges, conversion rates, and stalled-thread rate are the only internal numbers you may quote. Extrapolations from them must show the calculation.

## Page 1 — The Decision

Fixed structure. Do not reorganize.

### 1. Executive summary (three sentences)

Sentence 1: what was built. Sentence 2: the headline number (pass@1 on held-out, or reply-rate delta — pick the one that best makes the case). Sentence 3: the recommendation (pilot scope, single segment, specific dollar budget).

Example tone: "We built an outbound qualification system for Tenacious that leads with a research finding rather than a capability pitch. On the τ²-Bench retail held-out slice, pass@1 is 47% (95% CI 43–51), a 9-point lift over the Day 1 baseline at $0.09 per task. We recommend a 30-day pilot against Segment 1 (recently-funded Series A/B) at 60 outbound touches per week, $400 weekly budget, measured on reply rate against the 7–12% signal-grounded benchmark."

### 2. τ²-Bench pass@1 table

Three rows. 95% CIs for each. Source: `held_out_traces.jsonl`.

| Condition | Pass@1 | 95% CI | Cost / task |
| --- | --- | --- | --- |
| Published τ²-Bench reference (retail, Feb 2026) | ~0.42 | published | — |
| Your Day 1 baseline | X.XX | [lo, hi] | $X.XX |
| Your method | X.XX | [lo, hi] | $X.XX |

### 3. Cost per qualified lead

Derive from rig usage + LLM spend + trace count. Sources: `invoice_summary.json` + `trace_log.jsonl`.

Show the formula: `cost_per_lead = total_llm_spend / qualified_leads_count`, with both numerator and denominator traceable. Target < $5. Penalty threshold $8.

### 4. Speed-to-lead delta

Current manual process stalled-thread rate: 30–40% (Tenacious executive interview, in seed materials).
Your measured stalled-thread rate from traces: X%.
If your number is lower, show the math — which thread events reset the clock, what defines "stalled," and which trace events were counted.

### 5. Competitive-gap outbound performance

Fraction of outbound that led with a research finding (AI maturity score + top-quartile gap from `competitor_gap_brief.json`) vs. a generic Tenacious pitch.
Reply-rate delta between the two variants.
Source: traces tagged by outbound variant. This is the point of the whole system; if the delta is not positive and significant, name that explicitly and diagnose in the appendix.

### 6. Annualized dollar impact — three adoption scenarios

| Scenario | Lead volume / week | Reply rate | Discovery calls | Proposal rate | Close rate | ACV range | Annualized revenue |
| --- | --- | --- | --- | --- | --- | --- | --- |
| One segment only | X | 7–12% | X | 35–50% | 25–40% | $240–720K (outsourcing) or $80–300K (consulting) | $X.XM |
| Two segments | | | | | | | |
| All four segments | | | | | | | |

Every rate in this table cites a source: either measured from traces (reply rate) or quoted from the seed baseline-numbers table (conversion rates, ACV ranges). No freehand estimation.

### 7. Pilot scope recommendation

One segment. One lead volume. One dollar budget. One measurable success criterion Tenacious can track after 30 days. Specific numbers only.

Example: "Run against Segment 1 only. 60 outbound touches per week. $400/week budget (LLM + enrichment + rig). Success criterion: reply rate ≥ 6% (below the 7–12% benchmark but above the 1–3% generic baseline) measured across the 30-day window. Trigger for continuation: ≥ 6%. Trigger for rework: 3–6%. Trigger for kill: < 3%."

## Page 2 — The Skeptic's Appendix

The CFO reads Page 1. The CEO reads Page 2. Page 2 is what earns trust. The rubric penalizes generic risks and rewards Tenacious-specific ones.

### 1. Four failure modes τ²-Bench does not capture

Each must be Tenacious-specific. Generic ("the agent may hallucinate") is penalized.

For each, four sub-bullets:
- What it is (one sentence, concrete)
- Why the benchmark misses it (τ²-Bench is retail; it does not model offshore-perception, bench mismatch, founder tone, or long-cycle ACV)
- What would need to be added to catch it (specific probe, specific dataset, specific instrument)
- What that would cost (hours, dollars, or both)

Good examples of Tenacious-specific failure modes:
- "Offshore-perception objection handling. A founder replies 'we had a bad offshore experience in 2024 — tell me why this time is different.' The retail benchmark has no analog. Catching it requires a probe with 10+ synthetic defensive-objection replies and a scoring rubric graded by a human who knows the Tenacious sales voice. Cost: 6 hours of synthesis + 2 hours of grading."
- "Bench mismatch under specific-stack pressure. Prospect asks 'do you have Rust backend engineers with distributed-systems experience?' Bench summary shows zero. The agent's correct move is a defer-to-human; the failure is an improvised claim. τ²-Bench retail has no bench artifact to gate against."
- "Brand-reputation risk from wrong signal data. If 5% of signal-grounded emails have a factually wrong hiring-signal claim, the damage compounds because the emails are public records the recipient can quote."
- "Segment-2 language triggering in-house hiring managers. A restructuring-pitch email forwarded to the wrong internal stakeholder reads as 'replace us.' τ²-Bench does not model who else reads the email."

### 2. Public-signal lossiness

Name the known false-positive and false-negative modes of AI maturity scoring.

- **Quietly sophisticated, publicly silent.** Companies doing real AI work with no public signal score 0. What does the agent do wrong? It either skips them or opens with a Segment 1 "stand up your first AI function" pitch that reads as insulting. Business impact: loss of a high-value target per N attempts; quantify N from your data.
- **Loud but shallow.** Companies with one keynote and zero AI team score 2+ from the exec-commentary signal. The agent pitches Segment 4 capability work; the prospect has nowhere to put a capability-gap engagement. Business impact: wasted Segment 4 pitch, low reply rate, and brand dilution if the mismatch is obvious to the prospect.

For each, name the agent's wrong behavior and the business cost.

### 3. Gap-analysis risks

Under what conditions is a top-quartile practice a bad benchmark?

- **Deliberate strategic choice.** The prospect's CTO explicitly chose not to do what the top quartile does (e.g., keeps AI work private as competitive moat). Citing the absence is condescending.
- **Sub-niche irrelevance.** A practice universal in enterprise SaaS may be irrelevant in dev-tools-for-engineers. Sector granularity matters.

One paragraph per risk, with an example drawn from your actual data.

### 4. Brand-reputation comparison (unit economics)

If 1,000 signal-grounded emails go out and 5% contain factually wrong signal data:
- 50 emails with wrong claims.
- Reply rate gain over generic: (7–12%) − (1–3%) = ~5–9 percentage points, so 50–90 extra replies.
- Estimated brand damage per wrong-signal email: state an explicit assumption (e.g., "we assume each wrong-signal email costs $X in lifetime-account value, based on a Y% probability that this founder or their network remembers the mistake on the next deal cycle").
- Net calculation: is the trade worth it?

Show the math explicitly. The rubric rewards explicit assumptions even if debatable; it penalizes implicit ones.

### 5. One honest unresolved failure

Pick one probe from `probe_library.md` that you did not fix. State:
- What it is.
- Why you did not fix it.
- What happens in deployment if you ship anyway.
- What it would take to fix.

This section is where honesty buys credibility. Hiding a known failure is worse for the grade than naming it.

### 6. Kill-switch clause

The specific trigger metric, threshold, and rollback condition.

Example: "Pause the system if (a) reply rate over any 7-day window drops below 2%, OR (b) more than one wrong-signal complaint reaches Tenacious via any channel in a 14-day window, OR (c) cost per qualified lead exceeds $8 over any 7-day window. Rollback: revert outbound to manual partner-sourced mode; retain enriched prospects in HubSpot with the synthetic flag."

## Evidence graph (ships alongside memo)

`evidence_graph.json` schema:

```json
{
  "claims": [
    {
      "claim_id": "c1",
      "claim_text": "Pass@1 on held-out slice is 47%",
      "page": 1,
      "section": "tau2_bench_table",
      "source_type": "trace_file | invoice | published | seed_material",
      "source_ref": "held_out_traces.jsonl#condition=your_method",
      "value": 0.47,
      "ci_95": [0.43, 0.51]
    }
  ]
}
```

Every numeric claim in the memo appears in this file. The automated grader counts uncited claims.

## README for the inheriting engineer

Ships at repo root. It must include:
- Architecture diagram
- Setup instructions (sandboxed + production variants)
- **Kill-switch flag documentation.** Default-unset, routes all outbound to staff sink. Explicit warning about running against real prospects.
- Data-handling policy acknowledgment.
- A "draft" marker requirement in Tenacious-branded metadata.

## Never do this

- Never extrapolate an internal Tenacious number beyond what the seed materials provide. If the seed says ACV is $240–720K, the memo may cite $240–720K — not $500K (unless you show the weighting with a source).
- Never run the memo math on dev-slice scores. Held-out only.
- Never omit a CI on a pass@1 number.
- Never leave a claim without an `evidence_graph.json` entry.
- Never exceed two pages. Cut content before you cut precision.
