# 3. Dishes: definition pages at /dishes

Blocked by: 1.

## Why

The dish definitions have to be editable from the phone: four recipes is easy, a
hand-edited YAML file on a server is not.

## What

- Four routes:
  - `GET /dishes` -- list of dishes, a "new" form, load errors.
  - `POST /dishes` -- create from the form; slug from the name, 400 on duplicate.
  - `GET /dishes/{slug}` -- edit form, pre-filled.
  - `POST /dishes/{slug}` -- save (name change keeps the file, slug stays fixed).
  - `POST /dishes/{slug}/delete` -- remove the file.
- `dishes.html`: one form. Parts as a textarea, one part per line:
  `Aardappels: aardappels-uit-de-oven, gekookte-aardappels`
  (label, colon, comma-separated recipe slugs). No JS.
- Unknown recipe slugs are surfaced as a warning on the page, not an error -- a
  deleted recipe should not block editing the rest.
- Nav link in `base.html`, active state on `/dishes`.
- Tests in `tests/test_web.py`.

## Done when

- Creating a dish with two parts writes `data/dishes/<slug>.yaml` and it shows up
  in the list.
- A line without a colon is rejected with a Dutch error naming the line.
- Deleting removes the file and the dish.
