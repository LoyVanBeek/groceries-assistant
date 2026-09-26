"""Family preferences: per-person verdicts with group/person weights."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Verdict = Literal["love", "ok", "dislike", "never"]
VERDICT_POINTS: dict[str, float] = {"love": 3.0, "ok": 1.0, "dislike": -4.0}


class Role(StrEnum):
    KID = "kid"
    ADULT = "adult"


class Person(BaseModel):
    id: str
    name: str = ""
    role: Role
    weight: float = 1.0

    @property
    def display_name(self) -> str:
        return self.name or self.id


class Preferences(BaseModel):
    """Verdicts are keyed by recipe slug, then person id.

    Scoring: group_weight(role) * person.weight * verdict_points.
    'never' is not scored: it hard-excludes the recipe for everyone.
    """

    people: list[Person] = Field(default_factory=list)
    group_weights: dict[str, float] = Field(default_factory=lambda: {"kid": 2.0, "adult": 1.0})
    verdicts: dict[str, dict[str, Verdict]] = Field(default_factory=dict)

    def verdict_for(self, recipe_slug: str, person_id: str) -> Verdict | None:
        return self.verdicts.get(recipe_slug, {}).get(person_id)

    def _verdicts_by(
        self, recipe_slug: str, verdict: str, joiners: Sequence[str] | None
    ) -> list[str]:
        return [
            person_id
            for person_id, v in self.verdicts.get(recipe_slug, {}).items()
            if v == verdict and (joiners is None or person_id in set(joiners))
        ]

    def never_by(
        self, recipe_slug: str, joiners: Sequence[str] | None = None
    ) -> list[str]:
        return self._verdicts_by(recipe_slug, "never", joiners)

    def disliked_by(
        self, recipe_slug: str, joiners: Sequence[str] | None = None
    ) -> list[str]:
        return self._verdicts_by(recipe_slug, "dislike", joiners)

    def recipe_score(
        self, recipe_slug: str, joiners: Sequence[str] | None = None
    ) -> float:
        """Total verdict points; only `joiners` count when given.

        Absent people (not in joiners) don't steer the meal they won't eat.
        """
        counted = set(joiners) if joiners is not None else None
        total = 0.0
        for person in self.people:
            if counted is not None and person.id not in counted:
                continue
            verdict = self.verdict_for(recipe_slug, person.id)
            if verdict is None or verdict == "never":
                continue  # 'never' is a hard filter, not a score
            group_weight = self.group_weights.get(person.role.value, 1.0)
            total += group_weight * person.weight * VERDICT_POINTS[verdict]
        return total


def load_preferences(path: Path) -> Preferences:
    """Load preferences.yaml; a missing file means neutral preferences."""
    path = Path(path)
    if not path.is_file():
        return Preferences()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Preferences.model_validate(data)
