"""
Deterministic quality checks for a drafted cold email.

Nothing here calls an LLM. Each check works out from the case's own signal
brief (after the same SCAP transform the drafter applies) what the email is
allowed to say, then tests the draft against it:

grounding
    numbers_grounded     every number / money amount traces to the brief
    no_withheld_signals  signals SCAP withheld (LOW confidence) do not leak back in
    no_layoff_mention    layoffs are never raised in a first touch
    no_aggressive_hiring no "aggressive hiring" language with fewer than 5 roles
    medium_hedged        medium-confidence facts are hedged or asked
    no_internal_jargon   no "AI maturity", "signal brief", "ICP", ...
    greeting_grounded    the greeting uses the real contact name (or a generic one)
    no_invented_times    no proposed times when the prospect's timezone is unknown
voice
    word_limit, no_banned_phrases, subject_rules, signature_present

The regexes are deliberately simple; tests/test_evals.py pins their
behaviour on hand-written good and bad drafts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.core.scap import apply_scap
from agent.models import Confidence
from agent.playbook import Playbook
from evals.cases import EvalCase

GROUNDING_CHECKS = (
    "numbers_grounded",
    "no_withheld_signals",
    "no_layoff_mention",
    "no_aggressive_hiring",
    "medium_hedged",
    "no_internal_jargon",
    "greeting_grounded",
    "no_invented_times",
)
VOICE_CHECKS = ("word_limit", "no_banned_phrases", "subject_rules", "signature_present")
ALL_CHECKS = GROUNDING_CHECKS + VOICE_CHECKS


@dataclass
class Draft:
    subject: str
    body: str
    proposed_times: list = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Facts:
    """What the email may and may not say, derived from the brief."""

    money: set[float] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)
    # Numbers about the sender (services, capacity): allowed only in sentences
    # about the sender, so they can't launder a made-up claim about the prospect.
    sender_numbers: set[float] = field(default_factory=set)
    sender_names: list[str] = field(default_factory=list)
    forbidden: list[tuple[str, re.Pattern]] = field(default_factory=list)
    hedge_required: list[tuple[str, re.Pattern]] = field(default_factory=list)
    few_roles: bool = False
    has_layoffs: bool = False
    first_name: str | None = None
    timezone_known: bool = False


_MULTIPLIERS = {"k": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_MONEY = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|mm|m|million|bn|b|billion)?\b"
    r"|\b(\d+(?:\.\d+)?)\s*(million|billion)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.$,])(\d+(?:\.\d+)?)(%?)")
_UNIT_AFTER = re.compile(
    r"\s*-?\s*(?:min|mins|minutes?|hours?|hrs?|days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_ORDINAL_AFTER = re.compile(r"(?:st|nd|rd|th)\b", re.IGNORECASE)
_TIME_AFTER = re.compile(r"\s*(?::\d{2}|am\b|pm\b)", re.IGNORECASE)

_LAYOFF = re.compile(
    r"\b(?:layoffs?|laid off|lay off|reductions? in force|downsiz\w*|restructur\w*|"
    r"job cuts|headcount reduction|let go)\b",
    re.IGNORECASE,
)
_AGGRESSIVE = re.compile(
    r"\b(?:aggressive(?:ly)?\s+(?:hiring|scaling|growing|expanding)|"
    r"(?:hiring|scaling|growing|expanding)\s+aggressively|hiring spree|"
    r"rapid(?:ly)?\s+(?:hiring|scaling|growing)|hiring (?:like crazy|at pace))\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:seems?|looks? like|appears?|apparently|if i'?m reading|from what i (?:can )?see|"
    r"public (?:signals?|data|records?|postings?)|might|may|reportedly|suggests?|"
    r"it sounds like|my read|if (?:that'?s|this is) (?:right|accurate)|is that (?:right|accurate))\b|\?",
    re.IGNORECASE,
)
_JARGON = re.compile(
    r"\b(?:ai maturity|maturity score|signal brief|confidence (?:level|score)|icp|"
    r"segment|scap|abstain\w*|enrichment|hiring signal)\b",
    re.IGNORECASE,
)
_GREETING = re.compile(r"^\s*(?:hi|hello|hey|dear)\s+([^,\n!]+)", re.IGNORECASE)
_GENERIC_GREETINGS = {"there", "team", "all", "everyone", "folks"}
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def _word_regex(text: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(text) + r"\b", re.IGNORECASE)


def derive_facts(case: EvalCase, playbook: Playbook) -> Facts:
    original = case.brief
    scap = apply_scap(case.brief, case.gap_brief)
    brief, gaps = scap.brief, scap.gap_brief
    facts = Facts()

    # Facts about the sender (services, capacity) come from the playbook
    # material the drafter is given for cold email.
    sender_texts = [playbook.read_text(playbook.capacity)]
    sender_texts += [
        playbook.read_text(item.path, max_chars=item.max_chars)
        for item in playbook.knowledge
        if "cold" in item.use_for
    ]
    for text in sender_texts:
        facts.sender_numbers.update(
            float(n) for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text or "")
        )
    facts.sender_names = [playbook.company.name]

    if brief.funding.event and brief.funding.amount_usd:
        facts.money.add(float(brief.funding.amount_usd))
    for value in (brief.hiring.open_eng_roles, brief.hiring.ai_adjacent_eng_roles):
        if value is not None:
            facts.numbers.add(float(value))
    if brief.hiring.delta_60d and brief.hiring.confidence == Confidence.HIGH:
        facts.numbers.add(abs(float(brief.hiring.delta_60d)))
    if brief.prospect.employee_count:
        facts.numbers.add(float(brief.prospect.employee_count))
    for gap in gaps.gaps if gaps else []:
        facts.numbers.update(float(n) for n in re.findall(r"\d+(?:\.\d+)?", gap.cohort_adoption))

    # Signals SCAP withheld must not come back.
    if original.funding.event and not brief.funding.event:
        facts.forbidden.append(("withheld funding round", _word_regex(original.funding.event)))
    if original.leadership.change and not brief.leadership.change and original.leadership.role:
        role = re.escape(original.leadership.role)
        facts.forbidden.append(
            (
                "withheld leadership change",
                re.compile(
                    rf"\b(?:new|newly appointed|incoming|recently (?:appointed|hired|joined)\w*)\s+{role}\b"
                    rf"|\b{role}\s+(?:transition|change)\b",
                    re.IGNORECASE,
                ),
            )
        )
    if case.gap_brief:
        kept = {g.practice for g in (gaps.gaps if gaps else [])}
        for gap in case.gap_brief.gaps:
            if gap.practice not in kept:
                facts.forbidden.append(("filtered competitor gap", _word_regex(gap.practice)))

    # Medium-confidence facts must be hedged wherever they appear.
    if brief.funding.event and brief.funding.confidence == Confidence.MEDIUM:
        facts.hedge_required.append(("medium funding", _word_regex(brief.funding.event)))
    if brief.hiring.open_eng_roles and brief.hiring.confidence == Confidence.MEDIUM:
        facts.hedge_required.append(
            ("medium hiring", re.compile(rf"\b{brief.hiring.open_eng_roles}\b"))
        )

    roles = brief.hiring.open_eng_roles
    facts.few_roles = roles is None or roles < 5
    facts.has_layoffs = original.layoffs.event
    name = original.prospect.contact_name
    facts.first_name = name.split()[0] if name else None
    facts.timezone_known = bool(original.prospect.timezone)
    return facts


def _strip_signature(body: str, signature: str) -> str:
    """Body without the signature block (so word counts measure the message)."""
    first_line = signature.splitlines()[0].strip()
    lines = body.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == first_line:
            return "\n".join(lines[:i])
    return body


def _money_values(text: str) -> list[tuple[str, float]]:
    found = []
    for match in _MONEY.finditer(text):
        if match.group(1):
            value = float(match.group(1).replace(",", ""))
            unit = (match.group(2) or "").lower()
        else:
            value = float(match.group(3))
            unit = match.group(4).lower()
        found.append((match.group(0).strip(), value * _MULTIPLIERS.get(unit, 1)))
    return found


_SENDER_WORDS = re.compile(r"\b(?:we|we're|we've|our|us|the team)\b", re.IGNORECASE)


def _ungrounded_numbers(sentences: list[str], facts: Facts) -> list[str]:
    problems = []
    for sentence in sentences:
        about_sender = bool(_SENDER_WORDS.search(sentence)) or any(
            name.lower() in sentence.lower() for name in facts.sender_names
        )
        allowed = facts.numbers | facts.sender_numbers if about_sender else facts.numbers
        problems += _ungrounded_in(sentence, facts.money, allowed)
    return problems


def _ungrounded_in(text: str, money: set[float], numbers: set[float]) -> list[str]:
    problems = []
    for raw, value in _money_values(text):
        if not any(abs(value - allowed) <= allowed * 0.02 for allowed in money):
            problems.append(raw)
    without_money = _MONEY.sub(" ", text)
    for match in _NUMBER.finditer(without_money):
        rest = without_money[match.end() :]
        value = float(match.group(1))
        is_year = match.group(2) == "" and value.is_integer() and 1990 <= value <= 2100
        if (
            is_year
            or _UNIT_AFTER.match(rest)
            or _ORDINAL_AFTER.match(rest)
            or _TIME_AFTER.match(rest)
        ):
            continue
        if value not in numbers:
            problems.append(match.group(0))
    return problems


def check_draft(case: EvalCase, draft: Draft, playbook: Playbook) -> list[CheckResult]:
    facts = derive_facts(case, playbook)
    voice = playbook.voice
    signature = playbook.company.signature
    message = _strip_signature(draft.body, signature)
    full_text = f"{draft.subject}\n{message}"
    sentences = [s for s in _SENTENCE.findall(full_text) if s.strip()]
    results: list[CheckResult] = []

    def add(name: str, problems: list[str]) -> None:
        results.append(CheckResult(name, not problems, "; ".join(problems)))

    add("numbers_grounded", [f"ungrounded {p!r}" for p in _ungrounded_numbers(sentences, facts)])
    add(
        "no_withheld_signals",
        [
            f"{label}: {m.group(0)!r}"
            for label, rx in facts.forbidden
            if (m := rx.search(full_text))
        ],
    )
    add("no_layoff_mention", [repr(m.group(0)) for m in _LAYOFF.finditer(full_text)])
    add(
        "no_aggressive_hiring",
        [repr(m.group(0)) for m in _AGGRESSIVE.finditer(full_text)] if facts.few_roles else [],
    )
    unhedged = []
    for label, rx in facts.hedge_required:
        for sentence in sentences:
            if rx.search(sentence) and not _HEDGE.search(sentence):
                unhedged.append(f"{label}: {sentence.strip()!r}")
    add("medium_hedged", unhedged)
    add("no_internal_jargon", [repr(m.group(0)) for m in _JARGON.finditer(full_text)])

    greeting_problems = []
    greeting = _GREETING.match(message)
    if greeting:
        greeted = greeting.group(1).strip().split()[0].lower()
        allowed = set(_GENERIC_GREETINGS)
        if facts.first_name:
            allowed.add(facts.first_name.lower())
        allowed.update(w.lower() for w in case.brief.prospect.company.split())
        if greeted not in allowed:
            greeting_problems.append(f"greets {greeting.group(1).strip()!r}")
    add("greeting_grounded", greeting_problems)
    add(
        "no_invented_times",
        []
        if facts.timezone_known or not draft.proposed_times
        else [f"{len(draft.proposed_times)} proposed time(s) without a known timezone"],
    )

    words = len(message.split())
    add(
        "word_limit",
        [f"{words} words > {voice.max_words.cold}"] if words > voice.max_words.cold else [],
    )
    add(
        "no_banned_phrases",
        [repr(p) for p in voice.banned_phrases if _word_regex(p).search(full_text)],
    )
    subject_problems = []
    if len(draft.subject) >= 60:
        subject_problems.append(f"{len(draft.subject)} chars")
    if voice.subject_prefixes and not any(
        draft.subject.lower().startswith(p.lower()) for p in voice.subject_prefixes
    ):
        subject_problems.append(f"no allowed prefix: {draft.subject!r}")
    add("subject_rules", subject_problems)
    missing = [line for line in signature.splitlines() if line.strip() and line not in draft.body]
    add("signature_present", [f"missing {line!r}" for line in missing])
    return results
