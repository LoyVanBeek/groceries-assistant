# 6. Pantry: day-aware demand + allocation

Blocked by: 5. Blocks: 7.

## Why

Expiry is a per-day question, so the order can no longer be a week total. The
allocation decides, day by day, what is still needed and how many whole units to
buy -- and what the units leave behind.

## What

- `planner/shopping.py`:
  - `demand_by_day(entries) -> dict[date, dict[tuple[str, str], float]]`, the
    people-scaled per-day ingredient demand.
  - `aggregate_ingredients` stays as it is (a thin flat sum) so the existing
    tests and the no-fact path are untouched.
- New `planner/pantry.py`:
  - `StockEntry(product, available_from, expires_on, qty)`.
  - `allocate(demand_by_day, facts, carried, days) -> Allocation` with, per
    product: `bought` (packs), `qty` (bought in the demand unit), `leftovers`
    and warnings.
  - Algorithm, per product with a fact, walking days in order:
    eat in-date stock (FIFO), then `ceil(need / pack)` units; the remainder of the
    units just opened becomes stock dated that day, expiring `shelf_life` later.
  - `ceil`-safe: a need of 1e-9 still buys one unit, never zero.
  - A fact whose units cannot be converted to the demand's unit is skipped with a
    warning, never silently miscalculated.
  - Deterministic: the result must not depend on dict iteration order.
- `carried_stock(order_items, previous_demand, facts)` in the same module:
  derived remainders (`bought - used`) of the previous approved plan, dated from
  that plan. Read-only.
- Tests in `tests/test_planner_pantry.py`.

## Done when

- 350 g packs, needs 300/200/400 g on Mon/Tue/Thu -> 3 packs bought, and the
  Monday remainder is used on Tuesday and has expired by Thursday.
- A remainder created Monday is NOT used for a Friday need when it expires first.
- Two stock entries are consumed oldest first.
- A `stuk` pack with a piece weight handles a recipe written in `g` and one
  written in `stuk`.
- A fact that cannot convert is skipped with a warning.
- Shuffling the demand dict gives the same result.
