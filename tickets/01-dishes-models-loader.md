# 1. Dishes: models + loader

Blocks: 2, 3.

## Why

A composed dish ("aardappel-groente-vlees") is a dish made of parts, each part
filled by one of a few recipes. The definitions live next to the recipes, in
their own directory, as YAML.

## What

- `ComposedDish(slug, name, parts, notes?)` and `DishPart(key, label, options)`
  in `src/groceries_assistant/models.py`, next to `Recipe`.
  - `key` is derived from the label (`slugify`) so the definition form only has to
    ask for a label and a list of recipe slugs.
  - Part keys must be unique inside a dish (validator).
  - `ComposedDish.option_slugs` -> every recipe slug the dish can use.
  - `ComposedDish.part(key)` -> the part, or None.
- `src/groceries_assistant/dishes/loader.py`, mirroring `recipes/loader.py`:
  `DishError`, `load_dish`, `load_dishes`, `load_dishes_with_errors` (per-file
  error tolerance), `save_dish`, `dish_file(directory, slug)`.
- Tests in `tests/test_dish_loader.py`.

## Done when

- `load_dishes_with_errors` on a directory with one good and one broken file
  returns the good one and one error message.
- A dish with two parts of the same label is rejected.
- Saving and loading round-trips (name with a quote, unicode options).
