"""Load evaluation cases (evals/cases.yaml) into signal briefs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.models import (
    AIMaturitySignal,
    CompetitorGapBrief,
    FundingSignal,
    GapEntry,
    HiringSignal,
    HiringSignalBrief,
    LayoffSignal,
    LeadershipSignal,
    ProspectInfo,
)

CASES_PATH = Path(__file__).with_name("cases.yaml")


@dataclass
class EvalCase:
    id: str
    note: str
    brief: HiringSignalBrief
    gap_brief: CompetitorGapBrief | None
    expected_segment: str


def load_cases(path: Path = CASES_PATH) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [_build(entry) for entry in raw]
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"Duplicate case ids in {path}: {sorted(duplicates)}")
    return cases


def _build(entry: dict) -> EvalCase:
    prospect = ProspectInfo(**entry["prospect"])
    brief = HiringSignalBrief(
        prospect=prospect,
        enriched_at="2026-10-01T00:00:00",
        funding=FundingSignal(**entry.get("funding", {})),
        hiring=HiringSignal(**entry.get("hiring", {})),
        layoffs=LayoffSignal(**entry.get("layoffs", {})),
        leadership=LeadershipSignal(**entry.get("leadership", {})),
        ai_maturity=AIMaturitySignal(**entry.get("ai_maturity", {})),
        requires_human_review=entry.get("requires_human_review", False),
        human_review_reason=entry.get("human_review_reason"),
    )
    gap_brief = None
    if entry.get("gaps"):
        gap_brief = CompetitorGapBrief(
            prospect=prospect,
            gaps=[GapEntry(**gap) for gap in entry["gaps"]],
            generated_at="2026-10-01T00:00:00",
        )
    return EvalCase(
        id=entry["id"],
        note=entry.get("note", ""),
        brief=brief,
        gap_brief=gap_brief,
        expected_segment=entry["expect"]["segment"],
    )


def previous_email(case: EvalCase, signature: str) -> str:
    """
    The neutral first email follow-up cases reply to. Fixed and fact-free on
    purpose, so follow-up scores don't depend on any model's cold draft.
    """
    name = (case.brief.prospect.contact_name or "").split()
    greeting = f"Hi {name[0]}," if name else "Hi,"
    return (
        f"{greeting}\n\nI work with engineering teams on shipping faster than in-house hiring "
        f"alone allows. Would a short call be useful?\n\n{signature}"
    )
