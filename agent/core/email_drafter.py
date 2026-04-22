"""
Email Drafter — composes emails in the Tenacious voice.
Consumes signal brief, gap brief, and ICP classification.
Enforces: honesty rule, bench constraints, style guide compliance.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from agent.config import settings
from agent.llm import get_llm_client
from agent.models import (
    CompetitorGapBrief,
    Confidence,
    EmailDraft,
    EmailType,
    GroundedClaim,
    HiringSignalBrief,
    ICPClassification,
    ICPSegment,
    ProposedTime,
    TraceRecord,
)

logger = logging.getLogger(__name__)


async def draft_email(
    signal_brief: HiringSignalBrief,
    classification: ICPClassification,
    email_type: EmailType = EmailType.COLD,
    gap_brief: CompetitorGapBrief | None = None,
    thread_history: list[dict] | None = None,
    thread_id: str | None = None,
) -> tuple[EmailDraft, list[TraceRecord]]:
    """
    Draft an email grounded in the signal brief.

    Every factual claim must trace to a field in the brief.
    If a field is null or confidence: low → ASK rather than ASSERT.
    """
    traces: list[TraceRecord] = []
    thread_id = thread_id or f"thread_{uuid.uuid4().hex[:8]}"

    # Load seed materials for context
    style_guide = _load_seed_file(["style_guide.md", "style_guide_PLACEHOLDER.md"])
    _load_seed_file(["email_sequences.md", "email_sequences_PLACEHOLDER.md"])
    pricing_sheet = _load_seed_file(["pricing_sheet.md", "pricing_sheet_PLACEHOLDER.md"])
    bench_summary = _load_seed_file(["bench_summary.md", "bench_summary_PLACEHOLDER.md"])

    # Build the grounded claims list
    grounded = _extract_grounded_claims(signal_brief)

    # Build the system prompt
    system_prompt = _build_system_prompt(
        email_type=email_type,
        style_guide=style_guide,
        bench_summary=bench_summary,
        pricing_sheet=pricing_sheet,
    )

    # Build the user prompt with signal data
    user_prompt = _build_user_prompt(
        signal_brief=signal_brief,
        classification=classification,
        email_type=email_type,
        gap_brief=gap_brief,
        thread_history=thread_history,
        grounded_claims=grounded,
    )

    # Generate email via LLM
    llm = get_llm_client()
    result, trace = await llm.chat_json(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=2048,
        trace_event="email_draft",
        prospect_company=signal_brief.prospect.company,
        thread_id=thread_id,
    )
    traces.append(trace)

    # Run tone-preservation check
    tone_score = None
    if style_guide:
        tone_score, tone_trace = await _tone_check(
            result.get("body", ""),
            style_guide,
            signal_brief.prospect.company,
            thread_id,
        )
        traces.append(tone_trace)

        # Regenerate if score is too low
        if tone_score < 0.7:
            logger.warning("Tone score %.2f below threshold — regenerating.", tone_score)
            result, regen_trace = await llm.chat_json(
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        + "\n\nCRITICAL: The previous draft drifted from the Tenacious voice. Be more direct, conversational, and avoid hype words.",
                    },
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=2048,
                trace_event="email_draft_regen",
                prospect_company=signal_brief.prospect.company,
                thread_id=thread_id,
            )
            traces.append(regen_trace)

    # Check if we need human handoff
    handoff = False
    handoff_reason = None
    if signal_brief.requires_human_review:
        handoff = True
        handoff_reason = signal_brief.human_review_reason
    if classification.segment == ICPSegment.ABSTAIN:
        # Abstain → send generic exploratory instead
        pass  # The LLM prompt already handles this

    draft = EmailDraft(
        thread_id=thread_id,
        email_type=email_type,
        subject=result.get("subject", ""),
        body=result.get("body", ""),
        proposed_times=[ProposedTime(**t) for t in result.get("proposed_times", [])],
        calcom_link=result.get("calcom_link", f"{settings.calcom_base_url}/book"),
        grounded_claims=grounded,
        handoff_to_human=handoff,
        handoff_reason=handoff_reason,
        tone_check_score=tone_score,
        draft_metadata={
            "generated_at": datetime.utcnow().isoformat(),
            "marked_draft": True,
            "segment": classification.segment.value,
            "ai_maturity": signal_brief.ai_maturity.score,
        },
    )

    return draft, traces


def _build_system_prompt(
    email_type: EmailType,
    style_guide: str,
    bench_summary: str,
    pricing_sheet: str,
) -> str:
    """Build the system prompt for the email drafter LLM call."""
    return f"""You are an email drafter for Tenacious Consulting and Outsourcing.
You write outbound emails that a prospect (founder, CTO, VP Engineering) would read with interest rather than discomfort.

## RULES — THESE ARE ABSOLUTE CONSTRAINTS

1. HONESTY RULE: Every factual claim must be grounded in the signal brief data provided. If a signal has confidence: low or is null, ASK rather than ASSERT.
2. BENCH CONSTRAINT: Never commit to specific capacity that the bench summary does not show. Use cautious language for thin bench areas.
3. PRICING: Only quote public-tier pricing bands. Deeper pricing routes to a human.
4. NO FABRICATION: Never invent quotes, case studies, client names, or signal data.
5. TONE: Follow the style guide exactly. Two-sentence paragraphs max for cold outbound. No hype words.
6. DRAFTS: All outputs are marked as drafts per data handling policy.

## CONFIDENCE-AWARE PHRASING

For each signal referenced:
- confidence: high → ASSERT: "You closed a $14M Series B in February..."
- confidence: medium → SOFT-ASSERT: "Looking at the public signal, it seems..."
- confidence: low → ASK: "Is your engineering team expanding faster than..."

## STYLE GUIDE
{style_guide[:2000]}

## BENCH SUMMARY
{bench_summary[:1500]}

## PRICING
{pricing_sheet[:1000]}

## OUTPUT FORMAT

Return a JSON object with these exact fields:
{{
    "subject": "email subject line",
    "body": "email body text",
    "proposed_times": [{{"prospect_local": "2026-04-22 10:00 CET", "utc": "2026-04-22 09:00 UTC"}}],
    "calcom_link": "booking URL"
}}
"""


def _build_user_prompt(
    signal_brief: HiringSignalBrief,
    classification: ICPClassification,
    email_type: EmailType,
    gap_brief: CompetitorGapBrief | None,
    thread_history: list[dict] | None,
    grounded_claims: list[GroundedClaim],
) -> str:
    """Build the user prompt with all signal data."""
    prospect = signal_brief.prospect
    prompt_parts = [
        f"## Email Type: {email_type.value}",
        "\n## Prospect",
        f"Company: {prospect.company}",
        f"Industry: {prospect.industry or 'unknown'}",
        f"Location: {prospect.hq_location or 'unknown'}",
        f"Employees: {prospect.employee_count or 'unknown'}",
        f"Contact: {prospect.contact_name or 'unknown'} ({prospect.contact_title or 'unknown'})",
        "\n## ICP Classification",
        f"Segment: {classification.segment.value}",
        f"Confidence: {classification.confidence.value}",
    ]

    if classification.segment == ICPSegment.ABSTAIN:
        prompt_parts.append(
            "INSTRUCTION: Send a generic exploratory email. Do NOT reference specific segment-level pitches."
        )

    # Add signal brief data
    prompt_parts.append("\n## Signal Brief Data")

    if signal_brief.funding.event:
        conf = signal_brief.funding.confidence.value
        prompt_parts.append(
            f"Funding: {signal_brief.funding.event}, ${signal_brief.funding.amount_usd:,} (confidence: {conf})"
            if signal_brief.funding.amount_usd
            else f"Funding: {signal_brief.funding.event} (confidence: {conf})"
        )

    if signal_brief.hiring.open_eng_roles is not None:
        conf = signal_brief.hiring.confidence.value
        roles = signal_brief.hiring.open_eng_roles
        # Honesty constraint: < 5 roles → do NOT claim "aggressive hiring"
        if roles < 5:
            prompt_parts.append(
                f"Hiring: {roles} open eng roles — DO NOT claim 'aggressive hiring' or 'scaling aggressively'. Ask instead. (confidence: {conf})"
            )
        else:
            prompt_parts.append(
                f"Hiring: {roles} open eng roles, delta 60d: {signal_brief.hiring.delta_60d} (confidence: {conf})"
            )

    if signal_brief.layoffs.event:
        prompt_parts.append(
            f"Layoffs: yes, {signal_brief.layoffs.headcount_pct}% (confidence: {signal_brief.layoffs.confidence.value})"
        )
        prompt_parts.append(
            "INSTRUCTION: Do NOT reference layoffs directly in first touch. Use Segment 2 cost-discipline framing implicitly."
        )

    if signal_brief.leadership.change:
        prompt_parts.append(
            f"Leadership change: new {signal_brief.leadership.role} (confidence: {signal_brief.leadership.confidence.value})"
        )

    prompt_parts.append(
        f"AI maturity: {signal_brief.ai_maturity.score}/3 (confidence: {signal_brief.ai_maturity.confidence.value})"
    )
    if signal_brief.ai_maturity.language_notes:
        prompt_parts.append(f"AI language notes: {signal_brief.ai_maturity.language_notes}")

    # Pitch guidance
    if signal_brief.pitch_guidance.language_notes:
        prompt_parts.append(f"Pitch guidance: {signal_brief.pitch_guidance.language_notes}")

    # Competitor gap brief
    if gap_brief and gap_brief.gaps and email_type == EmailType.COLD:
        prompt_parts.append("\n## Competitor Gap Brief (use as opening hook for cold outbound)")
        for gap in gap_brief.gaps[:2]:
            prompt_parts.append(
                f"- Gap: {gap.practice} — {gap.cohort_adoption} (prospect has: {gap.prospect_has_it}, confidence: {gap.confidence.value})"
            )
        prompt_parts.append(
            "INSTRUCTION: Lead with the gap finding, NOT the vendor pitch. The gap is the hook; capability is the close."
        )

    # Thread history
    if thread_history:
        prompt_parts.append("\n## Thread History (for warm reply / re-engagement)")
        for msg in thread_history[-5:]:  # Last 5 messages
            prompt_parts.append(f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:200]}")

    return "\n".join(prompt_parts)


async def _tone_check(
    email_body: str,
    style_guide: str,
    company: str,
    thread_id: str,
) -> tuple[float, TraceRecord]:
    """
    Run a tone-preservation check as a second LLM call.
    Scores the draft against the style guide on a 0-1 scale.
    Regenerate if < 0.7. Cost this call in the memo.
    """
    llm = get_llm_client()
    result, trace = await llm.chat_json(
        messages=[
            {
                "role": "system",
                "content": 'You are a tone checker for Tenacious Consulting. Score the email draft on a 0-1 scale against the style guide. Return JSON: {"score": 0.85, "issues": ["list of issues"]}',
            },
            {
                "role": "user",
                "content": f"## Style Guide\n{style_guide[:1500]}\n\n## Email Draft\n{email_body}",
            },
        ],
        temperature=0.1,
        max_tokens=512,
        trace_event="tone_check",
        prospect_company=company,
        thread_id=thread_id,
    )

    score = float(result.get("score", 0.5))
    if result.get("issues"):
        logger.info("Tone check issues for %s: %s", company, result["issues"])

    return score, trace


def _extract_grounded_claims(brief: HiringSignalBrief) -> list[GroundedClaim]:
    """Extract all potential grounded claims from the signal brief."""
    claims = []

    if brief.funding.event and brief.funding.confidence != Confidence.LOW:
        val = f"{brief.funding.event}"
        if brief.funding.amount_usd:
            val += f", ${brief.funding.amount_usd:,}"
        claims.append(
            GroundedClaim(
                claim=val,
                source_field="funding.event",
                confidence=brief.funding.confidence,
            )
        )

    if brief.hiring.open_eng_roles and brief.hiring.confidence != Confidence.LOW:
        claims.append(
            GroundedClaim(
                claim=f"{brief.hiring.open_eng_roles} open engineering roles",
                source_field="hiring.open_eng_roles",
                confidence=brief.hiring.confidence,
            )
        )

    if brief.hiring.delta_60d and brief.hiring.confidence == Confidence.HIGH:
        claims.append(
            GroundedClaim(
                claim=f"Eng roles changed {brief.hiring.delta_60d} in 60 days",
                source_field="hiring.delta_60d",
                confidence=brief.hiring.confidence,
            )
        )

    return claims


def _load_seed_file(filenames: str | list[str]) -> str:
    """Load the first available seed material file from the configured seed path."""
    candidates = [filenames] if isinstance(filenames, str) else filenames
    for filename in candidates:
        path = settings.seeds_path / filename
        if path.exists():
            return path.read_text(encoding="utf-8")

    logger.warning("Seed file not found. Checked: %s", ", ".join(str(settings.seeds_path / f) for f in candidates))
    return ""
