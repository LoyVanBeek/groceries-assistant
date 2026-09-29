"""Aggregate planned dinners into a shopping list."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from groceries_assistant.models import Recipe

_BRACKETS = re.compile(r"\s*\([^)]*\)")


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


def order_product(product: str) -> tuple[str, str | None]:
    """Split an ingredient name into the orderable product and a note.

    Recipe names carry preparation hints in brackets ('kipfilet (in
    stukjes)', 'fijne spaghetti (pak à 500 g)') that are not part of what
    you buy. The brackets move to the note so the product stays one clean,
    matchable name. Multiple brackets join with ', '.
    """
    notes = re.findall(r"\(([^)]*)\)", product)
    clean = _BRACKETS.sub("", product)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    if not clean:
        # the whole name was in brackets; keep it as-is rather than an empty line
        clean = product.strip()
        return clean, None
    if not notes:
        return clean, None
    return clean, ", ".join(note.strip() for note in notes if note.strip()) or None


def _factor(recipe: Recipe, people: int) -> float:
    """How much to multiply a recipe's amounts by for `people` eaters.

    One for a component: it is the purchase itself ('a bag of potatoes'), so the
    amount the dish says is the amount that goes in the basket, whatever the
    household size is.
    """
    return people / recipe.servings if recipe.scales_with_servings else 1.0


def demand_by_day(
    entries: Sequence[tuple[date, Recipe, int]],
) -> dict[date, dict[tuple[str, str], float]]:
    """What each day needs, per product and unit, scaled to its people count.

    Leftovers entries pass the original recipe with a doubled people count, so
    the extra portions land on the day they are eaten. Demand is per day
    because whether stock is still good is a question about a date, not a sum.

    Products are keyed by their orderable name (brackets stripped), so a fact
    on 'kipfilet' also covers 'kipfilet (in stukjes)'.
    """
    per_day: dict[date, dict[tuple[str, str], float]] = {}
    for day, recipe, people in entries:
        factor = _factor(recipe, people)
        totals = per_day.setdefault(day, {})
        for ingredient in recipe.ingredients:
            name, _note = order_product(ingredient.product)
            key = (name.lower(), ingredient.unit)
            totals[key] = totals.get(key, 0.0) + ingredient.amount * factor
    return per_day


def aggregate_ingredients(
    entries: Sequence[tuple[date, Recipe, int]],
) -> tuple[list[ShoppingItem], list[str]]:
    """Sum recipe ingredients over the planned days.

    Each entry is (date, recipe, people_count); leftovers entries pass the
    original recipe with a doubled people count so the extra portions are
    bought. Returns (items sorted by product name, warnings).

    Bracketed preparation hints are stripped from the product and kept as the
    item's note, so the list says 'kipfilet (in stukjes)' without that text
    landing in the AH search.
    """
    warnings: list[str] = []
    totals: dict[tuple[str, str], float] = {}
    notes: dict[tuple[str, str], str] = {}
    for _day, recipe, people in entries:
        factor = _factor(recipe, people)
        for ingredient in recipe.ingredients:
            name, note = order_product(ingredient.product)
            key = (name.lower(), ingredient.unit)
            totals[key] = totals.get(key, 0.0) + ingredient.amount * factor
            if note:
                notes[key] = note

    items = [
        ShoppingItem(product=product, amount=amount, unit=unit, note=notes.get((product, unit)))
        for (product, unit), amount in totals.items()
    ]
    items.sort(key=lambda item: item.product.lower())
    return items, warnings
