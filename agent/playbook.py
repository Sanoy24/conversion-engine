"""
Playbook — everything that makes the engine speak for one specific company.

The engine itself is company-agnostic. A playbook is a directory containing a
`playbook.yaml` plus any knowledge files it references (style guide, pricing,
case studies, email templates, capacity). Point `PLAYBOOK_DIR` at a directory to
switch companies; see `playbooks/example/` for a complete, annotated example.

The playbook controls:
  - company identity and email signature
  - voice: style guide, banned phrases, subject prefixes, word limits
  - ICP segments, expressed as declarative rules over enrichment signals
  - delivery capacity (what the agent may and may not commit to)
  - extra knowledge files injected into the drafter prompt
"""

from __future__ import annotations

import contextlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from agent.config import settings

logger = logging.getLogger(__name__)

ABSTAIN = "abstain"


# ── Company & voice ───────────────────────────────────────────────────


class CompanyProfile(BaseModel):
    name: str
    website: str | None = None
    offering: str = Field(
        description="One or two sentences on what the company sells. Shown to the LLM."
    )
    sender_name: str = "The team"
    sender_title: str | None = None
    support_email: str | None = None

    @property
    def signature(self) -> str:
        parts = [self.sender_name]
        if self.sender_title:
            parts.append(self.sender_title)
        parts.append(self.name)
        if self.website:
            parts.append(self.website)
        return "\n".join(parts)


class WordLimits(BaseModel):
    cold: int = 120
    warm_reply: int = 100
    re_engagement: int = 70


class VoiceConfig(BaseModel):
    style_guide: str | None = Field(
        default=None, description="Path (relative to the playbook dir) to a markdown style guide."
    )
    banned_phrases: list[str] = Field(default_factory=list)
    subject_prefixes: list[str] = Field(default_factory=list)
    max_words: WordLimits = Field(default_factory=WordLimits)
    extra_rules: list[str] = Field(default_factory=list)
    tone_check_threshold: float = 0.7


class KnowledgeFile(BaseModel):
    """A file or directory of markdown injected into the drafter's system prompt."""

    path: str
    label: str
    use_for: list[str] = Field(
        default_factory=lambda: ["cold", "warm_reply", "re_engagement"],
        description="Email types this knowledge is included for.",
    )
    max_chars: int = 2000


# ── Segments ──────────────────────────────────────────────────────────


class Condition(BaseModel):
    """
    A single test against the enrichment signals. Set exactly one field.

    funding_rounds:    latest funding round is one of these (case-insensitive)
    employees:         {min, max} inclusive headcount bounds (either may be omitted)
    layoffs:           a layoff event was / was not detected
    leadership_change: a new technical leader was / was not detected
    ai_maturity_min:   AI maturity score (0-3) is at least this
    ai_roles_min:      at least this many AI-adjacent open roles
    open_roles_min:    at least this many open engineering roles
    """

    funding_rounds: list[str] | None = None
    employees: dict[str, int] | None = None
    layoffs: bool | None = None
    leadership_change: bool | None = None
    ai_maturity_min: int | None = None
    ai_roles_min: int | None = None
    open_roles_min: int | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        set_fields = [name for name in type(self).model_fields if getattr(self, name) is not None]
        if len(set_fields) != 1:
            raise ValueError(f"A condition must set exactly one field, got: {set_fields or 'none'}")
        return self

    @property
    def kind(self) -> str:
        return next(name for name in type(self).model_fields if getattr(self, name) is not None)


class Criterion(BaseModel):
    """A weighted condition. `required` criteria gate the segment entirely."""

    when: Condition
    weight: int = 1
    required: bool = False


class Segment(BaseModel):
    id: str
    label: str
    description: str = ""
    pitch: str = Field(default="", description="Guidance for the drafter when pitching this segment.")
    criteria: list[Criterion]
    exclude: list[Condition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_id(self) -> Segment:
        if self.id == ABSTAIN:
            raise ValueError(f"'{ABSTAIN}' is reserved and cannot be used as a segment id")
        return self


# ── Playbook ──────────────────────────────────────────────────────────


class Playbook(BaseModel):
    company: CompanyProfile
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    segments: list[Segment] = Field(
        description="Ordered by priority: on a score tie, the earlier segment wins."
    )
    capacity: str | None = Field(
        default=None, description="Path to a capacity JSON file (see example playbook)."
    )
    knowledge: list[KnowledgeFile] = Field(default_factory=list)

    root: Path = Field(default_factory=Path, exclude=True)

    @model_validator(mode="after")
    def _unique_segment_ids(self) -> Playbook:
        ids = [s.id for s in self.segments]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate segment ids: {sorted(dupes)}")
        return self

    def segment(self, segment_id: str) -> Segment | None:
        return next((s for s in self.segments if s.id == segment_id), None)

    def read_text(self, relative: str | None, max_chars: int | None = None) -> str:
        """Read a playbook file or directory of .md files. Returns '' if missing."""
        if not relative:
            return ""
        path = self.root / relative
        if path.is_dir():
            parts = [
                f"## {p.stem}\n\n{p.read_text(encoding='utf-8')}" for p in sorted(path.glob("*.md"))
            ]
            text = "\n\n---\n\n".join(parts)
        elif path.exists():
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                with contextlib.suppress(json.JSONDecodeError):
                    text = json.dumps(json.loads(text), indent=2)
        else:
            logger.warning("Playbook file not found: %s", path)
            return ""
        return text[:max_chars] if max_chars else text

    def load_capacity(self) -> dict[str, Any] | None:
        """Parsed capacity file, or None if the playbook does not declare one."""
        if not self.capacity:
            return None
        path = self.root / self.capacity
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load capacity file %s: %s", path, e)
            return None


def load_playbook(directory: str | Path) -> Playbook:
    root = Path(directory)
    config_path = root / "playbook.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"No playbook.yaml in {root.resolve()}. Set PLAYBOOK_DIR to a playbook directory "
            "(see playbooks/example/)."
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    playbook = Playbook.model_validate(raw)
    playbook.root = root
    return playbook


@lru_cache(maxsize=1)
def get_playbook() -> Playbook:
    """The active playbook, loaded once from `settings.playbook_dir`."""
    return load_playbook(settings.playbook_dir)
