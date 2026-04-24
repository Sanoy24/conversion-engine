"""
AI maturity scorer (0-3).
Scores a prospect's AI readiness from public signals with per-input justification.

Rules:
- Score 0: no public signal of AI engagement
- Score 1: light signal (one medium-weight input)
- Score 2: moderate signal (requires at least one HIGH-weight input)
- Score 3: active AI function with recent exec commitment + multiple open roles
- Absence is not proof of absence — score 0 means "no public signal"
- Confidence is separate from score — high score + low confidence = soften language
"""

from __future__ import annotations

import logging

from agent.models import (
    AIMaturityInput,
    AIMaturitySignal,
    Confidence,
    HiringSignal,
    SignalWeight,
)

logger = logging.getLogger(__name__)


def score_ai_maturity(
    hiring: HiringSignal | None = None,
    crunchbase_record: dict | None = None,
    github_activity: dict | None = None,
    exec_commentary: list[str] | None = None,
    tech_stack_signals: list[str] | None = None,
    strategic_communications: list[str] | None = None,
) -> AIMaturitySignal:
    """
    Score a prospect's AI maturity from 0-3 with per-input justification.

    Each input contributes evidence with a weight (high/medium/low).
    The final score is derived from the weighted sum of positive inputs.
    Confidence reflects the strength of evidence, not the score itself.
    """
    inputs: list[AIMaturityInput] = []
    high_weight_hits = 0
    medium_weight_hits = 0
    low_weight_hits = 0

    # ── HIGH weight: AI-adjacent open roles ──
    if hiring and hiring.ai_adjacent_eng_roles is not None:
        total_eng = hiring.open_eng_roles or 0
        ai_roles = hiring.ai_adjacent_eng_roles

        if ai_roles > 0 and total_eng > 0:
            fraction = ai_roles / total_eng
            evidence = f"{ai_roles} of {total_eng} eng roles are AI/ML adjacent ({fraction:.0%})"
            inputs.append(
                AIMaturityInput(
                    type="ai_adjacent_roles",
                    weight=SignalWeight.HIGH,
                    evidence=evidence,
                )
            )
            if ai_roles >= 3:
                high_weight_hits += 1
            else:
                medium_weight_hits += 1
        else:
            inputs.append(
                AIMaturityInput(
                    type="ai_adjacent_roles",
                    weight=SignalWeight.HIGH,
                    evidence=None,  # No AI roles found
                )
            )

    # ── HIGH weight: Named AI/ML leadership ──
    has_ai_leadership = False
    if crunchbase_record:
        people = crunchbase_record.get("people") or []
        ai_titles = {
            "head of ai",
            "vp data",
            "chief scientist",
            "head of machine learning",
            "vp ai",
            "director of ai",
            "chief ai officer",
            "head of data science",
        }
        for person in people:
            title = (person.get("title") or "").lower()
            if any(t in title for t in ai_titles):
                has_ai_leadership = True
                inputs.append(
                    AIMaturityInput(
                        type="named_ai_leadership",
                        weight=SignalWeight.HIGH,
                        evidence=f"{person.get('name', 'Unknown')} — {person.get('title')}",
                    )
                )
                high_weight_hits += 1
                break

    if not has_ai_leadership:
        inputs.append(
            AIMaturityInput(
                type="named_ai_leadership",
                weight=SignalWeight.HIGH,
                evidence=None,
            )
        )

    # ── MEDIUM weight: Public GitHub org activity ──
    if github_activity:
        ai_repos = github_activity.get("ai_repos", 0)
        recent_commits = github_activity.get("recent_ai_commits", 0)
        if ai_repos > 0 or recent_commits > 0:
            inputs.append(
                AIMaturityInput(
                    type="github_activity",
                    weight=SignalWeight.MEDIUM,
                    evidence=f"{ai_repos} AI/ML repos, {recent_commits} recent commits",
                )
            )
            medium_weight_hits += 1
        else:
            inputs.append(
                AIMaturityInput(
                    type="github_activity",
                    weight=SignalWeight.MEDIUM,
                    evidence=None,
                )
            )
    else:
        inputs.append(
            AIMaturityInput(
                type="github_activity",
                weight=SignalWeight.MEDIUM,
                evidence="Not checked — absence is not proof of absence",
            )
        )

    # ── MEDIUM weight: Executive commentary ──
    if exec_commentary:
        ai_mentions = [
            c
            for c in exec_commentary
            if any(
                kw in c.lower()
                for kw in ["ai", "machine learning", "ml", "artificial intelligence", "llm"]
            )
        ]
        if ai_mentions:
            inputs.append(
                AIMaturityInput(
                    type="exec_commentary",
                    weight=SignalWeight.MEDIUM,
                    evidence=f"{len(ai_mentions)} AI-related exec statements found",
                )
            )
            medium_weight_hits += 1

    # ── LOW weight: Modern data/ML stack ──
    ml_stack_tools = {
        "dbt",
        "snowflake",
        "databricks",
        "weights and biases",
        "wandb",
        "ray",
        "vllm",
        "mlflow",
        "kubeflow",
        "sagemaker",
        "vertex ai",
    }
    if tech_stack_signals:
        found_tools = [t for t in tech_stack_signals if t.lower() in ml_stack_tools]
        if found_tools:
            inputs.append(
                AIMaturityInput(
                    type="ml_stack",
                    weight=SignalWeight.LOW,
                    evidence=f"Stack signals: {', '.join(found_tools)}",
                )
            )
            low_weight_hits += 1

    # ── LOW weight: Strategic communications ──
    # Annual reports, fundraising press, investor letters positioning AI as
    # a company priority. Weighted LOW per the challenge brief rubric (line 90).
    # Absence is not proof of absence: we append an "unchecked" input when the
    # caller does not supply any text corpus so silent-company handling below
    # can flag it.
    if strategic_communications:
        ai_comms = [
            c for c in strategic_communications
            if any(
                kw in c.lower()
                for kw in ("ai", "artificial intelligence", "machine learning", "ml ", "llm")
            )
        ]
        if ai_comms:
            inputs.append(
                AIMaturityInput(
                    type="strategic_communications",
                    weight=SignalWeight.LOW,
                    evidence=f"{len(ai_comms)} AI-forward passage(s) in strategic comms",
                )
            )
            low_weight_hits += 1
        else:
            inputs.append(
                AIMaturityInput(
                    type="strategic_communications",
                    weight=SignalWeight.LOW,
                    evidence=None,
                )
            )
    else:
        inputs.append(
            AIMaturityInput(
                type="strategic_communications",
                weight=SignalWeight.LOW,
                evidence="Not checked — absence is not proof of absence",
            )
        )

    # ── Compute score ──
    score = _compute_score(high_weight_hits, medium_weight_hits, low_weight_hits)

    # ── Compute confidence ──
    confidence = _compute_confidence(score, high_weight_hits, medium_weight_hits, inputs)

    # ── Language notes for downstream agents ──
    language_notes = _generate_language_notes(score, confidence, inputs)

    return AIMaturitySignal(
        score=score,
        confidence=confidence,
        inputs=inputs,
        language_notes=language_notes,
    )


def _compute_score(
    high_hits: int,
    medium_hits: int,
    low_hits: int,
) -> int:
    """
    Derive the 0-3 score from weighted input counts.
    Requires at least one HIGH-weight input for score >= 2.
    """
    if high_hits >= 2 and medium_hits >= 1:
        return 3
    if high_hits >= 1 and (medium_hits >= 1 or low_hits >= 2):
        return 2
    if high_hits >= 1 or medium_hits >= 2:
        return 1
    if medium_hits >= 1 or low_hits >= 2:
        return 1
    return 0


def _compute_confidence(
    score: int,
    high_hits: int,
    medium_hits: int,
    inputs: list[AIMaturityInput],
) -> Confidence:
    """
    Confidence reflects evidence strength, not score magnitude.
    Score 2 from weak inputs ≠ score 2 from strong inputs.
    """
    evidence_count = sum(
        1
        for i in inputs
        if i.evidence and i.evidence != "Not checked — absence is not proof of absence"
    )

    if high_hits >= 2:
        return Confidence.HIGH
    if high_hits >= 1 and medium_hits >= 1:
        return Confidence.HIGH
    if high_hits >= 1 or (medium_hits >= 2 and evidence_count >= 3):
        return Confidence.MEDIUM
    return Confidence.LOW


def _generate_language_notes(
    score: int,
    confidence: Confidence,
    inputs: list[AIMaturityInput],
) -> str:
    """Generate language guidance for downstream agents."""
    notes = []

    if score == 0:
        notes.append(
            "No public AI signal detected. Score 0 means 'no public signal', not 'no AI work'. "
            "Use exploratory language: ask about AI plans rather than assuming absence."
        )

    if confidence == Confidence.LOW and score >= 2:
        notes.append(
            f"Score {score} is based on weak evidence — prefer ASK over ASSERT for AI claims."
        )

    if confidence == Confidence.LOW:
        notes.append(
            "Low confidence overall — downstream agent should soften all AI-related assertions."
        )

    # Check for missing inputs
    unchecked = [i for i in inputs if i.evidence and "Not checked" in (i.evidence or "")]
    if unchecked:
        notes.append(f"{len(unchecked)} signal source(s) were not checked — score may undercount.")

    return " ".join(notes) if notes else "Standard confidence — assert based on evidence."
