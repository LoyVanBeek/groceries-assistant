# 4. Dishes: week view with per-part picks

Blocked by: 2, 3.

## Why

The point of a composed dish is that the week row shows what you will actually
cook: the parts, joined with ` + `, with the dish name as a small label above.

## What

- `plan_view(...)` gains dishes and picks. For a dish evening it returns:
  - `name` = dish name, `parts_label` = chosen recipe names joined with ` + `,
  - `unfilled` = labels of parts without a pick,
  - `minutes` = longest part, `total_minutes` = sum,
  - `part_options` = one `<select>` per part: the part's label, its options, and
    the current pick (or "nog te kiezen").
- `plan.html`: below the dish name, the parts joined; a single form with one
  `<select name="part_<key>">` per part, a save button and
  `onchange="this.form.requestSubmit()"` like the other per-evening forms.
  The total time shows as a warning when it exceeds the budget.
- `POST /plan/picks` (day + one field per part) writes the picks. An empty option
  clears that part.
- `alternatives()` gains a `dishes=` argument: composed dishes are offered above
  the plain recipes, labelled with their part labels.
- Swapping an evening to another dish or to leftovers/no-cook clears that day's
  picks (`clear_day_picks`); approving a week must not (that is the hazard:
  `update_proposal_rows` rewrites every entry row).
- A leftovers evening copies the source evening's picks.
- Tests in `tests/test_web.py`.

## Done when

- Picking a part and submitting shows the parts joined with ` + ` on the row.
- The dish appears in the picker above the plain recipes.
- Swapping the evening to a different dish removes the old picks.
- Approving a week keeps the picks (regression test).
- Two parts of 30 and 20 minutes show `30/x min` and a `50 min totaal` note.
