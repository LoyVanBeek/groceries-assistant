# 8. Planner: composed dishes in plan_week

Blocked by: 2, 3.

## Why

Hand-picking the parts works, but the planner should also propose a dish and fill
the parts itself, using the same penalties it already applies to a recipe.

## What

- `store.history()` becomes a `UNION ALL` over `plan_entries` and `plan_picks`,
  so a component's recency counts too (oldest planned day wins the `MAX`).
- `plan_week(..., dishes=())` -- appended keyword, so the 304 lines of engine
  tests keep passing untouched.
  - For a dish: fill each part with an option, scoring each option with the
    existing per-recipe penalties (already planned this week = penalty, `never` =
    hard skip, recency, same protein as the previous evening).
  - `never` on the dish's own slug excludes the whole dish.
  - A dish with no usable option set is skipped.
  - Dish score = mean of the chosen parts' scores; a verdict on the dish slug
    replaces that mean entirely.
  - Time = max of the parts' same-day minutes (prep-ahead summed against spare
    capacity, one warning); the sum shows as a warning when it blows the budget.
  - `planned` contains the dish slug and the chosen part slugs, so a part is not
    reused in the same week.
- `propose(recipes, store, start, dishes=())` passes them through.
- Tests in `tests/test_planner_engine.py`.

## Done when

- A dish is proposed with every part filled and the reason names the combination.
- `never` on one option still picks the dish with another option; `never` on all
  of them skips the dish; `never` on the dish slug skips the dish.
- Two dishes never share a component in one week.
- A dish that does not fit the time budget is skipped while a plain recipe fits.
