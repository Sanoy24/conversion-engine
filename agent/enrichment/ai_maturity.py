"""
AI maturity scorer (0-3) with explicit six-input collection.
"""

from __future__ import annotations

import re
from typing import Any

from agent.models import AIMaturityInput, AIMaturitySignal, Confidence, HiringSignal, SignalWeight

_WEIGHT_POINTS = {
    SignalWeight.HIGH: 3,
    SignalWeight.MEDIUM: 2,
    SignalWeight.LOW: 1,
}
_AI_KEYWORDS = ("ai", "machine learning", "ml", "artificial intelligence", "llm", "agentic")
_AI_LEADERSHIP_TITLES = (
    "head of ai",
    "vp data",
    "chief scientist",
    "head of machine learning",
    "vp ai",
    "director of ai",
    "chief ai officer",
    "head of data science",
)
_ML_STACK_TOOLS = (
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
)


def collect_ai_maturity_supporting_signals(crunchbase_record: dict | None) -> dict[str, Any]:
    """
    Collect medium/low-tier AI maturity signals from public Crunchbase metadata.
    """
    if not crunchbase_record:
        return {
            "github_activity": None,
            "exec_commentary": None,
            "tech_stack_signals": None,
            "strategic_communications": None,
        }
    return {
        "github_activity": _collect_github_activity(crunchbase_record),
        "exec_commentary": _collect_exec_commentary(crunchbase_record),
        "tech_stack_signals": _collect_tech_stack_signals(crunchbase_record),
        "strategic_communications": _collect_strategic_communications(crunchbase_record),
    }


def score_ai_maturity(
    hiring: HiringSignal | None = None,
    crunchbase_record: dict | None = None,
    github_activity: dict | None = None,
    exec_commentary: list[str] | None = None,
    tech_stack_signals: list[str] | None = None,
    strategic_communications: list[str] | None = None,
) -> AIMaturitySignal:
    """
    Score AI maturity from six weighted input families:
      - HIGH: AI-adjacent roles, named AI/ML leadership
      - MEDIUM: GitHub activity, executive commentary
      - LOW: modern ML/data stack, strategic communications
    """
    if github_activity is None or exec_commentary is None or tech_stack_signals is None or strategic_communications is None:
        collected = collect_ai_maturity_supporting_signals(crunchbase_record)
        github_activity = github_activity if github_activity is not None else collected["github_activity"]
        exec_commentary = exec_commentary if exec_commentary is not None else collected["exec_commentary"]
        tech_stack_signals = tech_stack_signals if tech_stack_signals is not None else collected["tech_stack_signals"]
        strategic_communications = (
            strategic_communications
            if strategic_communications is not None
            else collected["strategic_communications"]
        )

    inputs: list[AIMaturityInput] = []
    high_hits = 0
    medium_hits = 0
    low_hits = 0
    weighted_points = 0

    # HIGH 1/2: AI-adjacent open roles.
    ai_roles_input, ai_roles_positive = _build_ai_adjacent_roles_input(hiring)
    inputs.append(ai_roles_input)
    if ai_roles_positive:
        high_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.HIGH]

    # HIGH 2/2: Named AI/ML leadership.
    leadership_input, leadership_positive = _build_named_leadership_input(crunchbase_record)
    inputs.append(leadership_input)
    if leadership_positive:
        high_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.HIGH]

    # MEDIUM 1/2: GitHub activity.
    github_input, github_positive = _build_github_input(github_activity)
    inputs.append(github_input)
    if github_positive:
        medium_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.MEDIUM]

    # MEDIUM 2/2: Executive commentary.
    exec_input, exec_positive = _build_exec_commentary_input(exec_commentary)
    inputs.append(exec_input)
    if exec_positive:
        medium_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.MEDIUM]

    # LOW 1/2: Modern stack.
    stack_input, stack_positive = _build_stack_input(tech_stack_signals)
    inputs.append(stack_input)
    if stack_positive:
        low_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.LOW]

    # LOW 2/2: Strategic communications.
    comms_input, comms_positive = _build_strategic_comms_input(strategic_communications)
    inputs.append(comms_input)
    if comms_positive:
        low_hits += 1
        weighted_points += _WEIGHT_POINTS[SignalWeight.LOW]

    score = _compute_score(high_hits=high_hits, medium_hits=medium_hits, low_hits=low_hits, weighted_points=weighted_points)
    confidence = _compute_confidence(score=score, high_hits=high_hits, medium_hits=medium_hits, weighted_points=weighted_points)
    language_notes = _generate_language_notes(score=score, confidence=confidence, inputs=inputs)

    return AIMaturitySignal(score=score, confidence=confidence, inputs=inputs, language_notes=language_notes)


def _build_ai_adjacent_roles_input(hiring: HiringSignal | None) -> tuple[AIMaturityInput, bool]:
    if not hiring or hiring.ai_adjacent_eng_roles is None:
        return AIMaturityInput(
            type="ai_adjacent_roles",
            weight=SignalWeight.HIGH,
            evidence="Not checked — absence is not proof of absence",
        ), False
    total_eng = hiring.open_eng_roles or 0
    ai_roles = hiring.ai_adjacent_eng_roles
    if ai_roles > 0 and total_eng > 0:
        fraction = ai_roles / total_eng
        evidence = f"{ai_roles} of {total_eng} eng roles are AI/ML adjacent ({fraction:.0%})"
        return AIMaturityInput(type="ai_adjacent_roles", weight=SignalWeight.HIGH, evidence=evidence), True
    return AIMaturityInput(type="ai_adjacent_roles", weight=SignalWeight.HIGH, evidence=None), False


def _build_named_leadership_input(crunchbase_record: dict | None) -> tuple[AIMaturityInput, bool]:
    people = crunchbase_record.get("people") if crunchbase_record else None
    if not people:
        return AIMaturityInput(type="named_ai_leadership", weight=SignalWeight.HIGH, evidence=None), False
    for person in people:
        title = (person.get("title") or person.get("role") or "").lower()
        if any(marker in title for marker in _AI_LEADERSHIP_TITLES):
            name = person.get("name", "Unknown")
            pretty_title = person.get("title") or person.get("role") or "AI/ML leadership"
            return AIMaturityInput(
                type="named_ai_leadership",
                weight=SignalWeight.HIGH,
                evidence=f"{name} — {pretty_title}",
            ), True
    return AIMaturityInput(type="named_ai_leadership", weight=SignalWeight.HIGH, evidence=None), False


def _build_github_input(github_activity: dict | None) -> tuple[AIMaturityInput, bool]:
    if github_activity is None:
        return AIMaturityInput(
            type="github_activity",
            weight=SignalWeight.MEDIUM,
            evidence="Not checked — absence is not proof of absence",
        ), False
    ai_repos = int(github_activity.get("ai_repos", 0) or 0)
    commits = int(github_activity.get("recent_ai_commits", 0) or 0)
    org = github_activity.get("org") or "unknown_org"
    if ai_repos > 0 or commits > 0:
        return AIMaturityInput(
            type="github_activity",
            weight=SignalWeight.MEDIUM,
            evidence=f"{org}: {ai_repos} AI/ML repos, {commits} recent commits",
        ), True
    return AIMaturityInput(type="github_activity", weight=SignalWeight.MEDIUM, evidence=None), False


def _build_exec_commentary_input(exec_commentary: list[str] | None) -> tuple[AIMaturityInput, bool]:
    if exec_commentary is None:
        return AIMaturityInput(
            type="exec_commentary",
            weight=SignalWeight.MEDIUM,
            evidence="Not checked — absence is not proof of absence",
        ), False
    ai_mentions = [text for text in exec_commentary if any(token in text.lower() for token in _AI_KEYWORDS)]
    if ai_mentions:
        snippet = ai_mentions[0][:120].strip()
        return AIMaturityInput(
            type="exec_commentary",
            weight=SignalWeight.MEDIUM,
            evidence=f"{len(ai_mentions)} AI-related executive statement(s); sample: {snippet}",
        ), True
    return AIMaturityInput(type="exec_commentary", weight=SignalWeight.MEDIUM, evidence=None), False


def _build_stack_input(tech_stack_signals: list[str] | None) -> tuple[AIMaturityInput, bool]:
    if tech_stack_signals is None:
        return AIMaturityInput(
            type="ml_stack",
            weight=SignalWeight.LOW,
            evidence="Not checked — absence is not proof of absence",
        ), False
    found = [token for token in tech_stack_signals if token.lower() in _ML_STACK_TOOLS]
    if found:
        unique = ", ".join(sorted(set(found))[:5])
        return AIMaturityInput(type="ml_stack", weight=SignalWeight.LOW, evidence=f"Stack signals: {unique}"), True
    return AIMaturityInput(type="ml_stack", weight=SignalWeight.LOW, evidence=None), False


def _build_strategic_comms_input(strategic_communications: list[str] | None) -> tuple[AIMaturityInput, bool]:
    if strategic_communications is None:
        return AIMaturityInput(
            type="strategic_communications",
            weight=SignalWeight.LOW,
            evidence="Not checked — absence is not proof of absence",
        ), False
    ai_mentions = [text for text in strategic_communications if any(token in text.lower() for token in _AI_KEYWORDS)]
    if ai_mentions:
        return AIMaturityInput(
            type="strategic_communications",
            weight=SignalWeight.LOW,
            evidence=f"{len(ai_mentions)} AI-forward strategic communication signal(s)",
        ), True
    return AIMaturityInput(type="strategic_communications", weight=SignalWeight.LOW, evidence=None), False


def _compute_score(*, high_hits: int, medium_hits: int, low_hits: int, weighted_points: int) -> int:
    """
    Compute integer score 0-3 with tier-aligned constraints.
    """
    if high_hits == 0 and medium_hits == 0 and low_hits == 0:
        return 0
    # Constraint from rubric: score >=2 requires at least one HIGH-tier signal.
    if high_hits >= 2 and medium_hits >= 1 and weighted_points >= 8:
        return 3
    if high_hits >= 1 and (medium_hits >= 1 or low_hits >= 2):
        return 2
    return 1


def _compute_confidence(*, score: int, high_hits: int, medium_hits: int, weighted_points: int) -> Confidence:
    if score == 0:
        return Confidence.LOW
    if high_hits >= 2 and medium_hits >= 1 and weighted_points >= 8:
        return Confidence.HIGH
    if high_hits >= 1 and weighted_points >= 5:
        return Confidence.MEDIUM
    return Confidence.LOW


def _generate_language_notes(score: int, confidence: Confidence, inputs: list[AIMaturityInput]) -> str:
    notes: list[str] = []
    if score == 0:
        notes.append(
            "No public AI signal detected. Score 0 means 'no public signal', not 'no AI work'."
        )
    if confidence == Confidence.LOW and score >= 2:
        notes.append(f"Score {score} is inferred from weaker evidence — prefer ASK over ASSERT.")
    if confidence == Confidence.LOW:
        notes.append("Low confidence overall; soften AI assertions.")
    unchecked = [i for i in inputs if i.evidence and "Not checked" in i.evidence]
    if unchecked:
        notes.append(f"{len(unchecked)} source(s) were not checked; score may undercount.")
    return " ".join(notes) if notes else "Standard confidence — assert based on evidence."


def _collect_github_activity(crunchbase_record: dict) -> dict | None:
    """
    Collect GitHub activity signal from public metadata fields.
    """
    # Prefer structured activity if present.
    structured = crunchbase_record.get("github_org_activity")
    if isinstance(structured, dict):
        ai_repos = int(structured.get("ai_repos", 0) or 0)
        commits = int(structured.get("recent_ai_commits", 0) or 0)
        org = structured.get("org") or "unknown_org"
        return {"org": org, "ai_repos": ai_repos, "recent_ai_commits": commits}

    github_text_fields = []
    for key, value in crunchbase_record.items():
        if "github" in key.lower():
            github_text_fields.append(str(value))
    if not github_text_fields:
        return None
    joined = " ".join(github_text_fields).lower()
    ai_repo_markers = len(re.findall(r"(llm|ai|ml|inference|model|agent)", joined))
    return {
        "org": _extract_github_org(joined),
        "ai_repos": ai_repo_markers,
        "recent_ai_commits": min(ai_repo_markers * 2, 20),
    }


def _extract_github_org(text: str) -> str:
    match = re.search(r"github\.com/([a-z0-9_.-]+)", text)
    return match.group(1) if match else "unknown_org"


def _collect_exec_commentary(crunchbase_record: dict) -> list[str] | None:
    """
    Collect executive commentary-like snippets from public company text fields.
    """
    candidates: list[str] = []

    # Prefer structured commentary when available.
    structured = crunchbase_record.get("exec_commentary")
    if isinstance(structured, list):
        for item in structured:
            if isinstance(item, dict):
                text = item.get("text") or item.get("quote") or item.get("summary")
                if text:
                    candidates.append(str(text))
            elif isinstance(item, str):
                candidates.append(item)

    # Fall back to common public text fields.
    for key in (
        "about",
        "description",
        "short_description",
        "full_description",
        "investor_description",
        "overview",
    ):
        value = crunchbase_record.get(key)
        if value:
            candidates.append(str(value))
    return candidates or None


def _collect_tech_stack_signals(crunchbase_record: dict) -> list[str] | None:
    """
    Collect modern stack indicators from text metadata.
    """
    blobs = []
    for key in ("tech_stack", "technology_stack", "stack", "tools", "keywords", "about", "description"):
        value = crunchbase_record.get(key)
        if value:
            blobs.append(str(value).lower())
    if not blobs:
        return None
    joined = " ".join(blobs)
    return [tool for tool in _ML_STACK_TOOLS if tool in joined] or []


def _collect_strategic_communications(crunchbase_record: dict) -> list[str] | None:
    """
    Collect strategic communication snippets from funding/announcement metadata.
    """
    snippets: list[str] = []

    def _append_nested_text(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for field in ("title", "summary", "description", "content", "quote"):
                        if item.get(field):
                            snippets.append(str(item[field]))
                elif item:
                    snippets.append(str(item))
        elif isinstance(value, dict):
            for field in ("title", "summary", "description", "content"):
                if value.get(field):
                    snippets.append(str(value[field]))
        elif value:
            snippets.append(str(value))

    for key in ("funding_rounds_list", "press_references", "announcements", "news", "about"):
        value = crunchbase_record.get(key)
        _append_nested_text(value)
    return snippets or None
