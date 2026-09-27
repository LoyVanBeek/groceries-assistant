# 9. Planner: persist picks + reasons + verdicts

Blocked by: 8.

## Why

A proposed dish is only useful if the parts it chose survive into the week view
as real picks, and if you can say "never again, this combination" as a verdict.

## What

- `plan_to_rows` / `save_proposal` persist the planner's per-part picks
  (`plan_picks`), so a proposal lands in the DB fully formed and `/plan/picks`
  can still overwrite them.
- Reason strings name the combination: the part labels with the chosen recipe
  names, plus the existing time and total.
- The week view shows a verdict row for a dish slug next to the recipe verdicts
  (`/recipes` or the week page) so `never` on a dish is one click.
  Verdicts are keyed by slug, which is what the store already does.
- Tests in `tests/test_planner_service.py` and `tests/test_web.py`.

## Done when

- A proposed dish's picks are in `plan_picks` after the proposal is saved and
  show up in the week view selects.
- A `never` verdict on a dish slug stops the planner proposing it.
- The reason string mentions every chosen part.
