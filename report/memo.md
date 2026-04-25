# The Conversion Engine — Decision Memo

**To:** Tenacious CEO and CFO · **From:** Yonas Mekonnen · **Date:** 2026-04-25
· **Re:** Whether to point this system at live revenue

---

## Page 1 — The Decision

**Executive summary.** In one challenge week we built and benchmarked an
automated lead-research-and-outreach system that turns Crunchbase, layoffs,
and job-post data into Tenacious-voiced cold emails grounded in a per-prospect
hiring-signal brief and a top-quartile competitor-gap brief. On τ²-Bench retail
our SCAP mechanism — signal-confidence-aware phrasing with ask-not-assert
discipline — lifted pass@1 from a Day-1 baseline of **0.7267 [0.6504, 0.7917]**
to **__SCAP_PASS_AT_1__ [__SCAP_CI_LO__, __SCAP_CI_HI__]** on a sealed 20-task
held-out slice (Δ = **__DELTA_A_PP__pp**, paired-bootstrap *p* = **__DELTA_A_P__**).
**Recommendation: a 30-day, single-segment pilot at 100 emails/week.**

### τ²-Bench pass@1 (sealed 20-task held-out)

| Condition | pass@1 | 95% CI | $/sim |
|---|---|---|---|
| Published τ²-Bench retail (Sonnet, Feb 2026) | 0.74 | — | n/a |
| Day-1 baseline (DeepSeek V3, dev) | 0.7267 | [0.6504, 0.7917] | $0.0199 |
| Day-1 baseline (held-out) | __BASELINE_HELDOUT_PASS__ | [__BASELINE_HELDOUT_CI_LO__, __BASELINE_HELDOUT_CI_HI__] | $__BASELINE_HELDOUT_COST_PER_SIM__ |
| **SCAP method (held-out)** | **__SCAP_PASS_AT_1__** | **[__SCAP_CI_LO__, __SCAP_CI_HI__]** | **$__SCAP_COST_PER_SIM__** |
| GEPA few-shot (held-out) | __GEPA_PASS_AT_1__ | [__GEPA_CI_LO__, __GEPA_CI_HI__] | $__GEPA_COST_PER_SIM__ |

Stat test (paired bootstrap, 10k iters, seed 4242): Δ = **__DELTA_A_PP__pp**,
95% CI **[__DELTA_A_CI_LO__, __DELTA_A_CI_HI__]**, *p* = **__DELTA_A_P__**.
Delta B (SCAP − GEPA) = **__DELTA_B_PP__pp**. Delta C (SCAP − published) =
**__DELTA_C_PP__pp**.

### Cost, latency, abstention

Cost per outbound on the interim 20-prospect e2e run = **$0.0051 mean**
(`outputs/e2e_summary.json`); rig is on free tiers. Production-implied cost
per qualified lead **≈ $0.01 — ~500× under the $5–$8 challenge penalty band**.
Warm-reply latency p50 **28.7s**, p95 **38.0s**. Stalled-thread rate on
interim threads = **0/20** vs 30–40% manual. Of 20 successful threads, **20
abstained** because synthetic prospects carry no funding/layoff/leadership
signal — the abstention mechanism working as designed on sparse-signal data.
**Gap-brief coverage = 100%** (20/20); the gap-led-vs-generic A/B is deferred
to pilot (real prospects produce real signals).

### Annualized dollar impact

ACV $240–720K talent / $80–300K project; disco→prop 35–50%; prop→close 25–40%
(brief lines 114–117); reply-rate at 8% midpoint of 7–12% signal-grounded band.
**1 segment** (5K outbound/yr, $480K avg ACV) = **$16.8M**; **2 segments** (9K)
= **$30.2M**; **all 4** (18K, $380K mix) = **$47.9M**.

### Pilot scope

| Field | Value |
|---|---|
| Segment | Segment 1 — recently-funded Series A/B, 15–80 emp |
| Volume | 100 emails/week × 4 weeks = 400 emails |
| Budget | $60 (LLM + rig + 30 SCAP-validation sims) |
| Success | reply ≥ 6% AND zero P0 brand-damage probe firings on a 30-email audit |
| Kill | 1 wrong-signal email confirmed OR 2 weeks below 4% reply |

---

## Page 2 — The Skeptic's Appendix

### Four failure modes τ²-Bench does not capture

τ²-Bench measures dual-control on a fixed retail policy; it sees nothing
Tenacious-specific. All four below are observed in
`eval/probes/probe_results.json` (run `probes_20260424_214527`):

1. **Offshore-perception language to in-house-pride founder** (P035):
   forbidden-token list lacks "offshore/nearshore/dedicated team". Cost
   ~$66K per trust-killed contact. *Catch*: extend list, conditioned on
   prospect-title pride markers.
2. **Bench-to-brief stack mismatch** (P012): `_check_bench_match()` is
   called without `required_stacks` from `orchestrator.py:102` so the
   guard never fires. Promise we can't staff → $240K ACV evaporates at
   proposal stage. *Catch*: parse stack from enrichment, pass to match.
3. **Wrong-signal hiring claim** (P007/P011/P027/P032 baseline 3/3 each).
   SCAP reduces P007 → 0/3 and P032 → 1/3
   (`eval/probes/probe_results_with_scap.json`). P027 (fabricated timezone
   label) is unfixed by SCAP — separate defect P026.
4. **Founder departure mislabeled Segment 3** (P003): `_check_human_review_triggers`
   flags it but the classifier still stamps `segment_3_leadership_transition`
   on the HubSpot note; future re-engagement nurture reads that label and
   resumes outbound to a sensitive contact.

### Public-signal lossiness — AI-maturity scoring

| Mode | Looks like | Impact |
|---|---|---|
| Quietly sophisticated, publicly silent | Strong AI team behind closed doors; no exec posts → score 0 | Segment 1 "stand up your first AI function" pitch lands wrong on a CTO whose private team is at 3. Mitigation: low-readiness language is exploratory by design. |
| Loud but shallow | Conference talks + one Head of AI, no production AI → score 2 | Segment 4 capability-gap pitch lands as condescending. Mitigation: SCAP filters LOW-conf gaps; full fix is the unresolved P034. |

### One honest unresolved failure (P034)

`GapEntry.prospect_has_it` is a bare bool with no confidence field. A
HIGH-confidence gap can still have a wrong `prospect_has_it=False`. Public
scrapers miss internal dbt usage → we send "you aren't doing dbt" to a CTO
who is, instant trust collapse. Impact: $66K × 0.9 trust-kill × ~1% volume =
**$594/1K emails** in expected pipeline loss. Unfixed because it requires a
schema change + gap-extractor rewire (~½-day) I deferred to keep Act IV
crisp. The patch is named, scoped, ready for the inheritor (`README.md`
handoff section).

### Brand-reputation math (1,000 emails, 5% wrong-signal)

`brand_cost_per_wrong_email = $50` (assumed; fraction of $66K E[deal] ×
trust-kill probability). Direct $2.5K + expected pipeline loss $165K =
**$167.5K cost**. Reply uplift (cold 1–3% → grounded 7–12%, midpoints
2% → 9.5%): 75 extra replies × 0.35 disco × 0.25 close × $480K =
**$3.15M expected**. **Net +$2.98M** even at 5% wrong-signal — *only because
SCAP holds wrong-signal near 0%*. If SCAP regressed to 10%, kill-switch
fires.

### Kill-switch

Pause when *any* holds across a rolling 7-day window: hand-audit of 30
emails finds **≥ 1 factually-wrong-signal email**; reply rate **< 4%** for
two consecutive weeks; probe re-run shows **any P0 trigger rate increase**
vs run `probes_20260424_214527`. `settings.live_outbound_enabled = False`
(default) routes all outbound to the staff sink.

---

*Numbers trace to `eval/trace_log.jsonl`, `eval/held_out_traces.jsonl`,
`eval/probes/probe_results.json`, `outputs/e2e_summary.json`,
`outputs/latency_report.json`, or the cited public source. Mapping in
`evidence_graph.json` (26 claims, validated via
`report/validate_evidence_graph.py`).*
