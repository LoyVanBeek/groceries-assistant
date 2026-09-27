"""Service-level tests: joiners/absences flow through to people counts."""

from datetime import date

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import Person, Preferences
from groceries_assistant.services.planner_service import (
    apply_absences,
    build_order,
    plan_to_rows,
    plan_week,
    rows_to_entries,
)
from groceries_assistant.store.db import PlanEntryRow, Store

D1 = date(2026, 9, 28)
D2 = date(2026, 9, 29)


def _recipe(slug="pasta", minutes=30):
    return Recipe.model_validate(
        {
            "slug": slug,
            "name": slug,
            "servings": 4,
            "time": {"cook": minutes},
            "ingredients": [{"product": "spaghetti", "amount": 500, "unit": "g"}],
        }
    )


def _part_recipe(slug, amount, unit, minutes=10, servings=4):
    return Recipe.model_validate(
        {
            "slug": slug,
            "name": slug,
            "servings": servings,
            "time": {"cook": minutes},
            "ingredients": [{"product": slug, "amount": amount, "unit": unit}],
        }
    )


def _dish(*parts):
    return ComposedDish.model_validate(
        {
            "slug": "aardappel-groente-vlees",
            "name": "Aardappel-groente-vlees",
            "parts": [{"label": label, "options": [option]} for label, option in parts],
        }
    )


def _four_people() -> Preferences:
    return Preferences(people=[Person(id=p, role="adult") for p in ("a", "b", "c", "d")])


def test_absence_reduces_portions():
    evening = EveningAvailability(date=D1, free_minutes=120, joiners=("c", "d"))
    plan = plan_week([_recipe()], _four_people(), [evening])
    rows = plan_to_rows(plan)
    assert rows[0].people == 2


def test_apply_absences_recomputes_counts_and_leftovers(tmp_path):
    store = Store(tmp_path / "state.db")
    for person in ("a", "b", "c", "d"):
        store.upsert_person(person, person, "adult", 1.0)
    store.set_absence(D1, "a", True)
    store.set_absence(D1, "b", True)
    store.set_absence(D2, "d", True)
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug="pasta", people=4),
        PlanEntryRow(day=D2, kind="leftovers", source_slug="pasta", people=4),
    ]
    fresh = apply_absences(store, rows)
    people_by_day = {row.day: row.people for row in fresh}
    assert people_by_day[D1] == 5  # 2 joiners Monday + 3 leftovers Tuesday
    assert people_by_day[D2] == 3


def test_build_order_scales_with_joiners():
    recipes = {"pasta": _recipe()}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="pasta", people=2)]
    entries = rows_to_entries(rows, recipes)
    order = build_order(entries, recipes, staples=[])
    assert order.shopping[0].amount == 250.0


def test_no_people_configured_falls_back_to_servings(tmp_path):
    store = Store(tmp_path / "state.db")  # nobody configured
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="pasta", people=4)]
    fresh = apply_absences(store, rows)
    assert fresh[0].people == 4


def test_no_cook_rows_contribute_no_groceries():
    """An evening marked 'niet koken' (eating elsewhere) adds nothing to the order."""
    recipes = {"pasta": _recipe()}
    rows = [
        PlanEntryRow(day=D1, kind="no_cook", people=0),
        PlanEntryRow(day=D2, kind="cook", recipe_slug="pasta", people=2),
    ]
    entries = rows_to_entries(rows, recipes)
    assert [entry.date for entry in entries] == [D2]
    order = build_order(entries, recipes, staples=[])
    assert len(order.shopping) == 1
    assert order.shopping[0].amount == 250.0


# --- composed dishes ----------------------------------------------------------


def test_a_dish_evening_resolves_into_its_picked_parts():
    dish = _dish(("Aardappels", "aardappels"), ("Groente", "boerenkool"))
    recipes = {
        "aardappels": _part_recipe("aardappels", 1000, "g", minutes=30),
        "boerenkool": _part_recipe("boerenkool", 300, "g", minutes=20),
    }
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="aardappel-groente-vlees", people=4)]
    picks = {D1: {"aardappels": "aardappels", "groente": "boerenkool"}}

    entry = rows_to_entries(rows, recipes, {dish.slug: dish}, picks)[0]

    assert entry.dish_slug == "aardappel-groente-vlees"
    assert entry.recipe is None
    assert [r.slug for r in entry.parts] == ["aardappels", "boerenkool"]
    assert entry.dishes == entry.parts
    assert entry.missing == []
    # longest part, not the sum
    assert entry.minutes == entry.combined_minutes == 30
    assert entry.total_minutes == 50


def test_each_part_is_scaled_by_its_own_servings():
    """A 2-person side dish next to a 4-person main stays a 2-person side dish."""
    dish = _dish(("Vlees", "kip"), ("Groente", "sperzieboontjes"))
    recipes = {
        "kip": _part_recipe("kip", 600, "g", servings=4),  # 4 people -> 4 portions
        "sperzieboontjes": _part_recipe("sperzieboontjes", 200, "g", servings=2),  # x2
    }
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4)]
    picks = {D1: {"vlees": "kip", "groente": "sperzieboontjes"}}
    entries = rows_to_entries(rows, recipes, {dish.slug: dish}, picks)

    order = build_order(entries, recipes, staples=[])

    amounts = {item.product: item.amount for item in order.shopping}
    assert amounts == {"kip": 600.0, "sperzieboontjes": 400.0}
    assert order.warnings == []


def test_an_unfilled_part_is_reported_and_only_filled_parts_are_bought():
    dish = _dish(("Aardappels", "aardappels"), ("Groente", "boerenkool"), ("Vlees", "kip"))
    recipes = {
        "aardappels": _part_recipe("aardappels", 1000, "g"),
        "boerenkool": _part_recipe("boerenkool", 300, "g"),
        "kip": _part_recipe("kip", 600, "g"),
    }
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4)]
    picks = {D1: {"aardappels": "aardappels"}}
    entries = rows_to_entries(rows, recipes, {dish.slug: dish}, picks)

    assert entries[0].missing == ["Groente", "Vlees"]

    order = build_order(entries, recipes, staples=[])
    assert [item.product for item in order.shopping] == ["aardappels"]
    assert len(order.warnings) == 1
    assert "zonder Groente en Vlees" in order.warnings[0]


def test_a_dish_with_nothing_picked_contributes_nothing():
    dish = _dish(("Groente", "boerenkool"))
    recipes = {"boerenkool": _part_recipe("boerenkool", 300, "g")}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4)]

    assert rows_to_entries(rows, recipes, {dish.slug: dish}, {D1: {}}) == []


def test_a_pick_of_a_deleted_recipe_leaves_the_part_unfilled():
    dish = _dish(("Groente", "boerenkool"), ("Vlees", "kip"))
    recipes = {
        "boerenkool": _part_recipe("boerenkool", 300, "g"),
        "kip": _part_recipe("kip", 600, "g"),
    }
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4)]
    picks = {D1: {"groente": "weg", "vlees": "kip"}}

    entry = rows_to_entries(rows, recipes, {dish.slug: dish}, picks)[0]

    assert [r.slug for r in entry.parts] == ["kip"]
    assert entry.missing == ["Groente"]


def test_a_dish_slug_is_never_mistaken_for_a_recipe():
    """Both a recipe and a dish may carry the same slug; the dish wins."""
    dish = _dish(("Groente", "boerenkool"))
    recipes = {
        "aardappel-groente-vlees": _recipe("aardappel-groente-vlees"),
        "boerenkool": _part_recipe("boerenkool", 300, "g"),
    }
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="aardappel-groente-vlees", people=4)]

    entry = rows_to_entries(rows, recipes, {dish.slug: dish}, {D1: {"groente": "boerenkool"}})[0]

    assert entry.dish_slug == "aardappel-groente-vlees"
    assert [r.slug for r in entry.parts] == ["boerenkool"]


def test_a_leftovers_evening_of_a_dish_doubles_every_part():
    """Cook once, eat twice: both parts of the dish get extra portions."""
    dish = _dish(("Vlees", "kip"), ("Groente", "boerenkool"))
    recipes = {
        "kip": _part_recipe("kip", 600, "g"),
        "boerenkool": _part_recipe("boerenkool", 300, "g"),
    }
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4),
        PlanEntryRow(day=D2, kind="leftovers", source_slug=dish.slug, people=4),
    ]
    picks = {D1: {"vlees": "kip", "groente": "boerenkool"}}
    entries = rows_to_entries(rows, recipes, {dish.slug: dish}, picks)

    amounts = {
        item.product: item.amount for item in build_order(entries, recipes, staples=[]).shopping
    }
    assert amounts == {"kip": 1200.0, "boerenkool": 600.0}


def test_plain_recipes_keep_their_entry_shape():
    recipe = _recipe()
    entry = rows_to_entries(
        [PlanEntryRow(day=D1, kind="cook", recipe_slug="pasta")], {"pasta": recipe}
    )[0]

    assert entry.dishes == [recipe]
    assert entry.combined_minutes == entry.total_minutes == 30
    assert entry.missing == []
