# Baseline - tau2-Bench Retail Reproduction

**Config.** Retail domain, model `openrouter/deepseek/deepseek-chat-v3-0324`, 5 trials x 30 tasks, temperature 0.0, max concurrency 4. The wrapper is `eval/harness.py`, which calls tau2, parses `results.json`, and writes `eval/score_log.json` plus `eval/trace_log.jsonl`.

**Status.** Act I status: complete. 150/150 simulations finished successfully.

**Result.** Baseline pass@1 = **0.093 +/- 0.012** with 95% CI range 0.082 to 0.105. Per-trial means: [0.1, 0.1, 0.0667, 0.1, 0.1]. Published retail reference is ~0.42 for a comparable model class.

**Reproduction.** Reproduction check: pass@1 0.093 +/- 0.012 (drift 0.000 from baseline).

**Cost and latency.** Total spend **$0.4458**, or **$0.002972 per simulation**. Per-task latency p50 **29.35s**, p95 **480.54s**. Total wall clock 2541s.

**Unexpected behavior.** The harness now forces the `openrouter/` prefix, UTF-8 child-process output, `--timeout`, and `--auto-resume`. If tau2 exits non-zero but leaves a `results.json`, we recover that run instead of discarding it.

**Artifacts.** `eval/score_log.json` has 4 entries, `eval/trace_log.jsonl` has 360 records, and raw simulations live under `eval/tau2-bench/data/simulations/eval_dev_tier_baseline_20260421_135541_dc1abd/`.

**Submission note.** Act I is only complete once the baseline run and reproduction check both finish cleanly enough to produce the required artifacts. Partial runs are useful for debugging, but not sufficient for submission.
