"""Planning services for the v2 web UI."""

from __future__ import annotations

from groceries_assistant.models import Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import Preferences


def alternatives(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    exclude_slug: str | None = None,
    limit: int = 8,
) -> list[tuple[float, Recipe]]:
    """Recipes that fit the evening's time budget, best preference first.

    Honours the same constraints as the planner engine: 'never' excludes,
    prep-ahead halves the effective time, and the previous evening's protein
    is penalised.
    """
    scored: list[tuple[float, Recipe]] = []
    for recipe in recipes:
        if recipe.slug == exclude_slug or prefs.never_by(recipe.slug):
            continue
        minutes = (
            recipe.time.same_day_minutes
            if recipe.time.prep_ahead
            else recipe.time.total_minutes
        )
        if minutes > evening.free_minutes:
            continue
        scored.append((prefs.recipe_score(recipe.slug), recipe))
    scored.sort(key=lambda item: (-item[0], item[1].slug))
    return scored[:limit]
