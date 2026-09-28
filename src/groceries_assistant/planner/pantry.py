"""Whole purchases and dated stock: leftovers as arithmetic, not bookkeeping.

A product with a declared fact is bought in whole packs. What a meal uses, it
uses from a pack; what the pack has left over is stock, dated and usable for a
few days -- in the same week and in any other dish that needs the product.
Everything else is summed as before, so a product without a fact is untouched.

Two rules keep the answer honest:

- expiry is exclusive: a remainder is good from the day it was opened until the
  day it expires, and never after;
- a fact the demand cannot be expressed in is not applied. The product falls
  back to its plain sum and the list says why, because a wrong pack count is
  worse than no pack count.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from groceries_assistant.models import ProductFact
from groceries_assistant.planner.shopping import ShoppingItem, demand_by_day

# Units we can weigh, as grams. A fact may be in any of them, a recipe in any
# other, so the two have to meet somewhere.
WEIGHT_IN_GRAMS = {
    "g": 1.0,
    "gram": 1.0,
    "gr": 1.0,
    "kg": 1000.0,
    "ml": 1.0,
    "l": 1000.0,
    "cl": 10.0,
    "dl": 100.0,
}
PIECE_UNITS = {"stuk", "stuks", "st", "stk", "a", "e"}

# Below this, a quantity is nothing left of something: 1e-9 g is not stock.
EPSILON = 1e-9


@dataclass(frozen=True)
class StockEntry:
    """A quantity of one product that is in the house until a date."""

    product: str
    available_from: date
    expires_on: date
    qty: float
    unit: str
    """The unit `qty` is in: the fact's measure ('g' or 'stuk')."""

    def is_good_on(self, day: date) -> bool:
        """True from the day it was opened up to (not including) the day it expires."""
        return self.available_from <= day < self.expires_on


@dataclass
class _Lot:
    """One batch of stock while we walk the days: its dates, and what is left of it."""

    available_from: date
    expires_on: date
    remaining: float
    product: str = ""
    unit: str = ""

    def is_good_on(self, day: date) -> bool:
        return self.available_from <= day < self.expires_on

    def as_entry(self) -> StockEntry:
        return StockEntry(
            product=self.product,
            available_from=self.available_from,
            expires_on=self.expires_on,
            qty=self.remaining,
            unit=self.unit,
        )


@dataclass
class Allocation:
    """What to buy, what the purchases leave over, and what to watch out for."""

    items: list[ShoppingItem] = field(default_factory=list)
    stock: list[StockEntry] = field(default_factory=list)
    """Stock the week ends with, dated -- next week's leftovers start here."""
    warnings: list[str] = field(default_factory=list)


def to_measure(qty: float, unit: str, fact: ProductFact) -> float | None:
    """`qty unit` as the fact measures it; None when the two do not meet.

    A pack of pieces knows one piece's weight (1 stuk = 350 g), so a recipe that
    counts pieces and one that weighs them are both understood. A pack with no
    known piece weight cannot answer a recipe in grams, and does not pretend to.
    """
    measure = fact.pack_measure
    if unit == measure:
        return qty
    if unit in WEIGHT_IN_GRAMS and measure in WEIGHT_IN_GRAMS:
        return qty * WEIGHT_IN_GRAMS[unit] / WEIGHT_IN_GRAMS[measure]
    if unit == fact.pack_unit and fact.unit_qty and fact.unit_measure == measure:
        return qty * fact.unit_qty
    return None


def packs_for(need: float, pack_contents: float) -> float:
    """How many whole packs cover a need. Any need at all buys one pack.

    Snapping a need that is a whole number of packs back onto it keeps float
    noise (`0.35 * 1000`) from buying a second pack.
    """
    if need <= 0.0:
        return 0.0
    ratio = need / pack_contents
    nearest = round(ratio)
    if nearest >= 1 and abs(ratio - nearest) < EPSILON:
        ratio = nearest
    return float(math.ceil(ratio))


def _eat(lots: list[_Lot], need: float, day: date) -> float:
    """Take what is in date, oldest batch first. Returns what is still needed.

    `lots` must be sorted oldest first, and is consumed in place.
    """
    for lot in lots:
        if need <= 0.0:
            break
        if not lot.is_good_on(day):
            continue
        taken = min(need, lot.remaining)
        lot.remaining -= taken
        need -= taken
    return need


def _by_unit(
    demand: Mapping[date, Mapping[tuple[str, str], float]], product: str
) -> dict[date, dict[str, float]]:
    """The demand for one product per day, grouped by unit."""
    per_day: dict[date, dict[str, float]] = {}
    for day in sorted(demand):
        for (name, unit), qty in sorted(demand[day].items()):
            if name != product:
                continue
            units = per_day.setdefault(day, {})
            units[unit] = units.get(unit, 0.0) + qty
    return per_day


def _flat_sum(
    demand: Mapping[date, Mapping[tuple[str, str], float]],
    product: str,
    note: str | None = None,
) -> list[ShoppingItem]:
    """The summed need, one line per unit -- what a product without a fact gets."""
    totals: dict[str, float] = {}
    for per_unit in _by_unit(demand, product).values():
        for unit, qty in per_unit.items():
            totals[unit] = totals.get(unit, 0.0) + qty
    return [
        ShoppingItem(product=product, amount=qty, unit=unit, note=note)
        for unit, qty in sorted(totals.items())
    ]


def shopping_item(product: str, packs: float, contents: float, fact: ProductFact) -> ShoppingItem:
    """The line for the list: how many packs to take, and what that weighs.

    '2 stuk (700 g)' for a product sold per piece, '1000 g (2 x 500 g)' for one
    sold by weight. The amount stays the weight in the fact's own measure, which
    is what the list has always carried.
    """
    if fact.pack_unit in PIECE_UNITS and fact.unit_measure:
        note = f"{contents:g} {fact.unit_measure}".replace(".", ",")
        return ShoppingItem(
            product=product,
            amount=contents,
            unit=fact.unit_measure,
            pack_count=packs,
            pack_unit=fact.pack_unit,
            note=note,
        )
    if fact.pack_unit in PIECE_UNITS:
        # Pieces, but we were not told what one weighs: one pack is a bundle.
        return ShoppingItem(
            product=product,
            amount=contents,
            unit=fact.pack_unit,
            pack_count=packs,
            pack_unit=fact.pack_unit,
            note=f"{fact.pack_qty:g} per verpakking",
        )
    # A weight pack: the total weight is the line, the pack size the note.
    return ShoppingItem(
        product=product,
        amount=contents,
        unit=fact.pack_unit,
        note=f"{packs:g} x {fact.pack_qty:g} {fact.pack_unit}",
    )


def allocate(
    demand: Mapping[date, Mapping[tuple[str, str], float]],
    facts: Mapping[str, ProductFact],
    carried: Sequence[StockEntry] = (),
    days: Sequence[date] = (),
    notes: Mapping[str, str] | None = None,
) -> Allocation:
    """Turn a week's demand into whole purchases, dated stock and warnings.

    Walks the days in order, per product: what is in date is eaten first (oldest
    batch first), then whole packs are bought and whatever they have left over
    becomes stock dated that day. A product with no fact, or whose units the
    fact cannot express, keeps its plain sum.

    `notes` carries the bracketed preparation hints `aggregate_ingredients`
    stripped from the product names (by lowercase product); they land back on
    the item's note so the list says 'kipfilet (in stukjes)' without the text
    reaching the AH search.
    """
    notes = notes or {}
    order = sorted(set(days) | set(demand))
    # Oldest batch first, once: a remainder is eaten before a fresher one.
    on_hand: dict[str, list[StockEntry]] = {}
    for entry in sorted(carried, key=_lot_order):
        on_hand.setdefault(entry.product, []).append(entry)

    allocation = Allocation()
    for product in sorted({name for per_day in demand.values() for name, _unit in per_day}):
        fact = facts.get(product)
        per_day = _by_unit(demand, product)
        if fact is None:
            allocation.items += _flat_sum(demand, product, notes.get(product))
            continue
        converted, warning = _convert_all(per_day, fact)
        if warning is not None:
            allocation.warnings.append(warning)
            allocation.items += _flat_sum(demand, product, notes.get(product))
            continue
        if not any(need > 0.0 for need in converted.values()):
            continue  # nobody needs any of it, so there is nothing to buy
        lots = [
            _Lot(
                available_from=entry.available_from,
                expires_on=entry.expires_on,
                remaining=entry.qty,
                product=entry.product,
                unit=entry.unit,
            )
            for entry in on_hand.get(product, ())
        ]
        packs, contents, stock = _buy(converted, fact, lots, order, product)
        allocation.items.append(shopping_item(product, packs, contents, fact))
        allocation.stock += stock
    allocation.items.sort(key=lambda item: item.product.lower())
    allocation.stock.sort(key=lambda entry: (entry.product, entry.available_from))
    return allocation


def _lot_order(entry: StockEntry) -> tuple[date, date]:
    """Oldest batch first: opened first, and so expiring first."""
    return (entry.available_from, entry.expires_on)


def _convert_all(
    per_day: Mapping[date, Mapping[str, float]], fact: ProductFact
) -> tuple[dict[date, float], str | None]:
    """Every day's need in the fact's measure, or a warning why not."""
    converted: dict[date, float] = {}
    for day, per_unit in per_day.items():
        total = 0.0
        for unit, qty in sorted(per_unit.items()):
            measure_qty = to_measure(qty, unit, fact)
            if measure_qty is None:
                return {}, (
                    f"{fact.product}: {qty:g} {unit} past niet in {fact.pack_measure}; "
                    f"de gewone som staat op de lijst"
                )
            total += measure_qty
        converted[day] = total
    return converted, None


def _buy(
    converted: Mapping[date, float],
    fact: ProductFact,
    lots: list[_Lot],
    days: Sequence[date],
    product: str,
) -> tuple[float, float, list[StockEntry]]:
    """Buy what the days still need after eating what is in date.

    Returns the packs bought, their total contents, and the stock that is still
    good when the plan runs out: what came in and was not used, plus what the
    new purchases leave. A batch that expires during the week is not stock by
    then -- it is in the bin.
    """
    packs = 0.0
    contents = 0.0
    for day in days:
        need = _eat(lots, converted.get(day, 0.0), day)
        if need <= 0.0:
            continue
        bought = packs_for(need, fact.pack_contents)
        opened = bought * fact.pack_contents
        packs += bought
        contents += opened
        leftover = opened - need
        if leftover > EPSILON:
            lots.append(
                _Lot(
                    available_from=day,
                    expires_on=day + fact.shelf_life,
                    remaining=leftover,
                    product=product,
                    unit=fact.pack_measure,
                )
            )
    last_day = days[-1] if days else date.min
    return (
        packs,
        contents,
        [lot.as_entry() for lot in lots if lot.remaining > EPSILON and lot.is_good_on(last_day)],
    )


def carried_stock(
    order_items: Sequence[Mapping[str, object]],
    previous_demand: Mapping[date, Mapping[tuple[str, str], float]],
    facts: Mapping[str, ProductFact],
) -> list[StockEntry]:
    """What last week's purchases left over, dated from the day they were opened.

    Read-only: derived from what was bought and what the week used. A product
    with no fact has no stock -- it was bought as it was needed. What is left
    keeps from the first day it was needed, so a remainder is as old as the
    earliest pack that was opened.
    """
    stock: list[StockEntry] = []
    bought_by_unit: dict[str, dict[str, float]] = {}
    for item in order_items:
        name = str(item["product"]).lower()
        unit = str(item["unit"])
        amount = float(item["amount"])  # ty: ignore[invalid-argument-type]
        units = bought_by_unit.setdefault(name, {})
        units[unit] = units.get(unit, 0.0) + amount

    for product, fact in sorted(facts.items()):
        per_day = _by_unit(previous_demand, product)
        if not per_day or product not in bought_by_unit:
            continue
        opened_on = min(per_day)
        bought = 0.0
        for unit, amount in sorted(bought_by_unit[product].items()):
            measure_qty = to_measure(amount, unit, fact)
            if measure_qty is not None:
                bought += measure_qty
        used = sum(
            to_measure(qty, unit, fact) or 0.0
            for per_unit in per_day.values()
            for unit, qty in per_unit.items()
        )
        left = bought - used
        if left > EPSILON:
            stock.append(
                StockEntry(
                    product=product,
                    available_from=opened_on,
                    expires_on=opened_on + fact.shelf_life,
                    qty=left,
                    unit=fact.pack_measure,
                )
            )
    return stock


__all__ = [
    "Allocation",
    "StockEntry",
    "allocate",
    "carried_stock",
    "demand_by_day",
    "packs_for",
    "shopping_item",
    "to_measure",
]
