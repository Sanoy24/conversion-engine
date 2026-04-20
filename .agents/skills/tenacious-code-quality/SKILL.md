---
name: tenacious-code-quality
description: Apply pragmatic Python and TypeScript/JavaScript code-quality conventions to the Tenacious Conversion Engine codebase — ruff + mypy + pytest for Python, eslint + prettier + tsc for TS/JS, with drop-in configs bundled. Use this skill whenever the user writes a new module, refactors existing code, adds a test, sets up tooling, fixes linter errors, or asks about imports, type hints, naming, dead code, error handling, logging, or any phrasing like "clean up this code," "make this production-ready," or "what does good look like here." Calibrated for a one-week sprint — pragmatic not strict. Configs in the assets/ directory are authoritative; copy them in rather than rewriting.
---

# Tenacious Code Quality

A pragmatic code-quality skill for the one-week conversion-engine build. The goal is "code that is easy to change on Day 5 when the target failure mode shifts" — not "code that would pass a Google readability review." The difference matters during a sprint.

## What this skill covers vs. does not cover

**Covers:**
- Python linting (ruff), formatting (ruff format), type checking (mypy)
- TS/JS linting (eslint), formatting (prettier), type checking (tsc)
- Testing conventions (pytest for Python; vitest or jest for TS, whichever fits)
- Naming, imports, error handling, logging, file organization
- Config files you can drop into the repo — see `assets/`

**Does not cover:**
- Business-logic correctness for Tenacious (that is in the domain skills — `tenacious-signal-brief`, `tenacious-email-drafter`, etc.)
- Architecture decisions (framework choice, database schema, deployment topology)
- Performance tuning (premature for a one-week sprint)
- Security hardening beyond the obvious (no secrets in code, input validation at API boundaries)

## The pragmatic rule

Every rule below has a single test: **if this rule catches a bug or speeds up an edit once in the week, it is worth the friction; if it does not, it is not.** When in doubt, lean toward less friction. The sprint is seven days and the grade is based on working output and a two-page memo, not codebase elegance.

## Drop-in configs (see `assets/`)

These files are ready to copy into the repo root. Do not rewrite them; copying is faster and the settings are already calibrated for pragmatic strictness.

| File | Drop into | What it does |
| --- | --- | --- |
| `pyproject.toml` | Repo root (merge with existing if present) | ruff + ruff format + mypy + pytest config |
| `.pre-commit-config.yaml` | Repo root | Runs ruff, ruff format, and (optionally) mypy on staged files |
| `eslint.config.js` | Repo root (flat config, ESLint 9+) | TS/JS linting with sane defaults |
| `.prettierrc.json` | Repo root | Formatting config compatible with the eslint config |
| `tsconfig.json` | Repo root (TS projects only) | Pragmatic strictness — `strict: true` but with `noUnusedLocals: warn`-level |
| `.gitignore` | Repo root | Python + Node + common IDE ignores |

Install commands are in `assets/INSTALL.md`.

## Python conventions

### Imports

Three groups, separated by one blank line, in this order:

1. Standard library
2. Third-party
3. Local (first-party)

Within each group, alphabetical. Ruff handles this automatically via the `I` ruleset — do not reformat by hand.

```python
import json
import logging
from pathlib import Path

import httpx
from pydantic import BaseModel

from tenacious.enrichment import fetch_company
from tenacious.models import Prospect
```

Rules:
- No wildcard imports (`from x import *`). Ruff catches these.
- No conditional imports in the middle of a file unless gated on a platform or optional dependency. Put them at the top.
- Absolute imports over relative. `from tenacious.enrichment import ...` beats `from ..enrichment import ...`.

### Naming

- `snake_case` for functions, methods, variables, modules.
- `PascalCase` for classes and type aliases.
- `SCREAMING_SNAKE_CASE` for module-level constants only. Not for enum values (those are PascalCase in Python).
- Single-letter names only in short comprehensions (`[x for x in xs]`) or well-established conventions (`i`, `k`, `v` for index/key/value).
- Avoid abbreviations unless they are universally recognized: `config` over `cfg`, `response` over `resp`, but `url`, `id`, `api` are fine.
- Private helpers prefixed with `_`. Do not rely on the single underscore for encapsulation — it's a hint to readers, not an enforcement mechanism.

### Type hints — pragmatic stance

Required:
- Function signatures (parameters and return types) for any function exposed outside its module.
- Data class fields (use `dataclass` or `pydantic.BaseModel`; both preferred over bare dicts for anything that crosses a module boundary).

Not required:
- Local variables inside short functions (mypy infers these well).
- Very small private helpers where the body is self-explanatory.

Use `from __future__ import annotations` at the top of every module that uses type hints. This makes `list[str]` and `X | None` work on Python 3.10+ without quotes.

```python
from __future__ import annotations

def enrich_prospect(crunchbase_id: str, *, live_crawl: bool = False) -> EnrichmentBundle | None:
    ...
```

### No unused code

Ruff flags:
- Unused imports (`F401`)
- Unused local variables (`F841`)
- Unused function arguments (`ARG001`, `ARG002`) — **with one exception:** callback signatures and protocol implementations sometimes need arguments they do not use. Prefix those with `_` (e.g., `_event`) to suppress the warning intentionally.

Unused-code rules apply to production code. Test files may have intentionally unused fixtures, so the ruff config disables the unused-arg check in `tests/`.

### Error handling

Pragmatic rules for a sprint:
- **Never bare-except.** `except Exception as e:` minimum. `except SpecificError:` preferred.
- **Never silently swallow.** Even if you want to continue, log it at WARNING with the exception info.
- **Fail loudly in development, gracefully in production.** The enrichment pipeline's scraper wrappers should log and return `None`; the API handlers in front of them should decide whether `None` is a user-facing 404 or an internal 500.
- **No `assert` in production code paths.** Assertions disappear under `python -O`. Use explicit `raise ValueError(...)` for preconditions.

Example of the standard pattern for the scraper:

```python
def fetch_team_page(domain: str) -> TeamPage | None:
    try:
        response = httpx.get(f"https://{domain}/team", timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("team_page_fetch_failed", extra={"domain": domain, "error": str(e)})
        return None
    return _parse_team_page(response.text)
```

### Logging

- Use the `logging` module, not `print`. One logger per module: `logger = logging.getLogger(__name__)`.
- Structured logging via `extra={...}` dict or a structured-logging library if you have one. Avoids f-string log messages because those can't be filtered by field.
- Log levels:
  - `DEBUG`: developer-only diagnostic.
  - `INFO`: normal operational events (request received, scrape completed).
  - `WARNING`: something unexpected but recovered (scrape failed, fell back to snapshot).
  - `ERROR`: operation failed in a way that matters (DB write failed, external API returned 5xx).
  - `CRITICAL`: reserved for kill-switch-worthy events (the kill-switch trigger in the memo).

### Testing

Pragmatic: **test the risky paths, skip the obvious ones.** Risky = anything the grader or the CEO would notice if it broke. Obvious = one-liner getters, constructor assignments.

Priority for this project:
1. Signal brief scoring (AI-maturity logic, confidence calibration) — unit tests with fixture briefs.
2. ICP classifier (segment overlap rules, abstention threshold) — table-driven tests with clear inputs and expected segments.
3. Bench-gating logic in the email drafter — prevent commitment regressions.
4. Enrichment pipeline's failure handling — test that a 429 backs off, a captcha returns None.

Not priority:
- Testing that a dataclass constructor assigns fields.
- Testing that logging calls fire.
- Testing third-party library behavior.

pytest conventions:
- Test files named `test_*.py`.
- Fixtures in `conftest.py` at the appropriate level.
- One assertion per test where practical; multiple if they're all aspects of one behavior.
- Use `pytest.mark.parametrize` for table-driven tests — much clearer than loops.

## TypeScript / JavaScript conventions

Narrower scope than Python — TS/JS is for any UI or Node-based integration work (e.g., a HubSpot MCP shim). The same pragmatic calibration applies.

### Imports

Same three-group pattern: node built-ins, third-party, local. Prettier and eslint-plugin-simple-import-sort handle ordering.

Use ESM imports (`import { x } from 'y'`), not CommonJS (`require`), except where a library forces otherwise.

### Types

- TypeScript over JavaScript for anything non-trivial. The `tsconfig.json` in assets has `strict: true` but downgrades `noUnusedLocals` and `noUnusedParameters` to warnings, matching the Python calibration.
- Explicit return types on exported functions. Local types inferred.
- `interface` for object shapes, `type` for unions and utilities. Do not mix conventions arbitrarily.
- `unknown` over `any`. If you must use `any`, comment why.

### Error handling

Same pragmatic stance as Python:
- Catch specific errors where possible.
- Never silently swallow.
- Promise rejections must be handled explicitly — `await` without a `try/catch` in an async function propagates, which is usually right, but only at the right boundary.

### React-specific (if you build a UI)

- Functional components + hooks only. No class components.
- Props typed via `interface`.
- No inline handlers that allocate objects or functions in hot-path lists — move them to `useCallback` only if profiling shows a problem, otherwise do not preemptively optimize.
- Keys on list items must be stable IDs, never array indices.

## File organization

Recommended Python layout for the Tenacious repo:

```
tenacious/
  __init__.py
  enrichment/          # upstream scraper layer (tenacious-playwright-enrichment)
    __init__.py
    crunchbase.py
    job_posts.py
    github_org.py
    layoffs.py
    press.py
  signals/             # hiring signal brief + competitor gap (tenacious-signal-brief)
    __init__.py
    ai_maturity.py
    brief.py
    competitor_gap.py
  classifier/          # ICP classifier (tenacious-icp-classifier)
    __init__.py
    segments.py
  outreach/            # email drafter + voice rig (tenacious-email-drafter, tenacious-voice-rig)
    __init__.py
    drafter.py
    tone_check.py
    voice.py
  harness/             # τ²-bench + probes (tau2-bench-probes)
    __init__.py
    runner.py
    probes/
  cli/                 # operator commands
    __init__.py
  config.py            # env vars, paths, constants
tests/
  (mirrors tenacious/ structure)
seeds/                 # real seed materials (or seeds_placeholder/ pre-Day-0)
data/
  snapshots/
  enrichment_bundles/
traces/
  langfuse/
pyproject.toml
.pre-commit-config.yaml
.gitignore
README.md
```

Why this layout:
- Module boundaries align with skill boundaries. When you refactor `tenacious-signal-brief`, you edit `tenacious/signals/`, not six scattered files.
- Tests mirror source. Easy to find, easy to run in parallel.
- Seeds, data, and traces at the top level. They are artifacts, not code.

## The "don't bikeshed" list

Things not worth arguing about in a one-week sprint. The configs pick a default; live with it:

- Line length: 100 characters (ruff default is 88; 100 fits modern screens better without being excessive).
- Quote style: double quotes (ruff format default).
- Trailing commas: required in multiline (ruff enforces).
- Import sort order: ruff's I ruleset decides; do not re-sort by hand.
- Naming of test functions: `test_<what>_<when>_<then>` is nice but not enforced — `test_<what>` is fine.

## Pre-commit hook strategy

`.pre-commit-config.yaml` in `assets/` runs ruff (lint + format) and prettier on staged files. Mypy and tsc are **not** in the pre-commit hook because they are slow enough to be friction during a sprint. Run them in CI instead, or manually before each act submission.

If you hit a pre-commit hook that fails on a legitimate edge case: either fix it (usually the right move) or add a specific ignore comment (`# noqa: E501` for a long URL you cannot break). Never `--no-verify` a whole commit.

## What to do when the skill's rule is wrong

Domain realities sometimes override conventions. Examples during this project:

- **Long signal dicts in tests.** A fixture brief with 20 fields is going to be long. Do not refactor it to be "cleaner" — it is fine as-is.
- **Tight coupling in the end-to-end smoke test.** The Act II demo-script may hit every subsystem in one flow. That is the point. Do not decompose it into unit tests; the integration test is what sells Act II.
- **Type hints on τ²-Bench integration code.** If the bench's API has dynamic shapes, aggressive typing adds friction without catching bugs. Use `Any` at the boundary and type your own code on the inside.

When you override a convention, leave a comment saying why — not "noqa: ignore this" but "# Fixture is long by design; brief has 20 fields across 5 signal categories."

## Never do this

- Never commit secrets. Even the challenge's test API keys go in `.env` and `.env` is in `.gitignore`.
- Never `print()` in production code paths. Logging only.
- Never catch `BaseException` or bare `except`. At worst, `Exception`.
- Never `--no-verify` pre-commit hooks on a commit you will push.
- Never ship a skill/module with a `TODO` that is actually a bug — fix it or write a test that captures the failing behavior.
- Never let the "code-quality" tail wag the "ship Act V on Saturday" dog. If a lint error and the memo deadline collide, the memo wins; fix the lint error after.
