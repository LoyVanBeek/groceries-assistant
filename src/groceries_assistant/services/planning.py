"""Planning services for the v2 web UI."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.planner.engine import fill_dish, why_not
from groceries_assistant.preferences import Preferences


@dataclass(frozen=True)
class Alternative:
    """One thing you could plan on an evening: a composed dish or a recipe."""

    slug: str
    name: str
    score: float
    is_dish: bool = False
    part_labels: tuple[str, ...] = ()
    """The part labels of a dish ('Groente', 'Vlees'); empty for a recipe."""
    minutes: int | None = None
    """How long the option takes this evening; None when it cannot be planned."""
    problem: str = ""
    """Why this cannot be planned tonight; empty when it can.

    Only dishes get one: a dish that is off the table is still listed, greyed
    out, so a dish with a typo in it is visible instead of simply gone.
    """


def alternatives(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    exclude_slug: str | None = None,
    limit: int | None = None,
    dishes: Sequence[ComposedDish] = (),
    *,
    by_slug: dict[str, Recipe] | None = None,
) -> list[Alternative]:
    """Dishes and recipes that fit the evening's time budget, best first.

    Composed dishes come first, then plain recipes, each group sorted by
    preference. Honours the same constraints as the planner engine: 'never'
    excludes, prep-ahead halves the effective time, and the previous evening's
    protein is penalised.

    Pass `by_slug` (all recipes, including components) when the dish options
    are components rather than dinner recipes; otherwise it is built from the
    `recipes` argument alone, which may miss component slugs.
    """
    if by_slug is None:
        by_slug = {r.slug: r for r in recipes}
    dish_rows: list[Alternative] = []
    for dish in dishes:
        if dish.slug == exclude_slug:
            continue
        choice = fill_dish(dish, by_slug, prefs, evening)
        problem = "" if choice is not None else why_not(dish, by_slug, prefs, evening) or ""
        dish_rows.append(
            Alternative(
                slug=dish.slug,
                name=dish.name,
                score=choice.score if choice is not None else 0.0,
                minutes=choice.minutes if choice is not None else None,
                is_dish=True,
                part_labels=tuple(part.label for part in dish.parts),
                problem=problem,
            )
        )
    dish_rows.sort(key=lambda row: (-row.score, row.slug))

    scored: list[Alternative] = []
    for recipe in recipes:
        if recipe.slug == exclude_slug or prefs.never_by(recipe.slug):
            continue
        minutes = (
            recipe.time.same_day_minutes if recipe.time.prep_ahead else recipe.time.total_minutes
        )
        # Over budget is a problem to show, not a filter: the picker lists
        # everything, blocked rows greyed out with the reason at the bottom.
        problem = (
            f"{recipe.name} is {minutes} min, deze avond heeft {evening.free_minutes}"
            if minutes > evening.free_minutes
            else ""
        )
        scored.append(
            Alternative(
                slug=recipe.slug,
                name=recipe.name,
                score=prefs.recipe_score(recipe.slug),
                minutes=minutes,
                problem=problem,
            )
        )
    scored.sort(key=lambda row: (-row.score, row.slug))
    # Dishes lead, because they are the meals the household put together on
    # purpose. Rows that cannot be planned tonight go to the very bottom,
    # greyed out with the reason: visible, but never in the way of a real choice.
    # No cap by default: the picker is a dropdown list and a recipe without
    # verdicts (a new one) must stay reachable, not be hidden by a limit.
    n_dishes = len([row for row in dish_rows if not row.problem])
    cap = max((limit if limit is not None else len(scored)) - n_dishes, 0)
    return (
        [row for row in dish_rows if not row.problem]
        + [row for row in scored if not row.problem][:cap]
        + [row for row in dish_rows if row.problem]
        + [row for row in scored if row.problem]
    )
