---
name: tenacious-email-drafter
description: Draft outbound, reply, and nurture emails in the Tenacious Consulting voice that ground every claim in the hiring signal brief and respect bench/pricing guardrails. Use this skill whenever the user asks to write a cold email, warm reply, re-engagement message, or discovery-call booking request for a Tenacious prospect, or when building the signal-confidence-aware phrasing mechanism or the tone-preservation check named in Act IV. Email is the primary channel for Tenacious (founders, CTOs, VPs Engineering live there); SMS is secondary for warm-lead scheduling only. The style guide's tone markers and the honesty rule (no assertion without grounding, ask when confidence is low) are brand constraints, not preferences.
---

# Tenacious Email Drafter

Produces email drafts that would pass the "a prospect would read with interest rather than discomfort" test. The drafter is a consumer of `hiring_signal_brief.json`, `competitor_gap_brief.json`, and the ICP classifier output — it does not enrich, it composes.

## Inputs

1. `hiring_signal_brief.json` (required)
2. `competitor_gap_brief.json` (required for outbound leading with a research finding)
3. ICP classifier output (required — determines which template variant to use)
4. `bench_summary.md` (required — capacity commitments are gated on this)
5. `style_guide.md` from the seed repo (required — tone markers are authoritative)
6. `pricing_sheet.md` (reference only; deeper pricing routes to a human)
7. Thread history if replying (stops multi-thread leakage — see below)

## The three email types

| Type | When | Primary goal |
| --- | --- | --- |
| Cold outbound (first touch) | No prior contact | Open with a research finding from the competitor gap brief. Hook is the gap, not the Tenacious pitch. |
| Warm reply | Prospect replied to any prior message | Preserve momentum. Propose a discovery call with two specific time windows. Offer SMS handoff for scheduling if the prospect shows urgency. |
| Re-engagement | 14+ days silent after a warm touch | Lead with new signal if one emerged; otherwise a low-pressure exit line ("worth a conversation later this quarter?"). |

## The honesty rule — the core constraint

Every factual claim in the email must trace to a field in the signal brief. If a field is null or `confidence: low`, the drafter must ASK rather than ASSERT.

Examples:

| Brief state | Wrong phrasing | Right phrasing |
| --- | --- | --- |
| `hiring.open_eng_roles: 3` | "You're scaling aggressively" | "I noticed a few open engineering roles — are you hiring ahead of a specific push?" |
| `ai_maturity.score: 2, confidence: low` | "Your AI practice is maturing" | "The signals I can see publicly point toward early AI investment — is that fair?" |
| `funding: null` | (inventing a funding event) | Do not mention funding at all |
| `layoffs.event: true, headcount_pct: 18` | "Given your recent layoffs, you need cheaper engineering" | Do not reference the layoff directly in a first touch. Let the pitch variant (Segment 2 language) carry the cost-discipline framing implicitly. |

## Signal-confidence-aware phrasing (Act IV mechanism)

This is one of the named mechanism directions in the challenge. Implement it as follows:

For each signal the email references, read the `confidence` field from the brief. Map to language mode:

- **`confidence: high`** → ASSERT mode. "You closed a $14M Series B in February and your open Python engineering roles tripled since then."
- **`confidence: medium`** → SOFT-ASSERT mode. "Looking at the public signal, it seems your Python engineering function has grown meaningfully in the last two months."
- **`confidence: low`** → ASK mode. "Is your engineering team expanding faster than your recruiting team can keep up?"

The mechanism is a template-selector that picks phrasing variants keyed on confidence. The ablation for the memo: measure reply rate with confidence-aware phrasing ON vs. OFF, reported as Delta A on the held-out slice.

## Bench-to-brief match — a hard constraint

The email MUST NOT commit to specific capacity that `bench_summary.md` does not currently show. This is the bench over-commitment failure mode and it is a disqualifying brand risk.

Allowed:
- "Happy to walk through how we'd staff this."
- "We have a bench of Python and data engineers — specific availability I'd confirm on a call."

Forbidden unless the bench summary literally shows it:
- "We can start three Python engineers next Monday."
- "We have five ML engineers available this quarter."

If a prospect asks directly for specific capacity in a reply and the bench does not show it, the email drafter emits a `handoff_to_human: true` flag in the output and drafts a neutral bridge message ("Let me confirm exact availability with our delivery lead and come back to you within the day").

## Pricing rule

Public-tier pricing bands from `pricing_sheet.md` may be quoted verbatim. Anything deeper (specific multi-year rates, custom bundles) must route to a human. The drafter emits `handoff_to_human: true` when the prospect asks a pricing question outside the published bands.

## Competitor gap opening (cold outbound only)

The strongest cold open leads with the research finding, not the vendor pitch. Structure:

1. **Line 1** — name one specific gap from the competitor gap brief. "Three of the five largest $REDACTED-stage SaaS companies have a named Head of AI on their public team page. Your team page lists engineering leadership but not an AI lead."
2. **Line 2** — the connection to action. "That is usually either a deliberate choice or a gap worth a thirty-minute conversation."
3. **Line 3** — the ask. "If it is the second, I can walk you through how we have helped three comparable teams stand up an AI function in six weeks." (Only if the bench supports it. Otherwise: "If it is the second, worth a call?")

Do NOT lead with Tenacious capability. The gap is the hook; the capability is the close.

## Style guide compliance

The tone markers in `style_guide.md` are the authority. Common drift patterns the tone-preservation check (Act IV mechanism) catches:

- **Over-formality.** "Pursuant to your recent fundraising event" is wrong. Tenacious voice is conversational and direct.
- **Over-familiarity.** "Hey team!" is wrong. The voice is warm but professional.
- **Hype words.** "Revolutionary," "cutting-edge," "10x" are all forbidden. Understatement reads as confidence.
- **Long paragraphs.** Two-sentence paragraphs maximum in cold outbound. Founders and CTOs skim.
- **Vague value props.** "We help companies scale" is forbidden. Every message must name a specific outcome or a specific next step.

Implement the tone-preservation check as a second LLM call that scores the draft against `style_guide.md` on a 0–1 scale. Regenerate if < 0.7. Cost the extra call in the memo (it is a real line item in the cost-per-lead math).

## Multi-thread leakage prevention

If the same prospect company has two contacts in-flight (e.g., a co-founder and a VP Eng), the drafter must NOT reference anything said in the sibling thread. Inputs explicitly include per-thread history; outputs are keyed on `thread_id`. Check the thread history for context before drafting, never the parent company's aggregated conversation log.

A probe that tests this: "Send a reply to contact A that references a detail only contact B mentioned." Correct behavior: ignore the sibling thread detail entirely.

## Scheduling edge cases (EU / US / East Africa)

When proposing times, read the prospect's timezone from Crunchbase HQ location. Propose two windows in the prospect's local time, with UTC in parentheses. Never propose a window that would be outside 08:00–18:00 prospect-local. Explicit rules:

- Prospect in EU, Tenacious delivery lead in East Africa → overlap is 10:00–17:00 EAT = 09:00–16:00 CET. Safe window: 10:00–15:00 prospect-local.
- Prospect in US (ET/PT), delivery lead in East Africa → overlap is 08:00–10:00 ET = 15:00–17:00 EAT. Propose two morning ET slots.
- Always include a Cal.com link as the fallback.

## Output schema

```json
{
  "thread_id": "string",
  "email_type": "cold | warm_reply | re_engagement",
  "subject": "string",
  "body": "string",
  "proposed_times": [{"prospect_local": "2026-04-22 10:00 CET", "utc": "2026-04-22 09:00 UTC"}],
  "calcom_link": "string",
  "grounded_claims": [
    {"claim": "Your open eng roles increased 60% in 60 days", "source_field": "hiring.delta_60d", "confidence": "high"}
  ],
  "handoff_to_human": false,
  "handoff_reason": "string | null",
  "tone_check_score": 0.84,
  "draft_metadata": {"generated_at": "ISO-8601", "marked_draft": true}
}
```

## Always mark drafts

Per the challenge data-handling policy, any Tenacious-branded output goes out with `marked_draft: true` in metadata. Tenacious reserves the right to redact.

## Never do this

- Never fabricate a quote, case study, or client name. The three redacted case studies in the seed repo are the only ones you may reference, and only as "a sector+size peer," never by name.
- Never commit to a start date without bench confirmation.
- Never send cold SMS — Tenacious prospects live in email. SMS is only for warm leads who replied at least once by email AND indicated scheduling urgency.
- Never cite a hiring signal that is null in the brief. Silence is preferred.
