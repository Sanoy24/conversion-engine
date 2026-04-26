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
from agent.core.scap import SCAPConfig, apply_scap, render_ask_directives
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

    # ── SCAP (Act IV mechanism) ────────────────────────────────────────
    # Pre-prompt transform: strip LOW-confidence signals, soften MEDIUM,
    # filter LOW-confidence gaps. Controlled by settings.enable_scap plus
    # sub-flags so ablation variants are selectable. The transform is
    # audited via draft_metadata["scap_transforms"] for the evidence graph.
    scap_transforms: list[str] = []
    scap_ask_block = ""
    if settings.enable_scap:
        scap_config = SCAPConfig(
            strip_low_confidence=settings.scap_strip_low,
            filter_gap_low=settings.scap_filter_gap_low,
            soften_medium=settings.scap_soften_medium,
        )
        scap_result = apply_scap(signal_brief, gap_brief, scap_config)
        signal_brief = scap_result.brief
        gap_brief = scap_result.gap_brief
        scap_transforms = scap_result.transforms
        scap_ask_block = render_ask_directives(scap_result.ask_directives)
        if scap_transforms:
            logger.info(
                "SCAP applied for %s: %d transform(s): %s",
                signal_brief.prospect.company, len(scap_transforms), scap_transforms,
            )

    # Load ALL seed materials from tenacious_sales_data/seed/.
    style_guide = _load_seed_file(["style_guide.md"])
    # email_sequences/ is a folder with cold.md, warm.md, reengagement.md
    email_sequences = _load_seed_file(["email_sequences"])
    pricing_sheet = _load_seed_file(["pricing_sheet.md"])
    # bench_summary is a JSON file — pretty-printed into the prompt
    bench_summary = _load_seed_file(["bench_summary.json"])
    # Discovery transcripts — folder used for objection handling patterns
    discovery_transcripts = _load_seed_file(["discovery_transcripts"])
    # Case studies — grounding for warm replies and proof points
    case_studies = _load_seed_file(["case_studies.md"])
    # Sales deck notes — pitch framing per segment
    sales_deck_notes = _load_seed_file(["sales_deck_notes.md"])
    # ICP definition — segment qualifying/disqualifying filters
    icp_definition = _load_seed_file(["icp_definition.md"])

    # Build the grounded claims list
    grounded = _extract_grounded_claims(signal_brief)

    # Build the system prompt with ALL seed context
    system_prompt = _build_system_prompt(
        email_type=email_type,
        style_guide=style_guide,
        bench_summary=bench_summary,
        pricing_sheet=pricing_sheet,
        email_sequences=email_sequences,
        discovery_transcripts=discovery_transcripts,
        case_studies=case_studies,
        sales_deck_notes=sales_deck_notes,
        icp_definition=icp_definition,
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
    if scap_ask_block:
        user_prompt = user_prompt + "\n\n" + scap_ask_block

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
            "scap_enabled": settings.enable_scap,
            "scap_transforms": scap_transforms,
        },
    )

    return draft, traces


def _build_system_prompt(
    email_type: EmailType,
    style_guide: str,
    bench_summary: str,
    pricing_sheet: str,
    email_sequences: str = "",
    discovery_transcripts: str = "",
    case_studies: str = "",
    sales_deck_notes: str = "",
    icp_definition: str = "",
) -> str:
    """Build the system prompt for the email drafter LLM call."""
    # Compose optional sections — only include if data is available
    optional_sections = []

    if email_sequences:
        optional_sections.append(
            f"## EMAIL SEQUENCE TEMPLATES (follow structure, NOT verbatim)\n{email_sequences[:2000]}"
        )
    if discovery_transcripts and email_type in (EmailType.WARM_REPLY, EmailType.RE_ENGAGEMENT):
        optional_sections.append(
            f"## DISCOVERY TRANSCRIPTS (objection handling patterns)\n{discovery_transcripts[:2000]}"
        )
    if case_studies and email_type == EmailType.WARM_REPLY:
        optional_sections.append(
            f"## CASE STUDIES (for proof points in warm replies only)\n{case_studies[:1500]}"
        )
    if sales_deck_notes:
        optional_sections.append(
            f"## SALES DECK NOTES (pitch framing per segment)\n{sales_deck_notes[:1500]}"
        )
    if icp_definition:
        optional_sections.append(
            f"## ICP SEGMENT DEFINITIONS (qualifying filters & pitch language)\n{icp_definition[:2000]}"
        )

    optional_block = "\n\n".join(optional_sections)

    return f"""You are an email drafter for Tenacious Intelligence Corporation.
You write outbound emails that a prospect (founder, CTO, VP Engineering) would read with interest rather than discomfort.

## RULES — THESE ARE ABSOLUTE CONSTRAINTS

1. HONESTY RULE: Every factual claim must be grounded in the signal brief data provided. If a signal has confidence: low or is null, ASK rather than ASSERT.
2. BENCH CONSTRAINT: Never commit to specific capacity that the bench summary does not show. Use cautious language for thin bench areas.
3. PRICING: Only quote public-tier pricing bands. Deeper pricing routes to a human.
4. NO FABRICATION: Never invent quotes, case studies, client names, or signal data.
5. TONE: Follow the style guide exactly. Max 120 words for cold outbound body. No hype words, no emojis in cold outreach.
6. SUBJECT LINE: Under 60 characters. Start with "Request:", "Context:", "Note on", "Congrats on", or "Question on".
7. SIGNATURE: First name, title (Research Partner), Tenacious Intelligence Corporation, gettenacious.com. Nothing else.
8. ONE ASK per email. Never stack multiple asks.
9. DRAFTS: All outputs are marked as drafts per data handling policy.
10. NO OFFSHORE / VENDOR LANGUAGE: Never use "offshore", "nearshore", "top talent", "world-class", "A-players", "rockstar", "ninja", or "cost savings of X%". Any of these signals to the prospect that Tenacious is a commodity vendor, which destroys trust instantly.
11. WORD "BENCH": Never use the word "bench" — prospects read it as offshore-vendor language. Use "engineering team" or "available capacity".

## CONFIDENCE-AWARE PHRASING

For each signal referenced:
- confidence: high → ASSERT: "You closed a $14M Series B in February..."
- confidence: medium → SOFT-ASSERT: "Looking at the public signal, it seems..."
- confidence: low → ASK: "Is your engineering team expanding faster than..."
- fewer than 5 open roles → NEVER claim "aggressive hiring" or "scaling aggressively"

## THE FIVE TONE MARKERS (all must be preserved)
1. Direct — clear, brief, actionable, no filler
2. Grounded — every claim maps to the signal brief
3. Honest — refuse claims that cannot be grounded
4. Professional — respectful, no internal jargon
5. Non-condescending — gaps are research findings, not failures

## STYLE GUIDE
{style_guide[:2500]}

## BENCH SUMMARY (available engineering capacity)
{bench_summary[:1500]}

## PRICING BANDS (quotable ranges only)
{pricing_sheet[:1200]}

{optional_block}

## OUTPUT FORMAT

Return a JSON object with these exact fields:
{{
    "subject": "email subject line (under 60 chars)",
    "body": "email body text (max 120 words for cold, 100 for follow-up, 70 for close)",
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
        # P027 fix: never fabricate a timezone — only include proposed_times when tz is known
        (
            f"Prospect timezone: {prospect.timezone} — use this for any proposed_times slots."
            if prospect.timezone
            else "Prospect timezone: UNKNOWN — set proposed_times to [] and do NOT invent a timezone or a local time."
        ),
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
        # P034 fix: confidence-aware phrasing for prospect_has_it=False claims.
        # Public scrapers cannot verify internal tooling — never assert absence confidently.
        prompt_parts.append(
            "INSTRUCTION (gap confidence rules — MANDATORY):\n"
            "  - prospect_has_it=False, confidence=high → assert: 'Your public profile shows no [practice] yet'\n"
            "  - prospect_has_it=False, confidence=medium → frame as a research question: "
            "'Based on our public scan, it looks like [company] may not have [practice] yet — is that accurate?'\n"
            "  - prospect_has_it=False, confidence=low → OMIT the gap entirely; the evidence is too thin to raise."
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
    """
    Load the first available seed material from the configured seed path.

    Each candidate is resolved in order. Handles three shapes:
      - plain file (e.g. `style_guide.md`) — returned as-is
      - directory (e.g. `email_sequences/`) — all .md files inside are
        concatenated with section headers so the drafter sees the full
        cold/warm/reengagement sequence
      - JSON file (e.g. `bench_summary.json`) — returned as pretty-printed
        text so the LLM can read it in the prompt
    """
    candidates = [filenames] if isinstance(filenames, str) else filenames
    for filename in candidates:
        path = settings.seeds_path / filename

        # Directory: concatenate all .md files inside
        if path.is_dir():
            parts: list[str] = []
            for md_path in sorted(path.glob("*.md")):
                parts.append(f"## {md_path.stem}\n\n{md_path.read_text(encoding='utf-8')}")
            if parts:
                return "\n\n---\n\n".join(parts)
            continue

        if path.exists():
            # JSON files: read + pretty-print so the LLM can consume them as text
            if path.suffix == ".json":
                import json as _json
                try:
                    data = _json.loads(path.read_text(encoding="utf-8"))
                    return _json.dumps(data, indent=2)
                except _json.JSONDecodeError:
                    pass
            return path.read_text(encoding="utf-8")

    logger.warning(
        "Seed file not found. Checked: %s",
        ", ".join(str(settings.seeds_path / f) for f in candidates),
    )
    return ""
