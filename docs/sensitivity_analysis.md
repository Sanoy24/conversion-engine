# Sensitivity Analysis — Signal Parameters

This document analyzes how sensitive the Conversion Engine's output (classification and tone) is to variations in its core enrichment parameters. Understanding these thresholds is critical for tuning the system to balance opportunity capture against brand risk.

## 1. Job-Post Velocity (`Delta_60d`)

The job-post velocity signal is a primary trigger for the **Segment 1 (Recently Funded / Scaling)** pitch.

### Parameter: Lookback Window (Currently 60 Days vs 120 Days)
- **If shortened to 30 days**: The delta becomes highly volatile. A single batch-hiring event for interns might look like "aggressive scaling," leading to false-positive Segment 1 pitches. 
- **If lengthened to 90 days**: The delta smooths out, but Tenacious misses the critical "pain window" where internal recruiters are overwhelmed.
- **Current Choice (60 days)**: Represents the optimal balance. It captures true structural scaling while filtering out month-to-month noise.

### Parameter: Volume Threshold
- **If we assert "scaling aggressively" at +2 roles**: High brand risk. A CTO hiring 2 engineers does not feel "overwhelmed." Pitching talent outsourcing here feels tone-deaf.
- **Current Implementation**: The drafter is instructed to use "ASK" phrasing if open roles < 5. "ASSERT" is reserved for high volume (e.g., +10 roles).

## 2. AI Maturity Scoring Weights

The AI maturity score (0-3) determines whether a prospect receives a **Segment 4 (Capability Gap)** pitch.

### Parameter: Weight of "AI-Adjacent Roles"
- **Current Weight: HIGH**. (If present, score jumps to ≥2).
- **Sensitivity**: This is the most sensitive parameter in the system because job roles indicate actual budget expenditure.
- **If downgraded to MEDIUM**: Many legitimately mature companies would score a 1, missing out on high-margin Segment 4 consulting pitches.
- **Risk Mitigation**: The score is paired with a Confidence metric. If a score of 2 is achieved *only* via low-quality roles (e.g., "Data Analyst"), confidence drops to LOW, and the system hedges the pitch.

### Parameter: Weight of "Executive Commentary"
- **Current Weight: MEDIUM**.
- **Sensitivity**: CEOs frequently mention AI on earnings calls regardless of actual engineering capability (the "loud but shallow" failure mode).
- **If upgraded to HIGH**: The system would over-classify companies as AI-mature, leading to highly technical Segment 4 pitches hitting teams that only use ChatGPT wrappers. The brand damage here is catastrophic. Medium weight correctly positions this as supporting evidence rather than primary evidence.

## 3. Competitor Gap Cohort Size

The competitor gap brief compares the prospect to top-quartile peers.

### Parameter: Cohort Size (Currently 5-10)
- **If reduced to 3**: Statistical noise dominates. A single outlier competitor skewing their stack heavily towards ML will falsely make the prospect look "behind."
- **If increased to 20**: The scraper latency increases linearly (~30s additional time), and the definition of "peer" becomes too loose, resulting in generic insights.
- **Current Choice**: 5-10 provides enough statistical grounding to make the "top-quartile" claim verifiable without incurring prohibitive scraping latency or token costs.

## 4. Tone-Preservation Check Threshold

The secondary LLM call evaluates the draft against the Tenacious style guide.

### Parameter: Pass/Fail Threshold
- **If set too strict**: The system auto-rejects drafts constantly, driving up OpenRouter costs and stalling the pipeline.
- **If set too loose**: Generic, sales-y "b2b spam" emails slip through, damaging Tenacious's premium brand.
- **Current Tuning**: The threshold targets a <5% rejection rate on the baseline model, optimizing for catching extreme deviations (e.g., "Synergize your paradigms") while passing standard variations.
