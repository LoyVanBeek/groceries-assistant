"""Aggregate planned dinners into a shopping list."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from groceries_assistant.models import Recipe


@dataclass
class ShoppingItem:
    product: str
    amount: float
    unit: str


def aggregate_ingredients(
    entries: Sequence[tuple[date, Recipe, int]],
) -> tuple[list[ShoppingItem], list[str]]:
    """Sum recipe ingredients over the planned days.

    Each entry is (date, recipe, people_count); leftovers entries pass the
    original recipe with a doubled people count so the extra portions are
    bought. Returns (items sorted by product name, warnings).
    """
    warnings: list[str] = []
    totals: dict[tuple[str, str], float] = {}
    for _day, recipe, people in entries:
        factor = people / recipe.servings
        for ingredient in recipe.ingredients:
            key = (ingredient.product.lower(), ingredient.unit)
            totals[key] = totals.get(key, 0.0) + ingredient.amount * factor

    items = [
        ShoppingItem(product=product, amount=amount, unit=unit)
        for (product, unit), amount in totals.items()
    ]
    items.sort(key=lambda item: item.product.lower())
    return items, warnings
