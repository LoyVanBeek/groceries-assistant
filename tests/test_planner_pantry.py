"""Whole packs, dated stock: what a week's need turns into on the list."""

from datetime import date

import pytest

from groceries_assistant.models import ProductFact, Recipe
from groceries_assistant.planner.pantry import (
    Allocation,
    StockEntry,
    allocate,
    carried_stock,
    demand_by_day,
    packs_for,
    to_measure,
)

MON = date(2026, 9, 28)
TUE = date(2026, 9, 29)
WED = date(2026, 9, 30)
THU = date(2026, 10, 1)
FRI = date(2026, 10, 2)
NEXT_MON = date(2026, 10, 5)


def _demand(days):
    """{day: {('broccoli', 'g'): 300}} as the planner hands the demand over."""
    return {day: dict(units) for day, units in days.items()}


def _fact(**kwargs):
    return ProductFact(product="broccoli", **kwargs)


BROCCOLI_350 = _fact(pack_qty=1, pack_unit="stuk", unit_qty=350, unit_measure="g")


def test_a_pack_of_pieces_serves_two_days_from_what_it_leaves():
    # 350 g packs with 300 g Monday, 200 g Tuesday and 400 g Thursday. Monday's
    # remainder is eaten on Tuesday, and is gone by Thursday: three packs.
    result = allocate(
        _demand(
            {
                MON: {("broccoli", "g"): 300},
                TUE: {("broccoli", "g"): 200},
                THU: {("broccoli", "g"): 400},
            }
        ),
        {"broccoli": BROCCOLI_350},
    )

    item = result.items[0]
    assert (item.pack_count, item.pack_unit, item.note) == (3, "stuk", "1050 g")
    # Tuesday's 200 g was eaten on Thursday; Thursday's own 150 g is what is left
    assert result.stock == [StockEntry("broccoli", THU, date(2026, 10, 4), 150, "g")]


def test_the_line_says_how_many_to_take_and_what_that_weighs():
    result = allocate(_demand({MON: {("broccoli", "g"): 700}}), {"broccoli": BROCCOLI_350})

    assert (result.items[0].pack_count, result.items[0].note) == (2, "700 g")


def test_a_remainder_is_not_used_after_it_expires():
    # A remainder from Monday is good Mon/Tue/Wed with a three-day shelf life,
    # so Friday's 100 g need buys a whole new pack.
    result = allocate(
        _demand({MON: {("broccoli", "g"): 300}, FRI: {("broccoli", "g"): 100}}),
        {"broccoli": BROCCOLI_350},
    )

    assert result.items[0].pack_count == 2  # 350 g for Monday, 350 g for Friday
    # Monday's 50 g expired before Friday, so only Friday's own remainder counts
    assert result.stock == [StockEntry("broccoli", FRI, date(2026, 10, 5), 250, "g")]


def test_two_batches_are_eaten_oldest_first():
    carried = [
        StockEntry("broccoli", MON, THU, 100, "g"),
        StockEntry("broccoli", TUE, FRI, 200, "g"),
    ]
    result = allocate(
        _demand({WED: {("broccoli", "g"): 250}}),
        {"broccoli": BROCCOLI_350},
        carried=carried,
    )

    assert result.items[0].pack_count == 0  # the two batches together cover it
    assert [entry.qty for entry in result.stock] == [50]


def test_a_pack_of_pieces_handles_a_recipe_counting_pieces():
    result = allocate(
        _demand({MON: {("broccoli", "stuk"): 3}}),
        {"broccoli": BROCCOLI_350},
    )

    # 3 pieces need 3 packs of 1 piece; the weight is only a note
    assert (result.items[0].pack_count, result.items[0].amount, result.items[0].unit) == (
        3,
        1050,
        "g",
    )


def test_a_recipe_in_kg_against_a_pack_in_grams():
    result = allocate(_demand({MON: {("broccoli", "kg"): 0.7}}), {"broccoli": BROCCOLI_350})

    assert (result.items[0].pack_count, result.items[0].amount) == (2, 700)


def test_a_product_the_fact_cannot_express_falls_back_to_its_sum():
    # A bundle of 6 with no known piece weight cannot answer a recipe in grams.
    bundle = _fact(pack_qty=6, pack_unit="stuk")
    result = allocate(_demand({MON: {("broccoli", "g"): 300}}), {"broccoli": bundle})

    assert (result.items[0].amount, result.items[0].unit, result.items[0].pack_count) == (
        300,
        "g",
        None,
    )
    assert result.warnings == ["broccoli: 300 g past niet in stuk; de gewone som staat op de lijst"]


def test_a_product_without_a_fact_is_summed_as_before():
    result = allocate(
        _demand({MON: {("spaghetti", "g"): 500}, TUE: {("spaghetti", "g"): 500}}),
        {"broccoli": BROCCOLI_350},
    )

    assert [(i.product, i.amount, i.unit, i.note) for i in result.items] == [
        ("spaghetti", 1000, "g", None)
    ]


def test_a_bundle_without_a_known_weight_says_how_many_are_in_one():
    bundle = _fact(pack_qty=6, pack_unit="stuk")
    result = allocate(_demand({MON: {("tomaat", "stuk"): 4}}), {"tomaat": bundle})

    assert (result.items[0].pack_count, result.items[0].note) == (1, "6 per verpakking")
    assert result.stock == [StockEntry("tomaat", MON, date(2026, 10, 1), 2, "stuk")]


def test_a_weight_pack_shows_its_pack_size_as_a_note():
    mince = ProductFact(product="gehakt", pack_qty=500, pack_unit="g")
    result = allocate(_demand({MON: {("gehakt", "g"): 700}}), {"gehakt": mince})

    assert (result.items[0].amount, result.items[0].unit, result.items[0].note) == (
        1000,
        "g",
        "2 x 500 g",
    )


def test_a_need_of_a_gram_still_buys_a_whole_pack():
    result = allocate(_demand({MON: {("broccoli", "g"): 1e-9}}), {"broccoli": BROCCOLI_350})

    assert result.items[0].pack_count == 1
    assert result.stock[0].qty == pytest.approx(350)


def test_a_need_that_is_exactly_one_pack_does_not_buy_two():
    # 0.35 * 1000 is 350.00000000000006, which must still be one pack
    result = allocate(_demand({MON: {("broccoli", "g"): 0.35 * 1000}}), {"broccoli": BROCCOLI_350})

    assert result.items[0].pack_count == 1
    assert result.stock == []  # the pack was used up, float dust aside


def test_the_result_does_not_depend_on_dict_order():
    forward = _demand({MON: {("broccoli", "g"): 300}, TUE: {("broccoli", "g"): 200}})
    backward = _demand({TUE: {("broccoli", "g"): 200}, MON: {("broccoli", "g"): 300}})

    assert allocate(forward, {"broccoli": BROCCOLI_350}) == allocate(
        backward, {"broccoli": BROCCOLI_350}
    )


def test_no_demand_buys_nothing():
    result = allocate({}, {"broccoli": BROCCOLI_350})
    assert result == Allocation()


def test_a_recipe_with_no_share_of_the_people_does_not_buy():
    result = allocate(_demand({MON: {("broccoli", "g"): 0}}), {"broccoli": BROCCOLI_350})
    assert result.items == []


def test_packs_for_is_never_fooled_by_float_noise():
    assert packs_for(350, 350) == 1
    assert packs_for(700, 350) == 2
    assert packs_for(351, 350) == 2
    assert packs_for(0, 350) == 0
    assert packs_for(1e-9, 350) == 1


def test_to_measure_refuses_what_it_cannot_convert():
    bundle = _fact(pack_qty=6, pack_unit="stuk")
    assert to_measure(3, "stuk", bundle) == 3
    assert to_measure(300, "g", bundle) is None
    # a 350 g pack is measured in grams, so a recipe in kg converts to grams
    assert to_measure(2, "kg", BROCCOLI_350) == 2000
    assert to_measure(2, "stuk", BROCCOLI_350) == 700


def test_last_weeks_remainder_keeps_from_the_day_the_pack_was_opened():
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g", shelf_life_days=30)}
    last_week = _demand({MON: {("broccoli", "g"): 900}, TUE: {("broccoli", "g"): 200}})

    stock = carried_stock([{"product": "broccoli", "unit": "g", "amount": 1400}], last_week, facts)

    # 4 packs were opened for a need of 1100 g, so 300 g is still in the house
    assert stock == [StockEntry("broccoli", MON, date(2026, 10, 28), 300, "g")]


def test_last_weeks_remainder_counts_for_this_week():
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g", shelf_life_days=30)}
    stock = carried_stock(
        [{"product": "broccoli", "unit": "g", "amount": 1050}],
        _demand({MON: {("broccoli", "g"): 700}}),
        facts,
    )

    result = allocate(_demand({MON: {("broccoli", "g"): 300}}), facts, carried=stock)

    assert result.items[0].pack_count == 0  # the 350 g left over covers it
    assert result.stock[0].qty == 50


def test_a_remainder_that_expired_before_this_week_is_not_carried():
    facts = {"broccoli": BROCCOLI_350}
    stock = carried_stock(
        [{"product": "broccoli", "unit": "g", "amount": 1050}],
        _demand({MON: {("broccoli", "g"): 700}}),
        facts,
    )
    assert stock[0].expires_on == THU  # three days after it was opened

    # a week later it is gone, so this week's need is bought whole
    result = allocate(_demand({NEXT_MON: {("broccoli", "g"): 300}}), facts, carried=stock)

    assert result.items[0].pack_count == 1
    assert [entry.available_from for entry in result.stock] == [NEXT_MON]


def test_a_product_bought_exactly_is_left_with_no_stock():
    facts = {"broccoli": _fact(pack_qty=1, unit_qty=350, unit_measure="g")}

    assert (
        carried_stock(
            [{"product": "broccoli", "unit": "g", "amount": 350}],
            _demand({MON: {("broccoli", "g"): 350}}),
            facts,
        )
        == []
    )


def test_the_demand_lands_on_the_day_it_is_eaten():
    # What the planner hands over: a recipe for four, cooked for six, and the
    # leftovers evening that eats the same thing the next day.
    recipe = Recipe.model_validate(
        {
            "slug": "stamppot",
            "name": "stamppot",
            "servings": 4,
            "ingredients": [{"product": "Broccoli", "amount": 300, "unit": "g"}],
        }
    )

    assert demand_by_day([(MON, recipe, 6), (TUE, recipe, 6)]) == {
        MON: {("broccoli", "g"): 450.0},
        TUE: {("broccoli", "g"): 450.0},
    }


def test_a_component_is_not_scaled_by_the_people_count():
    """A component is a purchase, not a portion: 1 bag stays 1 bag for any table.

    A recipe for four cooked for two halves its amounts; a component says what you
    buy, so what the dish asks for is what goes in the basket.
    """
    component = Recipe.model_validate(
        {
            "slug": "kipfilet",
            "name": "Kipfilet",
            "servings": 4,
            "scales_with_servings": False,
            "ingredients": [{"product": "Kipfilet", "amount": 600, "unit": "g"}],
        }
    )

    assert demand_by_day([(MON, component, 2)]) == {MON: {("kipfilet", "g"): 600.0}}
    assert demand_by_day([(MON, component, 8)]) == {MON: {("kipfilet", "g"): 600.0}}


def test_a_dish_option_holds_its_own_amount_of_a_shared_component():
    """The component stays 600 g; a dish may use it as 1 kg, and another as 300 g."""
    from groceries_assistant.models import ComposedDish

    dish = ComposedDish.model_validate(
        {
            "slug": "feest",
            "name": "Feest",
            "parts": [{"label": "Vlees", "options": [{"slug": "kipfilet", "amount": 1000}]}],
        }
    )
    component = Recipe.model_validate(
        {
            "slug": "kipfilet",
            "name": "Kipfilet",
            "servings": 4,
            "scales_with_servings": False,
            "ingredients": [{"product": "Kipfilet", "amount": 600, "unit": "g"}],
        }
    )

    as_the_dish_uses_it = dish.recipes({"kipfilet": component})["kipfilet"]
    assert as_the_dish_uses_it.ingredients[0].amount == 1000
    # the component itself is untouched
    assert component.ingredients[0].amount == 600


def test_bracketed_preparation_hints_move_to_the_note():
    """'kipfilet (in stukjes)' orders as 'kipfilet'; the hint becomes the note.

    The brackets are a preparation, not a product: without the split the text
    lands in the AH search and a fact on 'kipfilet' is never found.
    """
    from groceries_assistant.planner.shopping import aggregate_ingredients, order_product

    assert order_product("kipfilet (in stukjes)") == ("kipfilet", "in stukjes")
    assert order_product("fijne spaghetti (pak à 500 g)") == ("fijne spaghetti", "pak à 500 g")
    assert order_product("ui (gesnipperd) (vers)") == ("ui", "gesnipperd, vers")
    assert order_product("broccoli") == ("broccoli", None)
    # a name entirely inside brackets is not a product name at all; keep it
    assert order_product("(onbekend)") == ("(onbekend)", None)

    recipe = Recipe.model_validate(
        {
            "slug": "soep",
            "name": "soep",
            "servings": 4,
            "ingredients": [
                {"product": "kipfilet (in stukjes)", "amount": 300, "unit": "g"},
                {"product": "uien (gesnipperd)", "amount": 1, "unit": "stuk"},
            ],
        }
    )
    items, _ = aggregate_ingredients([(MON, recipe, 4)])
    by_product = {item.product: item for item in items}
    assert set(by_product) == {"kipfilet", "uien"}
    assert by_product["kipfilet"].note == "in stukjes"
    assert by_product["uien"].note == "gesnipperd"


def test_the_fact_lookup_hits_bracketed_names():
    """A fact declared on 'kipfilet' is found for 'kipfilet (in stukjes)'."""
    recipe = Recipe.model_validate(
        {
            "slug": "roerbak",
            "name": "roerbak",
            "servings": 4,
            "ingredients": [{"product": "kipfilet (in stukjes)", "amount": 300, "unit": "g"}],
        }
    )
    demand = demand_by_day([(MON, recipe, 4)])
    assert demand == {MON: {("kipfilet", "g"): 300.0}}


def test_allocate_keeps_the_bracket_note_without_a_fact():
    """A factless product's allocation line carries the preparation hint."""
    from groceries_assistant.planner.pantry import allocate

    allocation = allocate({MON: {("kipfilet", "g"): 300.0}}, {}, notes={"kipfilet": "in stukjes"})
    assert [(i.product, i.amount, i.note) for i in allocation.items] == [
        ("kipfilet", 300.0, "in stukjes")
    ]
