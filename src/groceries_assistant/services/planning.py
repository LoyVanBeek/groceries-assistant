"""Planning services for the v2 web UI."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.planner.engine import fill_dish
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


def alternatives(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    exclude_slug: str | None = None,
    limit: int = 8,
    dishes: Sequence[ComposedDish] = (),
) -> list[Alternative]:
    """Dishes and recipes that fit the evening's time budget, best first.

    Composed dishes come first, then plain recipes, each group sorted by
    preference. Honours the same constraints as the planner engine: 'never'
    excludes, prep-ahead halves the effective time, and the previous evening's
    protein is penalised.
    """
    by_slug = {r.slug: r for r in recipes}
    dish_rows: list[Alternative] = []
    for dish in dishes:
        if dish.slug == exclude_slug:
            continue
        choice = fill_dish(dish, by_slug, prefs, evening)
        if choice is None:
            continue
        dish_rows.append(
            Alternative(
                slug=dish.slug,
                name=dish.name,
                score=choice.score,
                is_dish=True,
                part_labels=tuple(part.label for part in dish.parts),
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
        if minutes > evening.free_minutes:
            continue
        scored.append(
            Alternative(slug=recipe.slug, name=recipe.name, score=prefs.recipe_score(recipe.slug))
        )
    scored.sort(key=lambda row: (-row.score, row.slug))
    return dish_rows + scored[:limit]
