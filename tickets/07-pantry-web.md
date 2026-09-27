# 7. Pantry: order page, settings, prefill

Blocked by: 6.

## Why

A fact has to be settable from the phone, and a matched AH product already shows
its pack size -- that is the moment to offer it.

## What

- `build_order(..., facts=None, carried=None)`:
  - products without a fact take today's path, unchanged.
  - products with a fact get the allocation; the shopping line reads
    `2 stuk (700 g)` (pack count, weight in brackets) and carries a muted note
    when part of it is meant to stay: "er blijft 50 g over tot donderdag".
  - `ShoppingItem` gains `pack_count`/`pack_unit` (None for a plain line) and
    `note` ("").
  - `order_items.amount` keeps storing the demand-unit quantity, so the
    carried-stock derivation stays uniform.
- `/order` shows the note; `order.html` renders the pack count.
- `/settings`: a "verpakkingen" section listing facts with inline edit, add and
  delete. Routes `POST /settings/facts` and `POST /settings/facts/delete`.
- `/order/match`: a "voeg verpakking toe" button next to a matched product that
  parses `unit_size` into a fact (route `POST /order/match/pack`). Unparseable
  strings are not offered.
- The order page stays read-only: no write on `GET /order`.
- Tests in `tests/test_web.py` and `tests/test_planner_service.py`.

## Done when

- A product with a fact reads `2 stuk (700 g)` and its note.
- A product without a fact reads exactly as before.
- The AH match page can store a fact from a "350 g" unit size.
- Refreshing `/order` does not change any stored row.
