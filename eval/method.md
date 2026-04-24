# SCAP — Signal-Confidence-Aware Phrasing

Act IV mechanism design for the Tenacious Conversion Engine.
Attacks the failure mode named in
[eval/probes/target_failure_mode.md](probes/target_failure_mode.md):
**signal over-claiming / ask-not-assert discipline**.

## One-sentence description

SCAP is a deterministic pre-prompt transform that (a) in the Tenacious drafter,
strips or softens any signal whose `Confidence` is LOW before the LLM sees it
and (b) in τ²-Bench retail, injects an ask-before-destructive-action postscript
onto the stock agent system prompt. No additional LLM calls; no temperature
change; no fine-tuning; purely a prompt-shape intervention.

## Root-cause linkage to the target failure

The target failure — signal over-claiming in Tenacious and dual-control
coordination failure in τ²-Bench — share one root cause:

> The agent's prompt contains evidence the agent treats as assertable when it
> should not be. In Tenacious the "evidence" is a LOW-confidence signal value
> (a funding round with `confidence=LOW`, a gap with `confidence=LOW`, an AI
> maturity score derived from weak inputs). In τ²-Bench retail the "evidence"
> is the user's apparent request, which the agent treats as sufficient to act
> on without a confirmation or authentication round-trip.

The surface-level failures differ (email body over-claim vs. tool call without
confirm) but the root cause is a calibration gap between evidence strength and
action boldness. SCAP closes that gap in both environments with one rule.

## Where the mechanism lives in source

| Environment | File(s) | What SCAP does |
|---|---|---|
| Tenacious drafter (email) | [agent/core/scap.py](../agent/core/scap.py), wired into [email_drafter.py](../agent/core/email_drafter.py) | Pre-prompt transform that (1) strips LOW-confidence signals from the user prompt and replaces them with explicit ASK directives; (2) filters LOW-confidence gap entries out of the competitor gap brief; (3) downgrades MEDIUM-confidence signals from ASSERT to SOFT-ASSERT ("looking at the public signal, it seems..."). |
| τ²-Bench retail (sealed held-out eval) | [eval/tau2-bench/src/tau2/agent/llm_agent.py](tau2-bench/src/tau2/agent/llm_agent.py) — `system_prompt` property | Reads `SCAP_POSTSCRIPT` env var and appends it to the stock system prompt. When env var is unset, behavior is byte-identical to upstream tau2-bench. Our harness sets the env var only for the SCAP condition. |

## Hyperparameters (actual values used)

| Name | Value | Rationale |
|---|---|---|
| `TENACIOUS_LOW_CONF_STRIP` | `True` | Remove LOW-conf signal lines from user prompt entirely |
| `TENACIOUS_MED_CONF_SOFTEN` | `True` | Rewrite MEDIUM-conf claims as soft-assertions |
| `TENACIOUS_GAP_CONF_MIN` | `Confidence.MEDIUM` | Drop gap entries with `confidence < this` before drafter |
| `SCAP_TAU2_POSTSCRIPT` (retail) | See prompt block below (≈170 words) | Ask-before-destructive-action rule |
| `SCAP_APPLY_TO_ROUND1_ONLY` | `False` | Postscript active every turn — dual-control failures recur mid-conversation |
| `temperature` (held-out runs) | `0.0` | Minimise trial-to-trial variance for CI separation |
| `n_trials` (held-out runs) | `5` | Match Day-1 baseline trial count |
| `n_held_out_tasks` | `20` | Per challenge line 216 |

### The exact τ²-Bench retail postscript

The postscript is injected via the `SCAP_POSTSCRIPT` env var. Its content is
the single source of truth for the mechanism in the benchmark environment:

```text
<signal_confidence_aware_phrasing>
Ask-not-assert rule: when evidence is weak or incomplete, ASK rather than ACT.

Before calling any tool that modifies state (cancel_order, exchange_order,
modify_user_address, modify_pending_order, refund, update_order, return_items),
you MUST:
1. Confirm the user's identity has been authenticated in this conversation.
   If authenticate_user_by_email / find_user_id_by_email has not been called
   with a confirmed match in this thread, call it first or ask the user for
   the email or last-order-id needed.
2. Echo back the specific action and the specific parameters (order id, item
   id, amount) and wait for an explicit affirmative confirmation. A plain
   "yes" or "ok" counts; a restatement of an earlier request does not.
3. If the user has not given you the exact value for a parameter, ASK. Do
   not guess, default, or fabricate any id or amount.

This rule is a precondition for modify-type tool calls only. Read-only tools
(get_order_details, get_user_details, find_user_id_by_email) may be called
freely to resolve the above.
</signal_confidence_aware_phrasing>
```

## Three ablation variants

The ablations isolate each SCAP component so the main-method delta is
attributable to specific rule contributions rather than the postscript as a
monolithic hint.

| Variant | What changes from main SCAP | What it tests |
|---|---|---|
| **A — Identity-only** | Keeps rule 1 (authentication). Drops rules 2 and 3. | Is the delta driven by the authentication requirement alone? This is the most commonly-stated τ²-Bench failure mode in the literature. |
| **B — Confirm-only** | Keeps rule 2 (echo-then-confirm). Drops rules 1 and 3. | Is the delta driven by confirmation-before-action, independent of authentication? |
| **C — Parameter-ask only** | Keeps rule 3 (ask for missing parameters). Drops rules 1 and 2. | Is there a separate contribution from avoiding fabricated order/item ids? |
| **(main) — SCAP full** | All three rules active | The full mechanism. |
| **(baseline) — Day-1** | No postscript. Stock tau2 agent. | Control — how much of the delta is noise? |

Conditions are evaluated on the same 20 task IDs × 5 trials = 100 sims each.
That is 500 sims total. Per-sim cost ≈ $0.02 on DeepSeek V3 → total Act IV
LLM spend ≈ **$10**, inside the challenge budget.

## Statistical test plan

**Test**: paired bootstrap over per-task mean rewards. For each task ID `t`,
we compute `r_method(t)` and `r_baseline(t)` as the mean reward across the
5 trials of that condition. The paired delta `d(t) = r_method(t) −
r_baseline(t)` is sampled with replacement 10,000 times over the 20 tasks to
estimate the sampling distribution of the mean delta `D = mean(d(t))`.

**Comparison**: Delta A = `SCAP_full` − `Day-1 baseline`. The challenge
requires Delta A > 0 with 95% CI separation, and we also report a two-sided
p-value for `H0: D ≤ 0`.

**Threshold**: `p < 0.05` (one-sided) with the 95% CI lower bound above 0.

**Secondary reports**:

- **Delta B** = `SCAP_full` − `GEPA-style few-shot baseline` (automated-opt
  comparison, same compute budget). Informational; failing Delta B does not
  fail the week but triggers an explanatory paragraph in the memo.
- **Delta C** = `SCAP_full` − published τ²-Bench retail reference. Single-point
  comparison; no CI.
- **Per-variant deltas** (A, B, C ablations) reported with the same bootstrap
  procedure to isolate the contribution of each rule.

**Multiple-comparison correction**: we pre-register Delta A as the primary
endpoint; the ablation variants are reported without correction because they
are diagnostic, not confirmatory.

## Implementation checklist (what lands in source)

1. `agent/core/scap.py` — the Tenacious pre-prompt transform. Pure function:
   `(HiringSignalBrief, CompetitorGapBrief | None) → (HiringSignalBrief,
   CompetitorGapBrief | None, list[str] applied_transforms)`.
2. `agent/core/email_drafter.py` — call `apply_scap(...)` at the top of
   `draft_email()` under a feature flag `settings.enable_scap`. Default True.
3. `eval/tau2-bench/src/tau2/agent/llm_agent.py` — env-var hook (already
   landed; see the marked block in `system_prompt`).
4. `eval/harness.py` — `run_baseline(..., extra_env={"SCAP_POSTSCRIPT": ...})`
   (already landed). Orchestrator script picks the postscript variant by
   condition name.
5. `eval/run_heldout.py` — one-shot script that runs all five conditions on
   `eval/heldout_slice.json` and writes `eval/ablation_results.json` +
   `eval/held_out_traces.jsonl`.
6. `eval/scap_stats.py` — loads held-out traces, runs the paired bootstrap,
   writes the final stats block consumed by `report/memo.md`.

## Why this mechanism beats the alternatives (see target_failure_mode.md)

Recap from the target-failure-mode analysis:

- Alternative A (ICP classifier bug fixes) has no held-out delta; `$3.3K/yr`
  ceiling.
- Alternative B (bench-stack guard) has no held-out delta; `$12K/yr` expected
  with very high variance.
- **SCAP** moves both axes — Tenacious brand-cost per email *and* τ²-Bench
  held-out pass@1 — with one implementation. Expected Tenacious-side value at
  scale ≈ **$3.4M/yr**; target held-out lift +3pp with 95% CI separation.

## Known limitations of SCAP (carry forward to Skeptic's Appendix)

1. SCAP does not change per-signal confidence *values*. If the enrichment
   pipeline misreports confidence (e.g. marks a weak AI-maturity score as
   HIGH), SCAP passes the error through. This is the P031 false-negative
   scraper case in the probe library.
2. SCAP softens LOW-confidence signals but does not infer new ones. A
   quietly-sophisticated AI team with no public signal remains invisible.
3. SCAP's τ²-Bench postscript adds ~220 tokens per agent turn. For conver-
   sations with 30+ turns this compounds. Measured cost impact: ≈ +3% on the
   cost-per-task line item versus Day-1 baseline. Reported in
   `ablation_results.json`.
4. SCAP does not address the prospect_has_it data-model gap (P034). That is
   the explicit "one honest unresolved failure" in the Skeptic's Appendix.
