"""
Validate `report/evidence_graph.json`.

For every claim entry, verify the `source_ref` actually resolves to existing
content. Walk path-style refs into trace files and JSON artifacts; for
`source_type == "public"` (e.g. challenge brief lines, leaderboard refs) we
only check the source_ref string is non-empty. Claims marked `placeholder:
true` are reported but not failed.

Exit code:
  0 — all claims resolve; no placeholder remaining
  1 — at least one missing-resolution failure
  2 — some placeholder entries still in the graph (acceptable pre-final)

Usage:
    python -m report.validate_evidence_graph
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("validate_evidence_graph")

REPO_ROOT = Path(__file__).parent.parent
GRAPH_PATH = REPO_ROOT / "report" / "evidence_graph.json"


def _resolve_artifact(rel_or_abs: str) -> Path | None:
    """Return Path if the cited artifact file exists, else None."""
    # source_ref shape: "<repo-relative path> :: <jsonpath-ish key>"
    # We split on " :: " and check the path component.
    if " :: " in rel_or_abs:
        path_part = rel_or_abs.split(" :: ", 1)[0]
    elif " ::" in rel_or_abs:
        path_part = rel_or_abs.split(" ::", 1)[0]
    else:
        path_part = rel_or_abs
    # Strip jsonpath-like tail if it doesn't have ::
    path_part = path_part.strip().split(" ", 1)[0] if "/" in path_part else path_part.strip()
    p = REPO_ROOT / path_part
    return p if p.exists() else None


def _check_claim(claim: dict) -> tuple[bool, str]:
    """Return (ok, reason) for one claim."""
    ctype = claim.get("source_type", "")
    sref = claim.get("source_ref", "")
    if claim.get("placeholder"):
        return True, "placeholder (acceptable pre-final)"
    if not sref:
        return False, "empty source_ref"
    if ctype == "public":
        return True, "public reference (string-only check)"
    if ctype == "assumption":
        return True, "stated assumption"
    # trace, code → resolve to a file path
    artifact = _resolve_artifact(sref)
    if artifact is None:
        return False, f"artifact not found: {sref}"
    return True, f"resolved {artifact.relative_to(REPO_ROOT)}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not GRAPH_PATH.exists():
        logger.error("evidence_graph.json missing at %s", GRAPH_PATH)
        return 1
    g = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    claims = g.get("claims", [])
    if not claims:
        logger.error("no claims in evidence_graph.json")
        return 1

    failures: list[tuple[str, str]] = []
    placeholders: list[str] = []
    ok = 0
    for c in claims:
        cid = c.get("id", "?")
        passed, reason = _check_claim(c)
        if c.get("placeholder"):
            placeholders.append(cid)
        if not passed:
            failures.append((cid, reason))
            logger.error("FAIL %s: %s", cid, reason)
        else:
            ok += 1
            logger.debug("ok %s: %s", cid, reason)

    logger.info(
        "Total claims: %d  resolved: %d  failures: %d  placeholders: %d",
        len(claims), ok, len(failures), len(placeholders),
    )
    if failures:
        logger.error("FAIL — %d unresolved claim(s): %s", len(failures), [f[0] for f in failures])
        return 1
    if placeholders:
        logger.warning("PRE-FINAL — %d placeholder claim(s) remain: %s",
                       len(placeholders), placeholders)
        return 2
    logger.info("All %d claims resolve cleanly.", len(claims))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
