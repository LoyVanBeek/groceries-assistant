"""Service-level tests: joiners/absences flow through to people counts."""

from datetime import date, timedelta

from groceries_assistant.ah.client import AHProduct
from groceries_assistant.ah.matching import AHMappingStore, fact_from_unit_size
from groceries_assistant.models import ComposedDish, ProductFact, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import Person, Preferences
from groceries_assistant.services.planner_service import (
    apply_absences,
    build_order,
    last_weeks_stock,
    match_products,
    plan_to_picks,
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


def test_a_fresh_match_keeps_ahs_pack_size_for_a_later_suggestion(tmp_path):
    """The unit size is only ever a suggestion, but it has to survive to be one."""
    mappings = AHMappingStore(tmp_path / "mappings.yaml")
    candidates = [AHProduct(webshop_id=7, title="AH Broccoli 350 g", unit_size="350 g")]

    assert match_products(["broccoli"], lambda _query: candidates, mappings) == {
        "broccoli": (7, "AH Broccoli 350 g")
    }

    fact = fact_from_unit_size("broccoli", mappings.get("broccoli").ah_unit_size)
    assert (fact.pack_qty, fact.pack_unit) == (350, "g")


# --- pack sizes and leftovers -------------------------------------------------


def _broccoli(amount: int, minutes: int = 10, servings: int = 4) -> Recipe:
    return Recipe.model_validate(
        {
            "slug": "broccoli",
            "name": "broccoli",
            "servings": servings,
            "time": {"cook": minutes},
            "ingredients": [{"product": "Broccoli", "amount": amount, "unit": "g"}],
        }
    )


def _fact(**kwargs) -> ProductFact:
    return ProductFact(product="broccoli", **kwargs)


def test_a_declared_pack_puts_a_count_on_the_list():
    recipes = {"broccoli": _broccoli(500)}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4)]
    facts = {"broccoli": _fact(pack_qty=1, pack_unit="stuk", unit_qty=350, unit_measure="g")}

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts=facts)

    item = order.shopping[0]
    assert (item.product, item.pack_count, item.pack_unit) == ("broccoli", 2, "stuk")
    assert (item.amount, item.unit, item.note) == (700, "g", "700 g")


def test_a_product_without_a_pack_reads_exactly_as_before():
    recipes = {"broccoli": _broccoli(500)}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4)]

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts={})

    assert (order.shopping[0].amount, order.shopping[0].unit) == (500, "g")
    assert order.shopping[0].pack_count is None and order.shopping[0].note is None


def test_the_order_says_what_a_meal_leaves_behind():
    recipes = {"broccoli": _broccoli(300)}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4)]
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")}

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts=facts)

    assert [(e.qty, e.unit, e.available_from) for e in order.leftovers] == [(50, "g", D1)]
    assert order.leftovers[0].expires_on == D1 + timedelta(days=3)


def test_two_meals_of_one_need_share_a_pack():
    recipes = {"broccoli": _broccoli(300)}
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4),
        PlanEntryRow(day=D2, kind="cook", recipe_slug="broccoli", people=4),
    ]
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")}

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts=facts)

    assert order.shopping[0].pack_count == 2  # 600 g needed, 350 g per pack
    assert order.leftovers[0].qty == 100  # Tuesday's pack is what is left


def test_a_leftover_evening_does_not_buy_a_second_pack():
    """Cook once on Monday, eat it on Tuesday: the portions are bought once."""
    recipes = {"broccoli": _broccoli(350)}
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4),
        PlanEntryRow(day=D2, kind="leftovers", source_slug="broccoli", people=4),
    ]
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")}

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts=facts)

    # 700 g for two evenings of four, so two packs, and nothing left over
    assert (order.shopping[0].pack_count, order.shopping[0].amount) == (2, 700)
    assert order.leftovers == []


def test_a_pack_the_recipes_cannot_express_warns_and_sums(tmp_path):
    recipes = {"broccoli": _broccoli(300)}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4)]
    facts = {"broccoli": _fact(pack_qty=6, pack_unit="stuk")}  # a bundle, weight unknown

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=[], facts=facts)

    assert (order.shopping[0].amount, order.shopping[0].pack_count) == (300, None)
    assert order.warnings == ["broccoli: 300 g past niet in stuk; de gewone som staat op de lijst"]


def test_a_staple_keeps_its_own_list():
    recipes = {"broccoli": _broccoli(300)}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug="broccoli", people=4)]
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")}

    order = build_order(rows_to_entries(rows, recipes), recipes, staples=["Broccoli"], facts=facts)

    assert order.shopping == [] and order.staples[0].pack_count == 1


def test_last_weeks_remainder_is_used_before_buying(tmp_path):
    """Week two needs 300 g; week one left 350 g on the shelf for 30 days."""
    store = Store(tmp_path / "state.db")
    recipes = {"broccoli": _broccoli(300)}
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g", shelf_life_days=30)}
    first = store.approve_plan(
        date(2026, 9, 28),
        [PlanEntryRow(day=date(2026, 9, 28), kind="cook", people=4, recipe_slug="broccoli")],
    )
    # 700 g bought for 300 g of need: 400 g is still in the house
    store.save_order_items(first, [("broccoli", "g", 700, None, None, None, None)])
    second = store.approve_plan(
        date(2026, 10, 5),
        [PlanEntryRow(day=date(2026, 10, 5), kind="cook", people=4, recipe_slug="broccoli")],
    )

    carried = last_weeks_stock(store, second, recipes, {}, facts)
    assert [(e.qty, e.unit) for e in carried] == [(400, "g")]

    order = build_order(
        rows_to_entries(store.plan_entries(second), recipes),
        recipes,
        staples=[],
        facts=facts,
        carried=carried,
    )
    assert order.shopping[0].pack_count == 0  # 300 g of the 400 g in the house
    assert [e.qty for e in order.leftovers] == [100]  # and 100 g keeps for next week


def test_the_first_week_has_nothing_to_carry(tmp_path):
    store = Store(tmp_path / "state.db")
    first = store.approve_plan(D1, [PlanEntryRow(day=D1, kind="cook", recipe_slug="pasta")])

    assert last_weeks_stock(store, first, {}, {}, {}) == []


def test_a_dish_evening_buys_the_parts_it_chose(tmp_path):
    """The parts of a planned dish reach the list, each with its own servings."""
    dish = _dish(("Groente", "broccoli"), ("Vlees", "kip"))
    recipes = {"broccoli": _broccoli(300), "kip": _part_recipe("kip", 600, "g")}
    rows = [PlanEntryRow(day=D1, kind="cook", recipe_slug=dish.slug, people=4)]
    picks = {D1: {"groente": "broccoli", "vlees": "kip"}}

    order = build_order(
        rows_to_entries(rows, recipes, {dish.slug: dish}, picks),
        recipes,
        staples=[],
        facts={"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")},
    )

    lines = {item.product: item for item in order.shopping}
    assert lines["broccoli"].pack_count == 1  # 300 g of a 350 g pack
    assert lines["kip"].amount == 600 and lines["kip"].pack_count is None


# --- the planner's own picks ---------------------------------------------------


def _plan_with_a_dish_filled():
    """What plan_week returns for a dish evening, without going through the UI."""
    dish = ComposedDish.model_validate(
        {
            "slug": "aardappel-groente-vlees",
            "name": "Aardappel-groente-vlees",
            "parts": [
                {"label": "Aardappels", "options": ["aardappels"]},
                {"label": "Groente", "options": ["sperziebonen", "boerenkool"]},
            ],
        }
    )
    recipes = [
        _part_recipe("aardappels", 1000, "g", minutes=30),
        _part_recipe("sperziebonen", 300, "g"),
        _part_recipe("boerenkool", 300, "g"),
    ]
    prefs = Preferences(
        people=[Person(id="a", role="adult")],
        verdicts={"aardappel-groente-vlees": {"a": "love"}},
    )
    evening = EveningAvailability(date=D1, free_minutes=60, joiners=("a",))
    plan = plan_week(recipes, prefs, [evening], dishes=[dish])
    return plan, {dish.slug: dish}


def test_the_parts_the_planner_chose_become_picks():
    plan, dishes = _plan_with_a_dish_filled()

    # boerenkool wins the groente part: the parts score the same, so the slug decides
    assert plan_to_picks(plan, dishes) == {
        D1: {"aardappels": "aardappels", "groente": "boerenkool"}
    }


def test_a_plain_evening_has_no_picks():
    plan = plan_week(
        [_recipe("pasta")], _four_people(), [EveningAvailability(date=D1, free_minutes=60)]
    )

    assert plan_to_picks(plan, {}) == {}


def test_a_dish_evening_is_stored_under_the_dish_slug():
    plan, dishes = _plan_with_a_dish_filled()
    rows = plan_to_rows(plan)

    assert [(row.kind, row.recipe_slug) for row in rows] == [("cook", "aardappel-groente-vlees")]
    entries = rows_to_entries(
        rows,
        {part.slug: part for part in plan.entries[0].parts},
        dishes,
        plan_to_picks(plan, dishes),
    )
    assert entries[0].dish_slug == "aardappel-groente-vlees"
    assert [part.slug for part in entries[0].parts] == ["aardappels", "boerenkool"]
    assert entries[0].missing == []


def test_a_dish_evening_with_half_the_picks_says_which_part_is_open():
    plan, dishes = _plan_with_a_dish_filled()
    rows = plan_to_rows(plan)

    entries = rows_to_entries(
        rows,
        {part.slug: part for part in plan.entries[0].parts},
        dishes,
        {D1: {"aardappels": "aardappels"}},
    )

    assert [part.slug for part in entries[0].parts] == ["aardappels"]
    assert entries[0].missing == ["Groente"]
