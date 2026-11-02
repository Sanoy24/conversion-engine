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
from agent.playbook import get_playbook
from evals.cases import EvalCase, load_cases
from evals.checks import ALL_CHECKS, GROUNDING_CHECKS, Draft, check_draft

RECORDINGS_DIR = Path(__file__).with_name("recordings")


def prompt_hash(case: EvalCase) -> str:
    """Hash of the drafter prompts for a case: changes when the drafter's prompt does."""
    from agent.core.email_drafter import build_draft_prompts

    prompts = build_draft_prompts(
        case.brief, classify_prospect(case.brief), gap_brief=case.gap_brief
    )
    return hashlib.sha256((prompts.system + prompts.user).encode()).hexdigest()[:12]


def fingerprint(case: EvalCase) -> str:
    payload = case.brief.model_dump_json() + (
        case.gap_brief.model_dump_json() if case.gap_brief else ""
    )
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


async def record(cases: list[EvalCase], model: str | None) -> Path:
    from agent import llm
    from agent.core.email_drafter import draft_email
    from agent.models import EmailType

    if not settings.openrouter_api_key:
        sys.exit("OPENROUTER_API_KEY is not set: recording needs a real model.")
    if model:
        settings.dev_model = settings.eval_model = model
        llm._default_client = None
    entries = {}
    for case in cases:
        classification = classify_prospect(case.brief)
        print(f"drafting {case.id} ({classification.segment}) ...", file=sys.stderr)
        draft, _ = await draft_email(
            signal_brief=case.brief,
            classification=classification,
            email_type=EmailType.COLD,
            gap_brief=case.gap_brief,
            thread_id=f"eval_{case.id}",
        )
        entries[case.id] = {
            "subject": draft.subject,
            "body": draft.body,
            "proposed_times": [t.model_dump() for t in draft.proposed_times],
            "tone_score": draft.tone_check_score,
        }
    return save_recording(settings.active_model, cases, entries)


def save_recording(model_name: str, cases: list[EvalCase], entries: dict[str, dict]) -> Path:
    """Merge drafts for `cases` into evals/recordings/<model>.json."""
    path = RECORDINGS_DIR / f"{model_name.replace('/', '__')}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"drafts": {}}
    drafts = existing["drafts"]
    for case in cases:
        if case.id not in entries:
            continue
        drafts[case.id] = {
            "fingerprint": fingerprint(case),
            "prompt_hash": prompt_hash(case),
            "segment": classify_prospect(case.brief).segment,
            **entries[case.id],
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"model": model_name, "drafts": drafts}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"saved {len(entries)} draft(s) to {path}", file=sys.stderr)
    return path


def export_prompts(cases: list[EvalCase], path: Path) -> None:
    """Write the exact drafter prompts per case, for drafting outside the app."""
    from agent.core.email_drafter import build_draft_prompts

    payload = {}
    for case in cases:
        prompts = build_draft_prompts(
            case.brief, classify_prospect(case.brief), gap_brief=case.gap_brief
        )
        payload[case.id] = {"system": prompts.system, "user": prompts.user}
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
    unknown = set(entries) - {c.id for c in cases}
    if unknown:
        sys.exit(f"unknown case ids in import: {sorted(unknown)}")
    return save_recording(model_name, cases, entries)


# ── Scoring ───────────────────────────────────────────────────────────


def score_recording(path: Path, cases: list[EvalCase]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    playbook = get_playbook()
    by_id = {c.id: c for c in cases}
    rows, stale, missing, old_prompt = [], [], [], []
    for case_id, case in by_id.items():
        entry = data["drafts"].get(case_id)
        if entry is None:
            missing.append(case_id)
            continue
        if entry.get("fingerprint") != fingerprint(case):
            stale.append(case_id)
            continue
        if entry.get("prompt_hash") != prompt_hash(case):
            old_prompt.append(case_id)
        draft = Draft(entry["subject"], entry["body"], entry.get("proposed_times", []))
        results = check_draft(case, draft, playbook)
        rows.append(
            {
                "case": case_id,
                "failed": {r.name: r.detail for r in results if not r.passed},
            }
        )
    failures = Counter(name for row in rows for name in row["failed"])
    return {
        "recording": path.name,
        "model": data.get("model"),
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
    print(f"\n## Drafts: {result['model']} ({result['recording']})\n")
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
    parser.add_argument("--json", type=Path, help="also write results to this JSON file")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        unknown = wanted - {c.id for c in cases}
        if unknown:
            parser.error(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in wanted]

    output: dict = {}
    if args.command == "record":
        asyncio.run(record(cases, args.model))
        return 0
    if args.command == "prompts":
        if len(args.paths) != 1:
            parser.error("prompts needs one output path")
        export_prompts(cases, args.paths[0])
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
        output["drafts"] = [score_recording(p, cases) for p in paths]
        for result in output["drafts"]:
            print_score(result)
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
