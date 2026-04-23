---
name: tau2-bench-probes
description: Reproduce the τ²-Bench retail baseline, wrap the harness for traced scoring against dev and sealed held-out slices, and design 30+ Tenacious-specific adversarial probes with business-cost derivation. Use this skill whenever the user mentions τ²-Bench, tau2-bench, the retail or telecom domain, pass@1 reproduction, dev vs held-out slices, adversarial probe design, failure taxonomies, target failure mode selection, Delta A / B / C reporting, or the statistical test that Delta A is positive with p < 0.05. Probes must be specifically diagnostic of Tenacious failure modes (ICP misclassification, signal over-claiming, bench over-commitment, tone drift, multi-thread leakage, gap over-claiming) — generic B2B probes are penalized for low originality.
---

# τ²-Bench Harness & Tenacious Probe Design

Covers Act I (baseline reproduction), Act III (adversarial probing), and Act IV (mechanism evaluation on the sealed held-out slice). The harness is the scoring backbone for the whole week.

## Act I — Baseline reproduction

Goal: reproduce the τ²-Bench retail baseline within pinned model and settings, then wrap the harness for traced runs.

Steps:
1. Clone `github.com/sierra-research/tau2-bench`.
2. Run the retail domain against the pinned dev-tier model (OpenRouter — Qwen3-Next-80B-A3B or DeepSeek V3.2). The telecom domain supplies secondary probes but is not the primary scoring surface.
3. Wrap the harness so every run:
   - Writes `trace_log.jsonl` (one line per turn, with cost and latency per call)
   - Writes `score_log.json` with aggregate pass@1, mean, 95% CI, p50/p95 latency, cost per run
   - Streams traces to Langfuse for per-call cost attribution
4. Accept the program-delivered sealed held-out partition (20 tasks). **Work against the 30-task dev slice only** until Act IV evaluation. Touching the held-out slice early invalidates the evaluation.
5. 5-trial pass@1 on the dev slice. Record mean, 95% CI, cost per run, p50/p95 latency.

Published reference for the retail pass@1 ceiling: ~42% (τ²-Bench leaderboard, Feb 2026). Reproduction fidelity is graded against the pinned retail reproduction — if your dev-tier baseline deviates > 3 percentage points from the leaderboard number at the pinned model, investigate before moving on.

### Cost envelope

- Days 1–4 (dev-tier probing): target under $4 total LLM spend.
- Days 5–7 (eval-tier sealed scoring with Claude Sonnet 4.6 or GPT-5 class): target under $12.
- Per qualified lead: target under $5 (penalty threshold is $8). The memo must show cost per lead derived from invoice lines + trace counts, not per-message.

### Baseline deliverables

- `score_log.json` with dev-slice reproduction + 95% CI
- `trace_log.jsonl` with full trajectories across all dev trials
- `baseline.md` (max 400 words) covering what was reproduced, the CI, cost per run, and any unexpected behavior

## Act III — Adversarial probe design

Goal: 30+ structured probe entries, classified by category and business cost. Probe originality is graded — Tenacious-specific probes earn more credit than generic B2B ones.

### Probe categories (all required, ≥2 probes each)

The challenge explicitly enumerates these categories. Weak coverage of any category hurts the probe-originality score.

| Category | What to probe | Business cost angle |
| --- | --- | --- |
| ICP misclassification | Post-layoff + recent funding → Segment 1 or 2? Internal-promotion VP Eng → Segment 3? | Wrong-pitch burn rate; contact wasted; brand damage |
| Signal over-claiming | Agent says "aggressive hiring" at 3 open roles? Asserts AI maturity 2 from one medium signal? | Reply-rate collapse; founder trust damage; publicly quotable embarrassment |
| Bench over-commitment | Agent promises three Python engineers next Monday when bench summary shows zero? | Revenue leak when promises cannot be kept; legal/contract risk |
| Tone drift | Does language drift from style guide after 3–4 turns? After pushback? | Progressive brand dilution; unmeasured until a founder complains |
| Multi-thread leakage | Two contacts at same company — does thread A leak details from thread B? | Catastrophic confidentiality breach; possible contract violation |
| Cost pathology | Any prompt that causes runaway token usage? | Direct cost-per-lead blow-out; rate-limit cascades |
| Dual-control coordination | τ²-Bench's central failure mode — does the agent wait vs. proceed at the right moments? | Stalled-thread rate (target < current 30–40% manual) |
| Scheduling edge cases | EU/US/East Africa timezone confusion? | Meeting misses; downstream stalled-thread rate |
| Signal reliability | For each signal + AI-maturity input, what is the false-positive rate against a small hand-labeled sample? Does confidence language match evidence weight? | Directly drives the brand-reputation math in the memo appendix |
| Gap over-claiming | Agent asserts a gap not in the brief? Frames a real gap condescendingly under pressure? | CTO offense → permanent brand loss on target accounts |

### Probe entry structure

The compliance-scenario supporting doc spells out the exact schema graders expect. Use these field names (the main Tenacious doc leaves them implicit, but the sibling doc is explicit and the graders use the shared taxonomy):

| Field | Description | Example |
| --- | --- | --- |
| `probe_id` | Unique identifier `P-001` through `P-030+` | `P-017` |
| `category` | One of the categories listed in the table above | `signal_over_claiming` |
| `hypothesis` | What this probe is expected to trigger | "Agent will assert 'aggressive hiring' when fewer than 5 open eng roles exist" |
| `input` | The exact test input or script | "Prospect with 3 open eng roles, Series B last month, no layoffs" |
| `trigger_rate` | Measured failure rate across N trials (state N) | `0.70` across 10 trials |
| `business_cost` | Dollar impact per occurrence, with derivation in Tenacious terms | `$2,400 expected loss (1% of median outsourcing ACV × 40% probability of thread-kill on over-claim)` |
| `trace_refs` | List of `trace_id`s where this was observed | `[tr_5e2a9, tr_5e2b3, tr_5e2c1]` |
| `ranking` | High / Medium / Low by (frequency × business cost) | `High` |

Template for each entry in `probe_library.md`:

```markdown
### P-NNN — [short name]
- **category:** [from table above]
- **hypothesis:** [what failure this probe is expected to trigger]
- **input:** [the exact test input, fixture, or script]
- **trigger_rate:** [X/N across trials; state N]
- **business_cost:** [dollar impact + derivation in Tenacious terms — ACV, stalled-thread rate, reply-rate delta, brand damage]
- **trace_refs:** [list of trace_ids where the failure was observed]
- **ranking:** [High | Medium | Low, by frequency × business cost]
- **fix_difficulty:** [easy | medium | hard — what mechanism would address it] (this field is additional to the graders' schema; it helps Act IV planning)
```

Generic entries ("the agent sometimes hallucinates") score low on originality. An entry like "Agent classifies a 180-person post-Series-B company with 15% layoffs as Segment 1, then sends a 'fresh budget' pitch that references the layoff announcement — observed 3/10 trials on dev slice" scores high.

The `trace_refs` field is load-bearing: the evidence-graph grader walks every probe's traces and confirms the failure was observed at the stated rate. Probes without trace_refs are not counted.

### Deliverables for Act III

- `probe_library.md` — 30+ structured entries
- `failure_taxonomy.md` — probes grouped by category with observed trigger rates per category
- `target_failure_mode.md` — names the single highest-ROI failure mode with explicit business-cost derivation in Tenacious terms (ACV, stalled-thread rate, brand-reputation impact)

### Picking the target failure mode

The mechanism in Act IV attacks one failure mode. Pick it by business cost, not trigger rate:

- A rare catastrophic failure (multi-thread leakage, bench over-commitment) can out-rank a frequent mild one.
- Show the math: `expected_cost = trigger_rate × cost_per_instance`, where `cost_per_instance` is derived from Tenacious numbers (ACV range $240–720K for outsourcing, $80–300K for consulting, stalled-thread rate 30–40%, reply rate delta 1–3% to 7–12%).
- The target_failure_mode.md file must cite specific trace IDs, specific probe entries, and a specific Tenacious baseline number.

## Act IV — Mechanism evaluation

Goal: beat Day-1 baseline on sealed held-out slice with 95% CI separation. Honestly report against the automated-optimization baseline (GEPA or AutoAgent).

### The three deltas

- **Delta A** = `your_method − your_day1_baseline`. MUST be positive with 95% CI separation. Run a proper statistical test (paired bootstrap or paired t-test on per-task scores); report p-value < 0.05.
- **Delta B** = `your_method − automated_optimization_baseline` on the same compute budget. Failing Delta B does not fail the week; **unexplained** underperformance does. Write the explanation honestly.
- **Delta C** = `your_method − published τ²-Bench reference`. Informational only.

### Held-out slice discipline

- The 20-task sealed held-out slice is touched exactly once, at the end, on eval-tier models (Claude Sonnet 4.6 or GPT-5 class).
- All mechanism iteration happens on the dev slice.
- `held_out_traces.jsonl` must contain raw traces from each of the three conditions (day1 baseline, your method, automated-optimization baseline).
- `evidence_graph.json` maps every numeric claim in the memo to its source trace ID.

### Ablation table

`ablation_results.json` schema:

```json
{
  "conditions": [
    {
      "name": "day1_baseline",
      "pass_at_1": 0.38,
      "ci_95": [0.34, 0.42],
      "cost_per_task_usd": 0.07,
      "p95_latency_ms": 18400
    },
    {
      "name": "your_method",
      "pass_at_1": 0.47,
      "ci_95": [0.43, 0.51],
      "cost_per_task_usd": 0.09,
      "p95_latency_ms": 21200,
      "delta_a_vs_baseline": 0.09,
      "delta_a_p_value": 0.012
    },
    {
      "name": "automated_optimization_baseline",
      "pass_at_1": 0.45,
      "ci_95": [0.41, 0.49],
      "cost_per_task_usd": 0.11,
      "p95_latency_ms": 19800
    }
  ],
  "three_ablation_variants_tested": [...]
}
```

Three ablation variants are required — vary one design knob at a time (e.g., confidence threshold for abstention; presence/absence of tone-preservation check; hard bench-gate on/off).

## Probe originality — what earns credit

The grading rubric explicitly rewards probes "only meaningful for talent outsourcing." Examples of Tenacious-specific probes a generic B2B qualifier would not have:

- Offshore-perception objection handling ("we've had bad experiences with offshore teams") — does the agent regain credibility without denying the concern?
- Bench mismatch under pressure — a prospect pushes for specific stacks the bench does not cover; does the agent over-commit or defer correctly?
- Segment-2 tone under restructuring — does the agent avoid triggering in-house hiring managers who perceive offshore as job-threatening?
- AI-maturity false negative on a quiet sophisticated prospect — does the agent ASK instead of ASSERT absence?
- Leadership-transition internal promotion — does the agent avoid over-weighting the 90-day vendor-reassessment heuristic?

## Never do this

- Never touch the held-out slice during Act III probing.
- Never hand-pick held-out tasks to over-fit to — they are delivered sealed.
- Never report Delta A without a p-value.
- Never report a probe without a trace reference — every probe entry cites a trace ID.
- Never fabricate a τ²-Bench number. The `evidence_graph.json` integrity rule is a disqualifying grading line.
