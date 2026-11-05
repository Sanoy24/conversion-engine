"""
Run the quality evaluations.

    uv run python -m evals.run                    # classifier + score every recording
    uv run python -m evals.run classify           # classifier accuracy only (no LLM)
    uv run python -m evals.run record [--model M] [--cases a,b]
                                                  # draft every case with a real model
    uv run python -m evals.run score [PATH ...]   # score recorded drafts
    uv run python -m evals.run prompts OUT.json   # export the exact drafter prompts
    uv run python -m evals.run import --model NAME DRAFTS.json [...]
                                                  # record drafts made outside the app

Every case is evaluated as a cold email and as a follow-up (re_engagement,
replying to a fixed neutral first email). `--type cold|re_engagement` limits
record/prompts/score to one of them. Recordings key cold drafts by case id and
follow-ups by `<case id>@re_engagement`.

`record` calls the drafter exactly as production does (SCAP on, tone check,
example playbook) and saves the drafts to evals/recordings/<model>.json, so
scoring is free and repeatable. `prompts` + `import` do the same for a model
reached some other way (no tone-check regeneration in that case). Recordings
remember a fingerprint of each case; when a case changes, its recording is
reported as stale.

Add --json PATH to any command to also write the results as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from agent.config import settings
from agent.core.icp_classifier import classify_prospect
from agent.models import EmailType
from agent.playbook import get_playbook
from evals.cases import EvalCase, load_cases, previous_email
from evals.checks import ALL_CHECKS, GROUNDING_CHECKS, Draft, check_draft

RECORDINGS_DIR = Path(__file__).with_name("recordings")
EMAIL_TYPES = (EmailType.COLD, EmailType.RE_ENGAGEMENT)


def draft_key(case: EvalCase, email_type: EmailType) -> str:
    return case.id if email_type == EmailType.COLD else f"{case.id}@{email_type.value}"


def draft_inputs(case: EvalCase, email_type: EmailType) -> dict:
    """The drafter's inputs for a case, mirroring production for each email type."""
    inputs: dict = {
        "signal_brief": case.brief,
        "classification": classify_prospect(case.brief),
        "email_type": email_type,
    }
    if email_type == EmailType.COLD:
        inputs["gap_brief"] = case.gap_brief
    else:
        signature = get_playbook().company.signature
        inputs["thread_history"] = [
            {
                "role": "agent",
                "content": previous_email(case, signature),
                "timestamp": "2026-10-01T09:00:00",
            }
        ]
    return inputs


def prompt_hash(case: EvalCase, email_type: EmailType = EmailType.COLD) -> str:
    """Hash of the drafter prompts: changes when the drafter's prompt does."""
    from agent.core.email_drafter import build_draft_prompts

    prompts = build_draft_prompts(**draft_inputs(case, email_type))
    return hashlib.sha256((prompts.system + prompts.user).encode()).hexdigest()[:12]


def fingerprint(case: EvalCase, email_type: EmailType = EmailType.COLD) -> str:
    # sample_titles is left out so adding that optional field didn't invalidate
    # every recording. A case that sets it still changes prompt_hash.
    payload = case.brief.model_dump_json(exclude={"hiring": {"sample_titles"}}) + (
        case.gap_brief.model_dump_json() if case.gap_brief else ""
    )
    if email_type != EmailType.COLD:
        payload += email_type.value + previous_email(case, get_playbook().company.signature)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# ── Classification ────────────────────────────────────────────────────


def run_classification(cases: list[EvalCase]) -> dict:
    rows = []
    for case in cases:
        got = classify_prospect(case.brief).segment
        rows.append({"case": case.id, "expected": case.expected_segment, "got": got})
    correct = sum(r["expected"] == r["got"] for r in rows)
    return {"correct": correct, "total": len(rows), "rows": rows}


def print_classification(result: dict) -> None:
    print(f"\n## Classification: {result['correct']}/{result['total']} correct\n")
    misses = [r for r in result["rows"] if r["expected"] != r["got"]]
    if misses:
        print("| case | expected | got |\n| --- | --- | --- |")
        for r in misses:
            print(f"| {r['case']} | {r['expected']} | {r['got']} |")


# ── Recording ─────────────────────────────────────────────────────────


async def record(
    cases: list[EvalCase], model: str | None, email_types: tuple[EmailType, ...] = EMAIL_TYPES
) -> Path:
    from agent import llm
    from agent.core.email_drafter import draft_email

    if not settings.openrouter_api_key:
        sys.exit("OPENROUTER_API_KEY is not set: recording needs a real model.")
    if model:
        settings.dev_model = settings.eval_model = model
        llm._default_client = None
    entries = {}
    for case in cases:
        for email_type in email_types:
            key = draft_key(case, email_type)
            print(f"drafting {key} ...", file=sys.stderr)
            draft, _ = await draft_email(**draft_inputs(case, email_type), thread_id=f"eval_{key}")
            entries[key] = {
                "subject": draft.subject,
                "body": draft.body,
                "proposed_times": [t.model_dump() for t in draft.proposed_times],
                "tone_score": draft.tone_check_score,
            }
    return save_recording(settings.active_model, cases, entries)


def save_recording(model_name: str, cases: list[EvalCase], entries: dict[str, dict]) -> Path:
    """Merge drafts for `cases` into evals/recordings/<model>.json."""
    path = RECORDINGS_DIR / f"{model_name.replace('/', '__').replace(':', '_')}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"drafts": {}}
    drafts = existing["drafts"]
    for case in cases:
        for email_type in EMAIL_TYPES:
            key = draft_key(case, email_type)
            if key not in entries:
                continue
            drafts[key] = {
                "fingerprint": fingerprint(case, email_type),
                "prompt_hash": prompt_hash(case, email_type),
                "segment": classify_prospect(case.brief).segment,
                **entries[key],
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"model": model_name, "drafts": drafts}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"saved {len(entries)} draft(s) to {path}", file=sys.stderr)
    return path


def export_prompts(
    cases: list[EvalCase], path: Path, email_types: tuple[EmailType, ...] = EMAIL_TYPES
) -> None:
    """Write the exact drafter prompts per draft key, for drafting outside the app."""
    from agent.core.email_drafter import build_draft_prompts

    payload = {}
    for case in cases:
        for email_type in email_types:
            prompts = build_draft_prompts(**draft_inputs(case, email_type))
            payload[draft_key(case, email_type)] = {"system": prompts.system, "user": prompts.user}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote prompts for {len(payload)} case(s) to {path}", file=sys.stderr)


def import_drafts(cases: list[EvalCase], model_name: str, paths: list[Path]) -> Path:
    """
    Record drafts produced outside the app: JSON files mapping case id to the
    drafter's output object ({"subject", "body", "proposed_times", ...}).
    """
    entries: dict[str, dict] = {}
    for path in paths:
        for case_id, output in json.loads(path.read_text(encoding="utf-8")).items():
            entries[case_id] = {
                "subject": output.get("subject", ""),
                "body": output.get("body", ""),
                "proposed_times": output.get("proposed_times", []),
                "tone_score": None,
            }
    unknown = set(entries) - {draft_key(c, t) for c in cases for t in EMAIL_TYPES}
    if unknown:
        sys.exit(f"unknown case ids in import: {sorted(unknown)}")
    return save_recording(model_name, cases, entries)


# ── Scoring ───────────────────────────────────────────────────────────


def score_recording(
    path: Path, cases: list[EvalCase], email_type: EmailType = EmailType.COLD
) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    playbook = get_playbook()
    rows, stale, missing, old_prompt = [], [], [], []
    for case in cases:
        key = draft_key(case, email_type)
        entry = data["drafts"].get(key)
        if entry is None:
            missing.append(key)
            continue
        if entry.get("fingerprint") != fingerprint(case, email_type):
            stale.append(key)
            continue
        if entry.get("prompt_hash") != prompt_hash(case, email_type):
            old_prompt.append(key)
        draft = Draft(entry["subject"], entry["body"], entry.get("proposed_times", []))
        results = check_draft(case, draft, playbook, email_type)
        rows.append(
            {
                "case": key,
                "failed": {r.name: r.detail for r in results if not r.passed},
            }
        )
    failures = Counter(name for row in rows for name in row["failed"])
    return {
        "recording": path.name,
        "model": data.get("model"),
        "email_type": email_type.value,
        "scored": len(rows),
        "clean": sum(not row["failed"] for row in rows),
        "grounded": sum(not set(row["failed"]) & set(GROUNDING_CHECKS) for row in rows),
        "pass_rates": {name: len(rows) - failures[name] for name in ALL_CHECKS},
        "rows": rows,
        "stale": stale,
        "missing": missing,
        "old_prompt": old_prompt,
    }


def print_score(result: dict) -> None:
    n = result["scored"]
    if not n and not result["stale"]:
        return  # nothing recorded for this email type
    print(f"\n## {result['email_type']} drafts: {result['model']} ({result['recording']})\n")
    if n:
        print(f"- fully clean: {result['clean']}/{n}")
        print(f"- no grounding violations: {result['grounded']}/{n}\n")
        print("| check | passed |\n| --- | --- |")
        for name, passed in result["pass_rates"].items():
            print(f"| {name} | {passed}/{n} |")
        failed_rows = [row for row in result["rows"] if row["failed"]]
        if failed_rows:
            print("\n### Failures\n")
            for row in failed_rows:
                for name, detail in row["failed"].items():
                    print(f"- `{row['case']}` {name}: {detail}")
    if result["stale"]:
        print(f"\nstale (case changed since recording, re-record): {', '.join(result['stale'])}")
    if result["old_prompt"]:
        print(
            f"\nnote: {len(result['old_prompt'])} draft(s) were recorded with an older drafter "
            "prompt; re-record to measure the current one."
        )
    if result["missing"]:
        print(f"not recorded: {', '.join(result['missing'])}")


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "classify", "record", "score", "prompts", "import"],
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="score: recordings; prompts: output file; import: draft files",
    )
    parser.add_argument(
        "--model", help="record: model id (default: configured); import: name for the recording"
    )
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument(
        "--type",
        choices=[t.value for t in EMAIL_TYPES],
        help="record/prompts/score only this email type (default: both)",
    )
    parser.add_argument("--json", type=Path, help="also write results to this JSON file")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        unknown = wanted - {c.id for c in cases}
        if unknown:
            parser.error(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in wanted]

    email_types = (EmailType(args.type),) if args.type else EMAIL_TYPES
    output: dict = {}
    if args.command == "record":
        asyncio.run(record(cases, args.model, email_types))
        return 0
    if args.command == "prompts":
        if len(args.paths) != 1:
            parser.error("prompts needs one output path")
        export_prompts(cases, args.paths[0], email_types)
        return 0
    if args.command == "import":
        if not (args.model and args.paths):
            parser.error("import needs --model NAME and at least one draft file")
        import_drafts(cases, args.model, args.paths)
        return 0
    if args.command in ("all", "classify"):
        output["classification"] = run_classification(cases)
        print_classification(output["classification"])
    if args.command in ("all", "score"):
        paths = args.paths or sorted(RECORDINGS_DIR.glob("*.json"))
        if not paths:
            print("\nNo recordings yet. Run `python -m evals.run record` first.")
        output["drafts"] = [score_recording(p, cases, t) for p in paths for t in email_types]
        for result in output["drafts"]:
            print_score(result)
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
