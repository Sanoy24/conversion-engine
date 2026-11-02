"""The eval suite as regression tests: classifier cases, and the draft checks themselves."""

from __future__ import annotations

import json

import pytest

from agent.core.icp_classifier import classify_prospect
from agent.playbook import get_playbook
from evals.cases import load_cases
from evals.checks import Draft, check_draft
from evals.run import RECORDINGS_DIR, score_recording

CASES = {case.id: case for case in load_cases()}
SIGNATURE = "Sam\nResearch Partner\nAcme Engineering\nacme.example"


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
def test_classifier_picks_expected_segment(case):
    assert classify_prospect(case.brief).segment == case.expected_segment, case.note


def _failures(case_id: str, body: str, subject: str = "Question on your roadmap", times=None):
    draft = Draft(subject=subject, body=f"{body}\n\n{SIGNATURE}", proposed_times=times or [])
    return {
        r.name: r.detail for r in check_draft(CASES[case_id], draft, get_playbook()) if not r.passed
    }


GOOD = (
    "Hi Daniel,\n\nCongrats on the $22M Series B. From the public postings it looks like you "
    "have around 7 engineering roles open, is that right?\n\nIf shipping faster than hiring "
    "allows is on your list, the team at Acme could help with a scoped project in Q3. "
    "Worth a 20-minute call in 2026?"
)


def test_good_draft_passes_every_check():
    assert _failures("funded_medium_hiring", GOOD) == {}


@pytest.mark.parametrize(
    ("case_id", "body", "check"),
    [
        ("funded_medium_hiring", "Hi Daniel, congrats on the $25M Series B.", "numbers_grounded"),
        ("funded_medium_hiring", "Hi Daniel, 3 of your peers already do this.", "numbers_grounded"),
        (
            "gap_low_filtered",
            "Hi Tomas, 4 of 6 peers run vector search in production.",
            "numbers_grounded",
        ),
        (
            "gap_low_filtered",
            "Hi Tomas, have you looked at vector search in production?",
            "no_withheld_signals",
        ),
        (
            "funded_low_confidence_funding",
            "Hi Mei, congrats on the Series A.",
            "no_withheld_signals",
        ),
        (
            "leadership_low_confidence_with_funding",
            "Hi Arjun, congrats on the new VP Engineering.",
            "no_withheld_signals",
        ),
        (
            "layoff_midsize",
            "Hi Grace, after the recent layoffs, capacity is tight.",
            "no_layoff_mention",
        ),
        ("layoff_midsize", "Hi Grace, restructuring is never easy.", "no_layoff_mention"),
        ("funded_few_roles", "Hi Ben, you're hiring aggressively.", "no_aggressive_hiring"),
        ("funded_medium_hiring", "Hi Daniel, you have 7 open engineering roles.", "medium_hedged"),
        ("medium_funding_high_hiring", "Hi Felix, you closed a Series B.", "medium_hedged"),
        ("ai_capability_high", "Hi Sofia, your AI maturity score is high.", "no_internal_jargon"),
        ("unknown_contact_and_timezone", "Hi John, quick question.", "greeting_grounded"),
        ("funded_medium_hiring", "Hi Daniel, " + "word " * 130, "word_limit"),
        ("funded_medium_hiring", "Hi Daniel, our world-class team can help.", "no_banned_phrases"),
    ],
)
def test_checks_catch_violations(case_id, body, check):
    assert check in _failures(case_id, body)


def test_sender_numbers_only_in_sentences_about_the_sender():
    ours = "Hi Grace, our dedicated teams typically run 3 to 12 engineers."
    theirs = "Hi Grace, 3 of 12 peers already outsource this."
    assert "numbers_grounded" not in _failures("layoff_midsize", ours)
    assert "numbers_grounded" in _failures("layoff_midsize", theirs)


def test_numbers_written_as_words_are_checked():
    # funded_clean: 12 open roles
    assert "numbers_grounded" in _failures("funded_clean", "Hi Priya, you have fifteen open roles.")
    assert "numbers_grounded" in _failures(
        "funded_clean", "Hi Priya, congrats on the fifteen million raise."
    )
    assert "numbers_grounded" not in _failures(
        "funded_clean", "Hi Priya, you have twelve open roles."
    )
    assert "numbers_grounded" not in _failures(
        "funded_clean", "Hi Priya, a dozen open roles is a lot."
    )
    assert "numbers_grounded" not in _failures("funded_clean", "Hi Priya, one quick question.")


def test_ranges_and_self_references_are_not_claims():
    assert "numbers_grounded" not in _failures("funded_clean", "Hi Priya, a 2 to 6 month project.")
    times = [{"prospect_local": "Tue 10:00", "utc": "Tue 15:00"}] * 2
    assert "numbers_grounded" not in _failures(
        "funded_clean", "Hi Priya, I have proposed two times below.", times=times
    )


def test_follow_up_rules():
    from agent.models import EmailType

    def follow_up_failures(body: str) -> dict:
        draft = Draft(subject="Question on hiring", body=body + "\n\n" + SIGNATURE)
        results = check_draft(CASES["funded_clean"], draft, get_playbook(), EmailType.RE_ENGAGEMENT)
        return {r.name: r.detail for r in results if not r.passed}

    assert "no_chaser_phrases" in follow_up_failures(
        "Hi Priya, just following up on my last email."
    )
    assert "no_chaser_phrases" in follow_up_failures("Hi Priya, circling back on this.")
    assert "no_chaser_phrases" not in follow_up_failures("Hi Priya, one more angle on hiring.")
    # Follow-ups have their own (shorter) word limit: 70 in the example playbook.
    assert "word_limit" in follow_up_failures("Hi Priya, " + "word " * 80)
    # Chaser phrases are only a follow-up rule.
    assert "no_chaser_phrases" not in _failures("funded_clean", "Hi Priya, just following up.")


def test_hedged_medium_claims_pass():
    assert "medium_hedged" not in _failures(
        "medium_funding_high_hiring", "Hi Felix, it looks like you closed a Series B recently?"
    )


def test_generic_greeting_is_fine_without_a_name():
    assert "greeting_grounded" not in _failures(
        "unknown_contact_and_timezone", "Hi there, quick question."
    )


def test_invented_times_without_timezone():
    times = [{"prospect_local": "Tue 10:00", "utc": "Tue 15:00"}]
    assert "no_invented_times" in _failures(
        "unknown_contact_and_timezone", "Hi there.", times=times
    )
    assert "no_invented_times" not in _failures("funded_clean", "Hi Priya.", times=times)


def test_subject_and_signature_rules():
    failures = _failures("funded_medium_hiring", "Hi Daniel.", subject="Let's talk")
    assert "subject_rules" in failures
    draft = Draft(subject="Question on hiring", body="Hi Daniel.\n\nSam")
    names = {
        r.name
        for r in check_draft(CASES["funded_medium_hiring"], draft, get_playbook())
        if not r.passed
    }
    assert "signature_present" in names


@pytest.mark.parametrize("path", sorted(RECORDINGS_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_recordings_are_current(path):
    """Recordings must match the current cases; re-record after changing a case."""
    from evals.run import EMAIL_TYPES

    for email_type in EMAIL_TYPES:
        result = score_recording(path, list(CASES.values()), email_type)
        assert not result["stale"], f"re-record: {result['stale']}"


def test_prompts_export_and_import_round_trip(tmp_path, monkeypatch):
    from evals import run

    monkeypatch.setattr(run, "RECORDINGS_DIR", tmp_path / "recordings")
    cases = [CASES["funded_medium_hiring"]]

    prompts_path = tmp_path / "prompts.json"
    run.export_prompts(cases, prompts_path)
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
    assert "Lumen Ledger" in prompts["funded_medium_hiring"]["user"]
    assert "OUTPUT FORMAT" in prompts["funded_medium_hiring"]["system"]

    drafts_path = tmp_path / "drafts.json"
    drafts_path.write_text(
        json.dumps(
            {
                "funded_medium_hiring": {
                    "subject": "Question on hiring",
                    "body": f"{GOOD}\n\n{SIGNATURE}",
                }
            }
        ),
        encoding="utf-8",
    )
    recording = run.import_drafts(cases, "test/model", [drafts_path])
    result = run.score_recording(recording, cases)
    assert result["scored"] == 1
    assert result["clean"] == 1
