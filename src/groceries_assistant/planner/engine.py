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
- composed dishes compete with plain recipes: every part is filled with the
  best option that evening allows, the dish scores as the mean of its parts plus
  DISH_BONUS (unless the dish slug itself carries a verdict), a dish is offered
  once a week and never repeated, and a dish that does not fit the evening is
  simply not offered;
- deterministic tie-break: higher score, then fewer minutes, then slug.
Every pick carries a human-readable reason string.
"""

from __future__ import annotations

from collections.abc import Sequence
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
DISH_BONUS = 2.0
"""What a defined dish is worth above the average of its own parts.

The mean of a set can never beat its best element, and every part is also a
recipe the planner could serve on its own, so without this a dish nobody has
judged could never win an evening. A small bonus makes it a candidate: it
outranks a plain recipe nobody cares about, and still loses to one person
loving a component. Small enough that it does not outrank a real preference.
"""


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
    dishes: Sequence[ComposedDish] = (),
) -> WeeklyPlan:
    """Pick a dish per evening, honouring time, verdicts and variety.

    Candidates are plain recipes and composed dishes side by side: a dish is
    filled with one option per part, each scored by the same rules, and stands
    or falls as a whole. `dishes` is a plain sequence so callers that only know
    recipes need not change.
    """
    history = history or {}
    recipes_by_slug = {recipe.slug: recipe for recipe in recipes}
    entries: list[PlanEntry] = []
    planned: set[str] = set()
    dislike_used = 0
    # Spare minutes on already-planned evenings, available for advance prep work.
    prep_capacity = 0

    for evening in sorted(evenings, key=lambda e: e.date):
        pick = _pick(
            recipes,
            recipes_by_slug,
            dishes,
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
                recipes_by_slug,
                dishes,
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
        planned.update(pick.planned_slugs)
        if pick.uses_dislike_slot:
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
    minutes: int
    prep_ahead_used: int
    planned_slugs: list[str] = field(default_factory=list)
    """Slugs this evening takes out of the week's pool: a recipe, or a dish and its parts."""
    uses_dislike_slot: bool = False
    """Whether one of those was already on the dislike list."""


@dataclass(frozen=True)
class Option:
    """One candidate scored for one evening: score, time and any warnings."""

    recipe: Recipe
    score: float
    minutes: int
    warnings: list[str]
    prep_ahead_used: int
    penalty: float = 0.0
    """What the plan took off this option: reuse, repetition, same protein."""


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
    points = prefs.recipe_score(recipe.slug, evening.joiners)
    penalty = 0.0
    if recipe.slug in planned:
        if not allow_reuse:
            return None
        penalty += REUSE_PENALTY
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
        penalty += REPEAT_PENALTY
    if (
        recipe.protein
        and previous_recipe is not None
        and previous_recipe.protein
        and previous_recipe.protein == recipe.protein
    ):
        penalty += PROTEIN_REPEAT_PENALTY
    return Option(
        recipe=recipe,
        score=points - penalty,
        minutes=minutes,
        warnings=warnings,
        prep_ahead_used=prep_ahead_used,
        penalty=penalty,
    )


@dataclass(frozen=True)
class _Candidate:
    """One evening's way to eat: a plain recipe, or a dish with its parts filled."""

    slug: str
    score: float
    minutes: int
    prep_ahead_used: int
    entry: PlanEntry
    planned_slugs: tuple[str, ...]
    uses_dislike_slot: bool


def _pick(
    recipes: list[Recipe],
    recipes_by_slug: dict[str, Recipe],
    dishes: Sequence[ComposedDish],
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
    """Score every recipe and every dish, and take the best that fits tonight."""
    candidates: list[_Candidate] = []

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
        if option is None:
            continue
        candidates.append(
            _Candidate(
                slug=recipe.slug,
                score=option.score,
                minutes=option.minutes,
                prep_ahead_used=option.prep_ahead_used,
                entry=PlanEntry(
                    date=evening.date,
                    recipe=recipe,
                    availability=evening,
                    score=option.score,
                    minutes=option.minutes,
                    prep_ahead_used=option.prep_ahead_used,
                    reason=_reason(recipe, prefs, evening, option.score),
                    warnings=option.warnings,
                ),
                planned_slugs=(recipe.slug,),
                uses_dislike_slot=bool(prefs.disliked_by(recipe.slug, evening.joiners)),
            )
        )

    for dish in dishes:
        choice = fill_dish(
            dish,
            recipes_by_slug,
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
        if choice is None:
            continue
        candidates.append(
            _Candidate(
                slug=dish.slug,
                score=choice.score,
                minutes=choice.minutes,
                prep_ahead_used=choice.prep_ahead_used,
                entry=PlanEntry(
                    date=evening.date,
                    recipe=None,
                    availability=evening,
                    score=choice.score,
                    minutes=choice.minutes,
                    prep_ahead_used=choice.prep_ahead_used,
                    reason=choice.reason,
                    warnings=choice.warnings,
                    dish_slug=dish.slug,
                    parts=list(choice.parts),
                ),
                # The dish and every part it uses are off the table for the rest
                # of the week: one component, one place in the plan.
                planned_slugs=(dish.slug, *(part.slug for part in choice.parts)),
                uses_dislike_slot=any(
                    prefs.disliked_by(part.slug, evening.joiners) for part in choice.parts
                ),
            )
        )

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
            return _Pick(entry=entry, minutes=LEFTOVER_MINUTES, prep_ahead_used=0)

    if not candidates:
        return None
    best = min(candidates, key=lambda c: (-c.score, c.minutes, c.slug))
    return _Pick(
        entry=best.entry,
        minutes=best.minutes,
        prep_ahead_used=best.prep_ahead_used,
        planned_slugs=list(best.planned_slugs),
        uses_dislike_slot=best.uses_dislike_slot,
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
    passed over. Returns None when the whole dish is off the table; use
    `why_not` to learn which of those it was.

    A dish is offered once a week and never repeated, not even when the week is
    out of fresh ideas. Its parts may not be reused either, so repeating the
    combination would repeat three recipes at once; the escape hatch then falls
    to a single plain recipe, which is the smaller thing to cook twice.

    A verdict on the dish's own slug says more about the combination than its
    parts do, so it replaces their mean score. Without one, the dish is worth the
    average of its parts plus DISH_BONUS: the household wrote this dish down, so
    it is a candidate of its own, not just the sum of its components.
    """
    return _fill_dish(
        dish,
        recipes_by_slug,
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
    )[0]


def why_not(
    dish: ComposedDish,
    recipes_by_slug: dict[str, Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
) -> str | None:
    """Why this dish is not on offer tonight, in words; None when it is.

    For the week view's picker: a dish it cannot show is worse than a dish it
    shows as unavailable with a reason, because a typo in a part option used to
    make the dish disappear with nothing to show for it.
    """
    return _fill_dish(dish, recipes_by_slug, prefs, evening)[1] or None


def _fill_dish(
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
) -> tuple[DishChoice | None, str]:
    """The fill, plus the reason there is none. See `fill_dish`."""
    if prefs.never_by(dish.slug, evening.joiners):
        return None, "nooit volgens een van de aanwezigen"
    if dish.slug in planned:
        return None, "staat al in deze week"

    used = set(planned)
    chosen: list[tuple[DishPart, Option]] = []
    for part in dish.parts:
        options: list[Option] = []
        unknown: list[str] = []
        for slug in part.options:
            recipe = recipes_by_slug.get(slug)
            if recipe is None:
                unknown.append(slug)  # deleted or renamed since the dish was written
                continue
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
            return None, _part_problem(part, recipes_by_slug, prefs, evening, unknown)
        best = min(options, key=lambda option: (-option.score, option.minutes, option.recipe.slug))
        used.add(best.recipe.slug)
        chosen.append((part, best))

    minutes = max(option.minutes for _part, option in chosen)
    longest = max(chosen, key=lambda pair: pair[1].minutes)
    if minutes > evening.free_minutes:
        return None, (
            f"{longest[0].label} {longest[1].recipe.name} is {minutes} min, "
            f"deze avond heeft {evening.free_minutes}"
        )

    prep_ahead_used = sum(option.prep_ahead_used for _part, option in chosen)
    warnings = [w for _part, option in chosen for w in option.warnings]
    if prep_ahead_used > prep_capacity:
        warnings.append(
            f"{prep_ahead_used} min voorbereiding, maar er is {prep_capacity} min eerder "
            "over; dat past niet"
        )
    mean = sum(option.score for _part, option in chosen) / len(chosen)
    if dish.slug in prefs.verdicts:
        # The verdict says what the combination is worth. What the plan took off
        # its parts -- reused, cooked recently, same protein -- still counts, or
        # a dish with a verdict would be planned over and over for free.
        score = prefs.recipe_score(dish.slug, evening.joiners) - sum(
            option.penalty for _part, option in chosen
        )
    else:
        score = mean
    score += DISH_BONUS
    return (
        DishChoice(
            dish=dish,
            parts=tuple(option.recipe for _part, option in chosen),
            score=score,
            minutes=minutes,
            total_minutes=sum(option.recipe.time.total_minutes for _part, option in chosen),
            prep_ahead_used=prep_ahead_used,
            warnings=warnings,
            reason=_dish_reason(dish, chosen, evening, minutes, score),
        ),
        "",
    )


def _part_problem(
    part: DishPart,
    recipes_by_slug: dict[str, Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    unknown: list[str],
) -> str:
    """Why a part could not be filled, in the words of the part.

    `score_option` drops an option for several reasons at once (too long, never,
    cooked recently, already planned), so this only names a cause when it holds
    for every option that does exist -- a true statement, not a guess at the
    single reason the planner used.
    """
    if unknown:
        gone = "bestaat niet meer" if len(unknown) == 1 else "bestaan niet meer"
        return f"{part.label}: {', '.join(unknown)} {gone}"
    existing = [recipes_by_slug[slug] for slug in part.options if slug in recipes_by_slug]
    if not existing:
        return f"{part.label}: geen opties"
    if all(recipe.time.same_day_minutes > evening.free_minutes for recipe in existing):
        longest = max(existing, key=lambda recipe: recipe.time.same_day_minutes)
        return (
            f"{part.label}: {longest.name} is {longest.time.same_day_minutes} min, "
            f"deze avond heeft {evening.free_minutes}"
        )
    if all(prefs.never_by(recipe.slug, evening.joiners) for recipe in existing):
        return f"{part.label}: niemand wil dit"
    return f"{part.label}: geen optie beschikbaar"


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
