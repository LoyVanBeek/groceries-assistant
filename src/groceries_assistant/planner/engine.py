"""Score-based weekly dinner planner (v2).

Selection is greedy per evening and fully explainable:
- hard filters: cooking time fits the evening (meal prep may be booked on an
  earlier evening), no 'never' verdict, not planned twice this week (unless
  nothing else fits), at most `dislike_slots` evenings with a disliked recipe;
- score: weighted verdict points, minus a repetition penalty when the recipe
  was cooked recently (history window configurable, default ~3 weeks), minus
  a penalty when the same main protein was planned the previous evening;
- optional leftover slot: an evening with `allow_leftovers` may repeat the
  previous evening's dish as 'restjes' (no extra shopping);
- deterministic tie-break: higher score, then fewer minutes, then slug.
Every pick carries a human-readable reason string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from groceries_assistant.models import Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import VERDICT_POINTS, Preferences

REPEAT_PENALTY = 6.0
DEFAULT_REPEAT_WINDOW_DAYS = 21
REUSE_PENALTY = 4.0
PROTEIN_REPEAT_PENALTY = 5.0
DEFAULT_DISLIKE_SLOTS = 1
LEFTOVER_MINUTES = 15
LEFTOVER_SCORE = 0.5


@dataclass(frozen=True)
class PlanEntry:
    date: date
    recipe: Recipe | None
    """None for a leftovers entry (previous evening's dish, eaten again)."""
    availability: EveningAvailability
    score: float
    reason: str
    kind: Literal["cook", "leftovers"] = "cook"
    minutes: int = 0
    """Minutes actually spent cooking this evening (prep-ahead excluded)."""
    prep_ahead_used: int = 0
    """Minutes booked as meal prep on an earlier evening."""
    source_slug: str | None = None
    """Recipe the leftovers come from (leftovers entries)."""
    people: int | None = None
    """Portions stored with the entry (joiners); None = use the default."""
    warnings: list[str] = field(default_factory=list)


@dataclass
class WeeklyPlan:
    entries: list[PlanEntry]

    @property
    def warnings(self) -> list[str]:
        return [w for entry in self.entries for w in entry.warnings]

    @property
    def recipe_dates(self) -> dict[str, date]:
        return {entry.recipe.slug: entry.date for entry in self.entries if entry.recipe}


def plan_week(
    recipes: list[Recipe],
    prefs: Preferences,
    evenings: list[EveningAvailability],
    history: dict[str, date] | None = None,
    dislike_slots: int = DEFAULT_DISLIKE_SLOTS,
    repeat_window_days: int = DEFAULT_REPEAT_WINDOW_DAYS,
) -> WeeklyPlan:
    """Pick a dish per evening, honouring time, verdicts and variety."""
    history = history or {}
    entries: list[PlanEntry] = []
    planned: set[str] = set()
    dislike_used = 0
    # Spare minutes on already-planned evenings, available for advance prep work.
    prep_capacity = 0

    for evening in sorted(evenings, key=lambda e: e.date):
        pick = _pick(
            recipes,
            prefs,
            evening,
            history,
            planned,
            dislike_used,
            dislike_slots,
            repeat_window_days,
            prep_capacity,
            _previous_recipe(entries),
        )
        if pick is None:
            pick = _pick(
                recipes,
                prefs,
                evening,
                history,
                planned,
                dislike_used,
                dislike_slots,
                repeat_window_days,
                prep_capacity,
                _previous_recipe(entries),
                allow_reuse=True,
            )
        if pick is None:
            # Nothing fits the time budget at all: plan the shortest recipe
            # anyway and warn loudly.
            shortest = min(recipes, key=lambda r: (r.time.same_day_minutes, r.slug))
            overflow = shortest.time.same_day_minutes - evening.free_minutes
            reason = f"te weinig tijd ({shortest.time.same_day_minutes}/{evening.free_minutes} min)"
            warnings = [
                f"{evening.date}: nothing fits {evening.free_minutes} min; "
                f"planned '{shortest.name}' anyway ({overflow:+d} min over budget)"
            ]
            entries.append(
                PlanEntry(
                    date=evening.date,
                    recipe=shortest,
                    availability=evening,
                    score=0.0,
                    minutes=shortest.time.same_day_minutes,
                    reason=reason,
                    warnings=warnings,
                )
            )
            planned.add(shortest.slug)
            prep_capacity += evening.free_minutes - shortest.time.same_day_minutes
            continue

        entries.append(pick.entry)
        if pick.recipe is not None:
            planned.add(pick.recipe.slug)
            if prefs.disliked_by(pick.recipe.slug, evening.joiners):
                dislike_used += 1
        # Spare minutes this evening offers (minus prep work booked here).
        prep_capacity += evening.free_minutes - pick.minutes - pick.prep_ahead_used

    return WeeklyPlan(entries=entries)


def _previous_recipe(entries: list[PlanEntry]) -> Recipe | None:
    for entry in reversed(entries):
        if entry.kind == "cook" and entry.recipe is not None:
            return entry.recipe
    return None


@dataclass
class _Pick:
    entry: PlanEntry
    recipe: Recipe | None
    minutes: int
    prep_ahead_used: int


def _pick(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    history: dict[str, date],
    planned: set[str],
    dislike_used: int,
    dislike_slots: int,
    repeat_window_days: int,
    prep_capacity: int,
    previous_recipe: Recipe | None,
    allow_reuse: bool = False,
) -> _Pick | None:
    candidates: list[tuple[float, Recipe, int, list[str], int]] = []
    for recipe in recipes:
        score = prefs.recipe_score(recipe.slug, evening.joiners)
        if recipe.slug in planned:
            if not allow_reuse:
                continue
            score -= REUSE_PENALTY
        if prefs.never_by(recipe.slug, evening.joiners):
            continue
        if prefs.disliked_by(recipe.slug, evening.joiners) and dislike_used >= dislike_slots:
            continue

        warnings: list[str] = []
        prep_ahead_used = 0
        if recipe.time.prep_ahead and recipe.time.prep_ahead <= prep_capacity:
            minutes = recipe.time.same_day_minutes
            prep_ahead_used = recipe.time.prep_ahead
            warnings.append(
                f"{recipe.time.prep_ahead} min voorbereiding moet eerder gebeuren"
            )
        else:
            minutes = recipe.time.total_minutes
        if minutes > evening.free_minutes:
            continue

        last_cooked = history.get(recipe.slug)
        if last_cooked is not None and 0 <= (evening.date - last_cooked).days <= repeat_window_days:
            score -= REPEAT_PENALTY
        if (
            recipe.protein
            and previous_recipe is not None
            and previous_recipe.protein
            and previous_recipe.protein == recipe.protein
        ):
            score -= PROTEIN_REPEAT_PENALTY
        candidates.append((score, recipe, minutes, warnings, prep_ahead_used))

    # Leftover option: reheat yesterday's dish.
    if (
        evening.allow_leftovers
        and previous_recipe is not None
        and evening.free_minutes >= LEFTOVER_MINUTES
    ):
        leftover_score = LEFTOVER_SCORE
        if not candidates or max(c[0] for c in candidates) < leftover_score:
            entry = PlanEntry(
                date=evening.date,
                recipe=None,
                availability=evening,
                score=leftover_score,
                minutes=LEFTOVER_MINUTES,
                kind="leftovers",
                source_slug=previous_recipe.slug,
                reason=f"restjes van '{previous_recipe.name}'",
            )
            return _Pick(entry=entry, recipe=None, minutes=LEFTOVER_MINUTES, prep_ahead_used=0)

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[2], item[1].slug))
    score, recipe, minutes, warnings, prep_ahead_used = candidates[0]
    entry = PlanEntry(
        date=evening.date,
        recipe=recipe,
        availability=evening,
        score=score,
        minutes=minutes,
        prep_ahead_used=prep_ahead_used,
        reason=_reason(recipe, prefs, evening, score),
        warnings=warnings,
    )
    return _Pick(entry=entry, recipe=recipe, minutes=minutes, prep_ahead_used=prep_ahead_used)


def _reason(recipe: Recipe, prefs: Preferences, evening: EveningAvailability, score: float) -> str:
    joiners = set(evening.joiners) if evening.joiners is not None else None
    parts: list[str] = []
    for person in prefs.people:
        if joiners is not None and person.id not in joiners:
            continue
        verdict = prefs.verdict_for(recipe.slug, person.id)
        if verdict is None:
            continue
        points = VERDICT_POINTS[verdict]
        group_weight = prefs.group_weights.get(person.role.value, 1.0)
        weighted = group_weight * person.weight * points
        parts.append(f"{person.display_name}: {verdict} ({weighted:+.0f})")
    parts.append(f"{recipe.time.total_minutes}/{evening.free_minutes} min")
    parts.append(f"totaal {score:+.0f}")
    return "; ".join(parts)
