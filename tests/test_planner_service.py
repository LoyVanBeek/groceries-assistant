"""Service-level tests: joiners/absences flow through to people counts."""

from datetime import date

from groceries_assistant.models import Recipe
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
