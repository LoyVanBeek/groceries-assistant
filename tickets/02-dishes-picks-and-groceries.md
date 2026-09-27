# 2. Dishes: plan picks + grocery plumbing

Blocked by: 1. Blocks: 4, 8.

## Why

A dish on a plan entry is worthless until the user says which recipe fills each
part. That choice is per plan, per day, per part -- so it needs its own table.
The chosen parts must then flow into the grocery list with correct scaling.

## What

- `plan_picks(plan_id, day, part_key, recipe_slug)`, PK (plan_id, day, part_key),
  `ON DELETE CASCADE` to plans. No migration (`CREATE TABLE IF NOT EXISTS`).
- Store: `plan_picks(plan_id)`, `set_pick(...)`, `clear_day_picks(plan_id, day)`,
  and `rename_recipe` also re-points `plan_picks.recipe_slug`.
- `PlanEntry` gains `dish_slug`, `parts: list[Recipe]` and `missing: list[str]`
  (labels of parts without a pick), plus:
  - `dishes` -> the recipes to actually cook (parts, or the single recipe).
  - `combined_minutes` -> longest part's same-day minutes.
  - `total_minutes` -> sum over parts.
- `rows_to_entries(rows, recipes_by_slug, dishes_by_slug=None, picks=None)`:
  a row whose slug is a dish resolves into parts; unfilled parts land in
  `missing` instead of silently vanishing. New args are optional so the existing
  tests keep passing.
- `build_order` cooks `entry.dishes` instead of `entry.recipe`, so every part is
  scaled by its own `servings`. `aggregate_ingredients` itself is unchanged.
- A dish evening that is missing parts adds a warning to the order.

## Done when

- Two parts with different `servings` are each scaled correctly (4 people, part
  servings 2 and 4 -> 2x and 1x).
- A dish with an unfilled part buys the filled parts and warns about the rest.
- Deleting the proposal cascades its picks away.
