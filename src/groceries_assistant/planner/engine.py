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

from groceries_assistant.models import ComposedDish, DishPart, Recipe
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
    dish_slug: str | None = None
    """Composed dish planned for this evening; `recipe` is then unset."""
    parts: list[Recipe] = field(default_factory=list)
    """Recipes filling the dish's parts, in the order the dish declares them."""
    missing: list[str] = field(default_factory=list)
    """Labels of dish parts that have no pick yet."""

    @property
    def dishes(self) -> list[Recipe]:
        """Every recipe to cook this evening: the dish parts, or the recipe."""
        if self.parts:
            return list(self.parts)
        return [self.recipe] if self.recipe is not None else []

    @property
    def combined_minutes(self) -> int:
        """Minutes on the evening itself: the longest part (they run in parallel)."""
        if not self.parts:
            return self.minutes
        return max(part.time.same_day_minutes for part in self.parts)

    @property
    def total_minutes(self) -> int:
        """Sum over the parts -- what you are actually busy with."""
        if not self.parts:
            return self.minutes
        return sum(part.time.total_minutes for part in self.parts)


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


@dataclass(frozen=True)
class Option:
    """One candidate scored for one evening: score, time and any warnings."""

    recipe: Recipe
    score: float
    minutes: int
    warnings: list[str]
    prep_ahead_used: int


def score_option(
    recipe: Recipe,
    prefs: Preferences,
    evening: EveningAvailability,
    *,
    history: dict[str, date] | None = None,
    planned: set[str] | frozenset[str] = frozenset(),
    dislike_used: int = 0,
    dislike_slots: int = DEFAULT_DISLIKE_SLOTS,
    repeat_window_days: int = DEFAULT_REPEAT_WINDOW_DAYS,
    prep_capacity: int = 0,
    previous_recipe: Recipe | None = None,
    allow_reuse: bool = False,
) -> Option | None:
    """Score one dish for one evening; None when it cannot be served then.

    This is the single scoring rule of the planner: verdicts, repetition,
    protein, prep-ahead and the time budget. A plain recipe is one option; each
    part of a composed dish is one too, scored the same way.
    """
    history = history or {}
    score = prefs.recipe_score(recipe.slug, evening.joiners)
    if recipe.slug in planned:
        if not allow_reuse:
            return None
        score -= REUSE_PENALTY
    if prefs.never_by(recipe.slug, evening.joiners):
        return None
    if prefs.disliked_by(recipe.slug, evening.joiners) and dislike_used >= dislike_slots:
        return None

    warnings: list[str] = []
    prep_ahead_used = 0
    if recipe.time.prep_ahead and recipe.time.prep_ahead <= prep_capacity:
        minutes = recipe.time.same_day_minutes
        prep_ahead_used = recipe.time.prep_ahead
        warnings.append(f"{recipe.time.prep_ahead} min voorbereiding moet eerder gebeuren")
    else:
        minutes = recipe.time.total_minutes
    if minutes > evening.free_minutes:
        return None

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
    return Option(
        recipe=recipe,
        score=score,
        minutes=minutes,
        warnings=warnings,
        prep_ahead_used=prep_ahead_used,
    )


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
    candidates: list[Option] = []
    for recipe in recipes:
        option = score_option(
            recipe,
            prefs,
            evening,
            history=history,
            planned=planned,
            dislike_used=dislike_used,
            dislike_slots=dislike_slots,
            repeat_window_days=repeat_window_days,
            prep_capacity=prep_capacity,
            previous_recipe=previous_recipe,
            allow_reuse=allow_reuse,
        )
        if option is not None:
            candidates.append(option)

    # Leftover option: reheat yesterday's dish.
    if (
        evening.allow_leftovers
        and previous_recipe is not None
        and evening.free_minutes >= LEFTOVER_MINUTES
    ):
        leftover_score = LEFTOVER_SCORE
        if not candidates or max(c.score for c in candidates) < leftover_score:
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
    candidates.sort(key=lambda option: (-option.score, option.minutes, option.recipe.slug))
    best = candidates[0]
    entry = PlanEntry(
        date=evening.date,
        recipe=best.recipe,
        availability=evening,
        score=best.score,
        minutes=best.minutes,
        prep_ahead_used=best.prep_ahead_used,
        reason=_reason(best.recipe, prefs, evening, best.score),
        warnings=best.warnings,
    )
    return _Pick(
        entry=entry,
        recipe=best.recipe,
        minutes=best.minutes,
        prep_ahead_used=best.prep_ahead_used,
    )


@dataclass(frozen=True)
class DishChoice:
    """A composed dish with a recipe chosen for each of its parts."""

    dish: ComposedDish
    parts: tuple[Recipe, ...]
    """Chosen recipes, in the order the dish declares its parts."""
    score: float
    minutes: int
    """Minutes on the evening itself: the longest part."""
    total_minutes: int
    """Sum over the parts."""
    prep_ahead_used: int
    warnings: list[str]
    reason: str


def fill_dish(
    dish: ComposedDish,
    recipes_by_slug: dict[str, Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    *,
    history: dict[str, date] | None = None,
    planned: set[str] | frozenset[str] = frozenset(),
    dislike_used: int = 0,
    dislike_slots: int = DEFAULT_DISLIKE_SLOTS,
    repeat_window_days: int = DEFAULT_REPEAT_WINDOW_DAYS,
    prep_capacity: int = 0,
    previous_recipe: Recipe | None = None,
    allow_reuse: bool = False,
) -> DishChoice | None:
    """Fill every part of a dish with the best option that evening allows.

    Each part's options are scored with the planner's own rules, so a component
    that is already planned this week, marked 'never' or cooked recently is
    passed over. Returns None when the whole dish is off the table: a 'never'
    verdict on the dish itself, a part with no usable option left, or a longest
    part that does not fit the evening.

    A verdict on the dish's own slug says more about the combination than its
    parts do, so it replaces their mean score.
    """
    if prefs.never_by(dish.slug, evening.joiners):
        return None

    used = set(planned)
    chosen: list[tuple[DishPart, Option]] = []
    for part in dish.parts:
        options: list[Option] = []
        for slug in part.options:
            recipe = recipes_by_slug.get(slug)
            if recipe is None:
                continue  # recipe deleted since the dish was defined
            option = score_option(
                recipe,
                prefs,
                evening,
                history=history,
                planned=used,
                dislike_used=dislike_used,
                dislike_slots=dislike_slots,
                repeat_window_days=repeat_window_days,
                prep_capacity=prep_capacity,
                previous_recipe=previous_recipe,
                allow_reuse=allow_reuse,
            )
            if option is not None:
                options.append(option)
        if not options:
            return None  # this part cannot be filled tonight
        best = min(options, key=lambda option: (-option.score, option.minutes, option.recipe.slug))
        used.add(best.recipe.slug)
        chosen.append((part, best))

    minutes = max(option.minutes for _part, option in chosen)
    if minutes > evening.free_minutes:
        return None  # the longest part does not fit

    prep_ahead_used = sum(option.prep_ahead_used for _part, option in chosen)
    warnings = [w for _part, option in chosen for w in option.warnings]
    if prep_ahead_used > prep_capacity:
        warnings.append(
            f"{prep_ahead_used} min voorbereiding, maar er is {prep_capacity} min eerder "
            "over; dat past niet"
        )
    mean = sum(option.score for _part, option in chosen) / len(chosen)
    score = prefs.recipe_score(dish.slug, evening.joiners) if dish.slug in prefs.verdicts else mean
    return DishChoice(
        dish=dish,
        parts=tuple(option.recipe for _part, option in chosen),
        score=score,
        minutes=minutes,
        total_minutes=sum(option.recipe.time.total_minutes for _part, option in chosen),
        prep_ahead_used=prep_ahead_used,
        warnings=warnings,
        reason=_dish_reason(dish, chosen, evening, minutes, score),
    )


def _dish_reason(
    dish: ComposedDish,
    chosen: list[tuple[DishPart, Option]],
    evening: EveningAvailability,
    minutes: int,
    score: float,
) -> str:
    """A reason string that names the combination, not just the number."""
    parts = [f"{part.label}: {option.recipe.name}" for part, option in chosen]
    parts.append(f"{minutes}/{evening.free_minutes} min")
    parts.append(f"totaal {score:+.0f}")
    return "; ".join(parts)


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
