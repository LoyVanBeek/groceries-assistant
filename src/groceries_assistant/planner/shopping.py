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
    pack_count: float | None = None
    """How many whole packs to take, when the product has a declared pack."""
    pack_unit: str | None = None
    """The unit `pack_count` is in ('stuk')."""
    note: str | None = None
    """What the packs add up to: '700 g', '2 x 500 g', '6 per verpakking'."""


def demand_by_day(
    entries: Sequence[tuple[date, Recipe, int]],
) -> dict[date, dict[tuple[str, str], float]]:
    """What each day needs, per product and unit, scaled to its people count.

    Leftovers entries pass the original recipe with a doubled people count, so
    the extra portions land on the day they are eaten. Demand is per day
    because whether stock is still good is a question about a date, not a sum.
    """
    per_day: dict[date, dict[tuple[str, str], float]] = {}
    for day, recipe, people in entries:
        factor = people / recipe.servings
        totals = per_day.setdefault(day, {})
        for ingredient in recipe.ingredients:
            key = (ingredient.product.lower(), ingredient.unit)
            totals[key] = totals.get(key, 0.0) + ingredient.amount * factor
    return per_day


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
