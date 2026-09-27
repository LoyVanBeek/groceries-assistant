# 5. Pantry: product facts + AH unit size

Blocks: 6.

## Why

The leftover should not be a thing you declare. It is arithmetic: you buy whole
things, and what a purchase has left over is stock with a shelf life. That needs
one fact per product -- how one purchase looks, and how long it keeps.

## What

- `product_facts(product PRIMARY KEY, pack_qty, pack_unit, unit_qty, unit_measure,
  shelf_life_days DEFAULT 3)`, `CREATE TABLE IF NOT EXISTS`, no migration.
  - `pack_qty`/`pack_unit` = one purchase ("1 stuk", "500 g").
  - `unit_qty`/`unit_measure` = the weight of one piece when sold per piece
    (1 stuk = 350 g), NULL for a weight pack.
  - Fact dataclass `ProductFact` in `models.py` with a `shelf_life` of
    `timedelta(days=shelf_life_days)`.
- Store: `product_facts()`, `set_product_fact(...)`, `delete_product_fact(product)`.
- `AHMapping` gains `ah_unit_size: str | None` (backward compatible YAML
  addition) so a confirmed match can suggest a pack size; `match_products` and
  the pin route record it.
- `parse_unit_size(text)` in `ah/matching.py`: "350 g" -> 350 g, "1 kg" -> 1000 g,
  "2 x 100 g" -> 200 g, "6 stuks" -> 6 stuk; anything else -> None. Never
  guesses; a None result means the user types it.
- Tests in `tests/test_store.py` and `tests/test_ah_matching.py`.

## Done when

- A fact round-trips through the store.
- `parse_unit_size` handles the four shapes and returns None for junk.
- An existing `ah_mappings.yaml` without `ah_unit_size` still loads.
