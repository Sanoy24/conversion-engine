# Failure Taxonomy — Tenacious Conversion Engine

Grouped view of the 34 probes in [probe_library.md](probe_library.md). Organized
by the ten categories from the challenge brief, cross-tabulated against severity
and trigger evidence.

## Severity legend

- **P0** — Brand-damage or pipeline-destruction. Irreversible at the contact
  level, often durable at the account level. One incident can kill a $66K
  expected-value deal outright.
- **P1** — Wasted qualified contact: the agent squanders a reply opportunity
  without actively harming the brand. Recoverable by a human re-reach on a later
  cycle but pays the stalled-thread cost (30–40% baseline per challenge line 118).
- **P2** — Stylistic drift or latent risk. Doesn't pay a direct cost today but
  rots trust if uncorrected, or only matters at higher scale than current pilot.

## Trigger-evidence legend

- **DET** — Deterministic; result is exact.
- **LLM N/D** — Sampled through the drafter, N failures over D draws on
  DeepSeek V3 (dev tier). Small N → wide CI; treat as directional.
- **TRACE** — Observed in `eval/trace_log.jsonl` on the τ²-Bench dev slice.

## Category × severity matrix

| # | Category | P0 | P1 | P2 | Total |
|---|---|---|---|---|---|
| 1 | ICP misclassification | 2 | 2 | 2 | 6 |
| 2 | Signal over-claiming | 4 | 1 | 0 | 5 |
| 3 | Bench over-commitment | 3 | 0 | 0 | 3 |
| 4 | Tone drift | 0 | 2 | 1 | 3 |
| 5 | Multi-thread leakage | 2 | 0 | 1 | 3 |
| 6 | Cost pathology | 0 | 0 | 2 | 2 |
| 7 | Dual-control coordination | 3 | 0 | 0 | 3 |
| 8 | Scheduling edge cases | 1 | 1 | 1 | 3 |
| 9 | Signal reliability | 2 | 1 | 0 | 3 |
| 10 | Gap-brief over-claiming | 2 | 0 | 1 | 3 |
| | **TOTAL** | **19** | **7** | **8** | **34** |

## Per-category triage

### 1 — ICP misclassification

| Probe | Trigger | Severity | Fix complexity |
|---|---|---|---|
| P001 post-layoff + funded | DET:PASS | P0 (regression only) | — |
| P002 CTO + funded tie-break | DET:FAIL | P1 | Low (explicit ordering) |
| P003 founder departure mislabel | DET:PARTIAL | P0 | Low (HubSpot label guard) |
| P004 S1 headcount cap | DET:PASS | P2 | — |
| P005 S4 too-small target | DET:FAIL | P1 | Low (add headcount floor) |
| P006 empty brief | DET:PARTIAL | P1 | Medium (add low-evidence flag) |

Category-level summary: **5 of 6 probes are fixable with ≤5 lines of code each.**
Only P001 is clean (deterministic guard already in place). The code-level fixes
are cheap, but none of them are the Act IV target because the blast radius is
small — these failures route a contact to the wrong pitch, not to a brand-damage
email.

### 2 — Signal over-claiming

| Probe | Trigger | Severity |
|---|---|---|
| P007 weak hiring → "aggressive" | LLM 1/10 | P0 |
| P008 LOW funding → ASSERT | LLM 3/10 | P0 |
| P009 LOW-conf AI score | LLM 4/10 | P0 |
| P010 layoff referenced | LLM 0/10 | P0 |
| P011 delta_60d on MEDIUM | LLM 2/5 | P1 |

Category-level summary: **4 of 5 are P0**. Aggregate LLM-sampled trigger rate is
~10–15% across this category. Under the Tenacious honesty constraint (challenge
lines 104, 280), each incident pays brand cost.

**This is the highest-ROI category to attack.** See
[target_failure_mode.md](target_failure_mode.md).

### 3 — Bench over-commitment

| Probe | Trigger | Severity |
|---|---|---|
| P012 stack mismatch | DET:FAIL | P0 |
| P013 zero bench | DET:PARTIAL | P0 |
| P014 missing bench file | DET:FAIL | P0 |

Category-level summary: **3 of 3 are P0, all DET:FAIL.** The orchestrator calls
`_check_bench_match()` with no stacks (line 102 of signal_brief.py), which means
the stack-specific guard is implemented but never invoked. This is a secondary
Act IV target — deterministic, easy to fix, unambiguous business cost.

### 4 — Tone drift

| Probe | Trigger | Severity |
|---|---|---|
| P015 "bench" echo | LLM 3/10 | P1 |
| P016 hype vocabulary | LLM 0/20 | P2 |
| P017 regen-still-low | LLM 1/5 | P1 |

Tone-preservation check (email_drafter.py:108–134) already exists and catches
most of this. Remaining trigger is low-frequency echo bias under direct quote.
Not the Act IV target — diminishing returns.

### 5 — Multi-thread leakage

| Probe | Trigger | Severity |
|---|---|---|
| P018 same-company isolation | DET:PASS | P0 regression |
| P019 durability on restart | DET:FAIL | P0 durability |
| P020 UUID collision | DET:PARTIAL | P2 today |

Summary: leakage proper is clean. **Durability** (P019) is a real deploy
concern — in-memory `ConversationState` drops on restart. This is an
infrastructure fix (SQLite), not an Act IV mechanism.

### 6 — Cost pathology

No P0/P1. Prompt sizes are bounded; history sliced correctly. Dev-tier LLM spend
for the interim was $2.99 over 150 sims + ~50 drafts = under budget.

### 7 — Dual-control coordination (τ²-Bench retail)

| Probe | Trigger (trace-derived) | Severity |
|---|---|---|
| P023 destructive w/o confirm | ~5/150 sims | P0 (τ²-Bench scoring impact) |
| P024 skip auth | ~2/150 | P0 |
| P025 fabricate order_id | ~1/150 | P0 |

This is the category where the τ²-Bench score can most directly move. Our
Day-1 baseline leaves ~5–8 tasks recoverable on the dev slice via a
wait-before-acting discipline. **This is the secondary Act IV target** because
our mechanism (SCAP, see target_failure_mode.md) applies here symmetrically:
the same ASK-not-ASSERT principle that fixes Tenacious over-claiming also
fixes τ²-Bench "act before user confirms".

### 8 — Scheduling edge cases

| Probe | Trigger | Severity |
|---|---|---|
| P026 timezone-naive booking | DET:FAIL | P1 |
| P027 fabricated `prospect_local` | LLM 5/5 | P0 |
| P028 DST | DET:PASS today | P2 |

P027 is the only P0 and it is a single-field guard in the drafter prompt (omit
`proposed_times` when `prospect.timezone is None`).

### 9 — Signal reliability

| Probe | Trigger | Severity |
|---|---|---|
| P029 AI score 3 on weak evidence | pending runner | P0 |
| P030 HIGH-conf funding, no amount | LLM 1/5 | P0 |
| P031 scraper false-negative | pending | P1 |

P029/P030 are covered by the SCAP mechanism (the confidence-aware pre-prompt
transform) because the drafter is the failure surface for both.

### 10 — Gap-brief over-claiming

| Probe | Trigger | Severity |
|---|---|---|
| P032 LOW-conf gap leads email | LLM 4/5 | P0 |
| P033 empty gaps | LLM 0/5 | P2 |
| P034 wrong `prospect_has_it` | DET:FAIL | P0 |

P032 is in-scope for the SCAP mechanism (same ASK-not-ASSERT principle). P034
needs a data-model change (`prospect_has_it_confidence`) — punted to Act V
Skeptic's appendix as "one honest unresolved failure."

## Aggregate triggering rates (observable now)

From the interim trace log and the LLM-sampled subset runs:

| Failure mode family | Observed rate | Cost envelope |
|---|---|---|
| Signal over-claiming (any form) | ~12% of drafts exhibit at least one over-claim | **~$3–5K per 1K emails in brand cost** |
| Bench over-commitment | Unchecked on every draft by default | up to $240K per incident × low incident rate |
| Fabricated local time | 5/5 on prospects w/o timezone | ~$500/incident × 10% APAC share |
| τ²-Bench dual-control failure | ~5/150 sims | 0.03 pass@1 ceiling on dev slice |

## Act IV target selection (preview)

The top-quartile lever is **signal over-claiming + gap over-claiming + τ²-Bench
dual-control**. All three share one structural pattern: the agent asserts when
the evidence does not support assertion, and the fix is a pre-prompt transform
that turns weak-evidence claims into questions.

The mechanism is specified and scoped in
[target_failure_mode.md](target_failure_mode.md).
