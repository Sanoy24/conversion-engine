# Target Failure Mode — Signal Over-claiming

One failure mode is selected as the Act IV attack surface. This document names
it, derives its business cost in Tenacious terms, and explains why it is the
highest-ROI lever available inside the constraints of the challenge week.

## The target

**Signal over-claiming**: the agent asserts a hiring, funding, AI-maturity, or
competitor-gap fact whose underlying evidence does not support assertion.

Concretely this is the failure that fires across probes **P007–P011, P029–P030,
P032, P035–P037, and (with a structural parallel) P023–P025 on τ²-Bench retail**.

The family shares one pattern:

> When per-signal confidence is LOW (or evidence is weak), the drafter still
> renders the signal as an ASSERT in the prompt, and the LLM then propagates it
> into the prospect-visible email body.

## Why this failure and not another — alternatives comparison

The 37 probes split into three actionable failure families. For each one, we
derive the cost of leaving it unaddressed and the expected benefit of a
mechanism targeting it in Act IV.

### Alternative A — ICP classifier bugs (probes P002, P005)

- **Scope**: Deterministic ordering and threshold errors in
  `agent/core/icp_classifier.py`.
- **Fix shape**: 5-line patches (explicit tie-break; headcount floor for
  Segment 4). No mechanism per se.
- **Addressable loss**: `~1% of volume × $66K × 0.5 reply-rate drop ≈
  $330/1K emails`. Across 10K emails/year = **$3.3K/year**.
- **τ²-Bench pass@1 lift**: Zero — this code path is not exercised by
  τ²-Bench retail at all.
- **Why this is not the target**: The rubric for Act IV requires a mechanism
  with 95% CI separation on the held-out slice. A deterministic classifier
  patch produces no measurable held-out delta. The $3.3K/year ceiling also
  puts a hard cap on the business value.

### Alternative B — Bench over-commitment (probes P012, P013, P014)

- **Scope**: `_check_bench_match()` is called without `required_stacks`
  (orchestrator line 102), so stack-specific bench guards never fire.
- **Fix shape**: Infer required stack from enrichment (AI-adjacent roles →
  `ml`; data-platform → `data`) and pass to `_check_bench_match`. Pass the
  resulting `BenchMatch` into the drafter prompt.
- **Addressable loss**: High per-incident ($240K per rescinded staffing
  promise) but the incident rate is low — probably 1 in 200 drafts, because
  most drafts don't promise specific staffing. `$240K × (1/200) = $1.2K
  amortized per email`. Over 10K emails/year = **$12K/year** expected; the
  variance is huge (one big incident = $240K).
- **τ²-Bench pass@1 lift**: Zero — unrelated to retail tasks.
- **Why this is not the target**: Same held-out-delta problem as A. Worth
  fixing (and we ship it in the Act IV code alongside SCAP), but the probe
  classification shows it as a structural bug rather than a mechanism.
  High variance also makes it a poor measurement target at our volume.

### Alternative C — Target: Signal over-claiming (SCAP) ✓

- **Scope**: 12 probes (P007–P011, P029–P030, P032, P035–P037, plus the
  τ²-Bench dual-control analog P023–P025). One mechanism addresses all.
- **Fix shape**: Pre-prompt transform that converts LOW-confidence signals
  into ASK directives and filters LOW-confidence gap entries before the
  drafter sees them. Same principle applied in τ²-Bench: ASK before
  destructive tool call.
- **Addressable loss**: ~$67K/week at pilot volume (arithmetic below). At
  50 weeks of operation = **~$3.4M/year** expected.
- **τ²-Bench pass@1 lift**: +3pp target (95% CI separation) on held-out slice.
- **Why this wins**: The only family where one mechanism moves both the
  benchmark metric (measurable Delta A) and the Tenacious-deployment metric
  (brand-cost per email) simultaneously. The unit economics are also 100×
  the next alternative.

### Head-to-head summary

| Alternative | Expected $/year at scale | τ²-Bench delta | Act IV viability |
|---|---|---|---|
| A (ICP bug fixes) | $3.3K | 0 | Low — no held-out delta |
| B (bench-stack guard) | $12K expected (high var) | 0 | Low — no held-out delta |
| **C (SCAP over-claim gate)** | **~$3.4M** | **+3pp target** | **High — measurable on both axes** |

The third class is the only one where one mechanism addresses multiple P0
probes AND produces a measurable τ²-Bench delta on the sealed held-out slice.

## Why τ²-Bench retail measures this failure

τ²-Bench retail's central failure mode is *dual-control coordination*: the
agent either proceeds without user confirmation on destructive actions
(cancel_order, modify_address) or authenticates lazily. Structurally this is
the same miscalibration: **the agent asserts a course of action when the
evidence from the conversation does not yet support it.**

In the Tenacious drafter, the action is a factual claim in an email body. In
τ²-Bench retail, the action is a tool call. In both cases:

- The agent has access to an evidence set (signal brief / conversation state).
- The evidence set contains a confidence-like property (our `Confidence` enum /
  τ²-Bench's policy preconditions).
- Safe behavior is ASK-then-ACT.
- The observed failure is ACT-then-explain or ACT-then-apologize.

The SCAP mechanism (next section) is a pre-prompt transform that forces
ASK-then-ACT discipline regardless of the agent's free-generation bias.

## Business-cost derivation (Tenacious-specific)

### Direct brand cost per wrong-signal email

Per the challenge brief Skeptic's Appendix prompt (lines 463–464), the memo
must explicitly price the reputation cost of a wrong-signal email. We adopt
the following unit economics:

| Quantity | Value | Source |
|---|---|---|
| Cold emails in one pilot week | 200 | Challenge brief, line 56 (~60/person/week × 3-person team floor; we round up to 200 for the pilot) |
| Wrong-signal rate (our LLM-sampled trigger rate, category aggregate) | 10% | Probes P007–P011 aggregate |
| Brand-reputation cost per wrong-signal email | $50 | Our explicit assumption — a fraction of the expected-loss of one trust-damaged contact |
| Conversion loss per wrong-signal email | `E[deal] × p(trust-kill) = $66K × 0.8` | Derived below |

`E[deal] = mid(ACV_TAL) × disco_to_proposal × proposal_to_close
        = $480K × 0.425 × 0.325 ≈ $66K` (challenge lines 114–116).

`p(trust-kill | wrong-signal) = 0.8` — assumption based on the challenge's
explicit stance that over-claiming damages brand more than silence (line 104).

### Per-week cost at the baseline rate

```
wrong_signal_emails = 200 × 0.10 = 20
direct_brand_cost   = 20 × $50       = $1,000
expected_conv_loss  = 20 × $66K × 0.05 lifetime-contact-attrition
                    = $66,000 in expected pipeline
total_weekly_cost   = $1,000 + $66,000 = ~$67K/week at full pilot volume
```

The $66K number dominates. Even a 2× reduction in over-claim rate is worth
~$33K/week in expected pipeline.

### Act IV target: reduce over-claim rate from 10% to ≤ 3%

A 7-percentage-point reduction, at the weekly cadence above, saves:

```
saved_emails   = 200 × 0.07 = 14
saved_pipeline = 14 × $66K × 0.05 = ~$46K/week in expected-value
                 (+ ~$700/week in direct brand cost)
```

This is the unit benefit of the SCAP mechanism on the production pipeline. The
τ²-Bench held-out pass@1 delta is the evaluable proxy for the same mechanism.

## τ²-Bench delta target

On the retail dev slice we are at `0.7267 pass@1` with a 95% CI of
`[0.6504, 0.7917]`. Of the 150 simulated trials:

- ~5 task-trial pairs show destructive-action-without-confirm (P023 pattern).
- ~2 show auth-skip (P024).
- ~1 shows info-fabrication (P025).

Each recovery is worth roughly 0.5 pass@1 on that task-trial (partial credit).
Total recoverable signal ≈ `8 × 0.5 / 150 = ~0.027` pass@1 on the dev slice if
every such failure converts.

We aim for **+3 percentage points on the held-out slice** (20 tasks × 5 trials
= 100 sims). That is achievable if SCAP converts 3–4 of the expected ~5 dual-
control failures in 100 sims. 95% CI separation requires the method variance
across trials to be small enough; we address that with temperature=0.0 on the
held-out runs.

## The mechanism — SCAP (Signal-Confidence-Aware Phrasing)

Specification (full design in `method.md` after Act IV implementation):

1. **Pre-prompt transform.** Before the drafter LLM call, strip any signal from
   the user prompt whose `confidence == Confidence.LOW`. Replace each stripped
   signal with an explicit ASK directive. For `Confidence.MEDIUM`, downgrade
   ASSERT phrasing to SOFT-ASSERT ("looking at the public signal, it seems…").
2. **Gap-brief filter.** Drop any `GapEntry` with `confidence == LOW` from the
   gap_brief before the drafter sees it. If 0 gaps pass, skip the "Lead with
   the gap" instruction entirely.
3. **τ²-Bench analog.** For τ²-Bench retail, inject a system-prompt postscript
   that says: *"If the user has not explicitly authorized a destructive action
   (cancel, modify, refund, exchange) in this turn, you MUST ask for
   confirmation before calling the corresponding tool."* This is the
   conversation-level analog of the signal-confidence gate.
4. **No additional LLM call.** SCAP is deterministic string transformation
   inside the existing prompt-builder path. Delta B (vs GEPA baseline) is
   therefore a fair comparison on compute budget — we pay nothing extra.

## Why this is the best target and not a combination

The challenge Act IV deliverable requires *one* mechanism with a clean
statistical test. Attacking multiple unrelated failures dilutes the signal
(each individual effect becomes smaller than its CI). SCAP wins because:

- It is one mechanism that fixes multiple P0 probes, so the aggregate effect on
  held-out pass@1 is measurable.
- It is implementable in ~100 lines of pure-Python string work plus two
  prompt-template additions.
- It has zero additional LLM cost so Delta B (vs automated-optimization) is a
  fair compute-equalized comparison.
- The business-cost story is clean and survives the evidence-graph audit.

## One failure this target does not fix

P034 (gap entry's `prospect_has_it` is a bare bool with no confidence). This is
a data-model change, not a prompt-transform. We name it in the Skeptic's
Appendix as the honest unresolved failure.

## What the Act IV artifact set will be

- `method.md` — design rationale, exact prompt-transform rules, hyperparameters.
- `ablation_results.json` — pass@1, 95% CI, cost-per-task, p95 latency for
  (Day-1 baseline, SCAP, GEPA-style few-shot) on the sealed held-out slice.
- `held_out_traces.jsonl` — raw traces from all three conditions.
- Statistical test (paired bootstrap over per-task mean rewards) with p < 0.05.
