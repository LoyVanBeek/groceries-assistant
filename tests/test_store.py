from datetime import date, timedelta

import pytest

from groceries_assistant.ah.deeplink import (
    build_add_multiple_url,
    chunk_items,
    generate_qr_svg,
    parse_product_ref,
    product_url,
)
from groceries_assistant.models import ProductFact
from groceries_assistant.store.db import DEFAULT_TEMPLATE, PlanEntryRow, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_default_template_populated(store):
    template = store.week_template()
    assert set(template) == set(range(7))
    assert template[0][2] is True  # Monday allows leftovers
    assert template[0][1] < template[6][1]  # Monday shorter than Sunday


def test_people_and_verdicts_round_trip(store):
    store.upsert_person("kid1", "Kees", "kid", 1.5)
    store.set_verdict("pasta", "kid1", "love")
    assert store.verdicts()["pasta"]["kid1"] == "love"
    people = store.people()
    assert people[0]["role"] == "kid"


def test_verdict_overwrite(store):
    store.set_verdict("pasta", "kid1", "ok")
    store.set_verdict("pasta", "kid1", "love")
    assert store.verdicts()["pasta"]["kid1"] == "love"


def test_staples_toggle(store):
    store.set_staple("rijst", True)
    store.set_staple("pasta", True)
    store.set_staple("rijst", False)
    assert store.staples() == ["pasta"]


def test_approve_plan_and_history(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(
        day,
        [
            PlanEntryRow(day=day, recipe_slug="stoofpot"),
            PlanEntryRow(
                day=day.replace(day=day.day + 1),
                kind="leftovers",
                source_slug="stoofpot",
            ),
        ],
    )
    entries = store.plan_entries(plan_id)
    assert entries[0].kind == "cook"
    assert entries[1].kind == "leftovers"
    assert entries[1].recipe_slug is None
    assert store.history() == {"stoofpot": day}
    latest = store.latest_approved()
    assert latest is not None and latest[0] == plan_id


def test_week_start_weekday_defaults_to_tuesday_and_round_trips(store):
    assert store.week_start_weekday() == 1
    store.set_week_start_weekday(3)
    assert store.week_start_weekday() == 3
    with pytest.raises(ValueError):
        store.set_week_start_weekday(7)


def test_extend_proposal_appends_days_and_picks(store):
    start = date(2026, 10, 6)
    plan_id = store.save_proposal(
        start, [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
    )
    next_week_start = start + timedelta(days=7)
    store.extend_proposal(
        plan_id,
        [PlanEntryRow(day=next_week_start + timedelta(days=i)) for i in range(7)],
        picks={next_week_start: {"groente": "brocolli"}},
    )
    days = [e.day for e in store.plan_entries(plan_id)]
    assert len(days) == 14
    assert days[7] == next_week_start
    assert store.plan_picks(plan_id) == {next_week_start: {"groente": "brocolli"}}


def test_approval_and_order_extents_round_trip(store):
    start = date(2026, 10, 6)
    rows = [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
    plan_id = store.save_proposal(start, rows)
    assert store.approved_through(plan_id) is None
    store.set_approved_through(plan_id, start + timedelta(days=3))
    assert store.approved_through(plan_id) == start + timedelta(days=3)
    store.set_ordered_through(plan_id, start + timedelta(days=3))
    assert store.ordered_through(plan_id) == start + timedelta(days=3)


def test_shift_proposal_moves_entries_and_picks_together(store):
    start = date(2026, 10, 5)  # Monday
    plan_id = store.save_proposal(
        start,
        [
            PlanEntryRow(day=start, recipe_slug="pasta"),
            PlanEntryRow(day=start + timedelta(days=1), recipe_slug="dhaal"),
        ],
        picks={start: {"groente": "brocolli"}},
    )
    store.shift_proposal(plan_id, 2)
    assert store.load_proposal()[1] == date(2026, 10, 7)
    assert [e.day for e in store.plan_entries(plan_id)] == [
        date(2026, 10, 7),
        date(2026, 10, 8),
    ]
    assert store.plan_picks(plan_id) == {date(2026, 10, 7): {"groente": "brocolli"}}


def test_shift_proposal_by_a_week_keeps_weekdays(store):
    start = date(2026, 10, 5)
    plan_id = store.save_proposal(
        start, [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
    )
    store.shift_proposal(plan_id, 7)
    assert [e.day.weekday() for e in store.plan_entries(plan_id)] == [0, 1, 2, 3, 4, 5, 6]


def test_shift_proposal_forward_only_and_idempotent_after_re_anchor(store):
    """The /plan re-anchor formula (0..6 forward) can never move the plan back."""
    start = date(2026, 10, 5)  # Monday
    plan_id = store.save_proposal(start, [PlanEntryRow(day=start, recipe_slug="pasta")])
    for _ in range(2):
        shift = (3 - store.load_proposal()[1].weekday()) % 7  # anchor = Wednesday
        if shift:
            store.shift_proposal(plan_id, shift)
    assert store.load_proposal()[1].weekday() == 3
    assert store.plan_entries(plan_id)[0].day.weekday() == 3
    assert store.plan_entries(plan_id)[0].day == date(2026, 10, 8)


def test_the_parts_of_a_planned_dish_count_as_history_too(store):
    """A component cooked as part of a dish was still cooked last week."""
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(day, [PlanEntryRow(day=day, recipe_slug="agv")])
    store.set_pick(plan_id, day, "groente", "boerenkool")
    store.set_pick(plan_id, day, "vlees", "kip")

    assert store.history() == {"agv": day, "boerenkool": day, "kip": day}


def test_a_pick_takes_the_last_day_a_component_was_planned(store):
    first = date(2026, 9, 28)
    second = date(2026, 10, 5)
    first_plan = store.approve_plan(first, [PlanEntryRow(day=first, recipe_slug="boerenkool")])
    store.set_pick(first_plan, first, "groente", "sperziebonen")
    second_plan = store.approve_plan(second, [PlanEntryRow(day=second, recipe_slug="agv")])
    store.set_pick(second_plan, second, "groente", "sperziebonen")

    assert store.history()["sperziebonen"] == second


def test_chunk_items_splits():
    items = [(i, 1) for i in range(60)]
    chunks = chunk_items(items, max_items=25)
    assert [len(c) for c in chunks] == [25, 25, 10]


# --- composed-dish picks ------------------------------------------------------


def test_picks_round_trip_per_day_and_part(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(
        day, [PlanEntryRow(day=day, recipe_slug="aardappel-groente-vlees")]
    )

    store.set_pick(plan_id, day, "groente", "boerenkool")
    store.set_pick(plan_id, day, "vlees", "kip")

    assert store.plan_picks(plan_id) == {day: {"groente": "boerenkool", "vlees": "kip"}}


def test_setting_a_pick_twice_overwrites_it(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(day, [PlanEntryRow(day=day, recipe_slug="agv")])

    store.set_pick(plan_id, day, "groente", "boerenkool")
    store.set_pick(plan_id, day, "groente", "sperziebonen")

    assert store.plan_picks(plan_id) == {day: {"groente": "sperziebonen"}}


def test_an_empty_pick_clears_the_part(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(day, [PlanEntryRow(day=day, recipe_slug="agv")])
    store.set_pick(plan_id, day, "groente", "boerenkool")

    store.set_pick(plan_id, day, "groente", "")

    assert store.plan_picks(plan_id) == {}


def test_clear_pick_and_clear_day_picks(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(
        day,
        [
            PlanEntryRow(day=day, recipe_slug="agv"),
            PlanEntryRow(day=date(2026, 9, 29), recipe_slug="agv"),
        ],
    )
    store.set_picks(plan_id, day, {"groente": "boerenkool", "vlees": "kip"})
    store.set_pick(plan_id, date(2026, 9, 29), "groente", "boerenkool")

    store.clear_pick(plan_id, day, "vlees")
    assert store.plan_picks(plan_id)[day] == {"groente": "boerenkool"}

    store.clear_day_picks(plan_id, day)
    assert day not in store.plan_picks(plan_id)
    assert date(2026, 9, 29) in store.plan_picks(plan_id)  # other evenings untouched


def test_set_picks_replaces_the_whole_evening(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(day, [PlanEntryRow(day=day, recipe_slug="agv")])
    store.set_picks(plan_id, day, {"groente": "boerenkool", "vlees": "kip"})

    store.set_picks(plan_id, day, {"groente": "sperziebonen"})

    assert store.plan_picks(plan_id) == {day: {"groente": "sperziebonen"}}


def test_replacing_a_proposal_cascades_its_picks_away(store):
    day = date(2026, 9, 28)
    first = store.save_proposal(day, [PlanEntryRow(day=day, recipe_slug="agv")])
    store.set_pick(first, day, "groente", "boerenkool")

    second = store.save_proposal(day, [PlanEntryRow(day=day, recipe_slug="agv")])

    assert store.plan_picks(first) == {}
    assert store.plan_picks(second) == {}


def test_a_proposal_can_be_saved_with_its_picks(store):
    """The planner's own parts land in the store, ready for the week view."""
    day = date(2026, 9, 28)
    plan_id = store.save_proposal(
        day,
        [PlanEntryRow(day=day, recipe_slug="agv")],
        {day: {"groente": "boerenkool", "vlees": "kip"}},
    )

    assert store.plan_picks(plan_id) == {day: {"groente": "boerenkool", "vlees": "kip"}}


def test_a_new_proposal_takes_the_picks_with_it(store):
    day = date(2026, 9, 28)
    store.save_proposal(
        day, [PlanEntryRow(day=day, recipe_slug="agv")], {day: {"groente": "boerenkool"}}
    )

    second = store.save_proposal(
        day, [PlanEntryRow(day=day, recipe_slug="agv")], {day: {"groente": "sperziebonen"}}
    )

    assert store.plan_picks(second) == {day: {"groente": "sperziebonen"}}


def test_an_empty_part_is_left_open_in_a_saved_proposal(store):
    day = date(2026, 9, 28)
    plan_id = store.save_proposal(
        day, [PlanEntryRow(day=day, recipe_slug="agv")], {day: {"groente": "", "vlees": "kip"}}
    )

    assert store.plan_picks(plan_id) == {day: {"vlees": "kip"}}


def test_updating_the_entries_of_a_proposal_keeps_its_picks(store):
    """Approving a week rewrites the entry rows; the picks must survive it."""
    day = date(2026, 9, 28)
    plan_id = store.save_proposal(day, [PlanEntryRow(day=day, recipe_slug="agv", people=3)])
    store.set_pick(plan_id, day, "groente", "boerenkool")

    store.update_proposal_rows(plan_id, [PlanEntryRow(day=day, recipe_slug="agv", people=4)])

    assert store.plan_picks(plan_id) == {day: {"groente": "boerenkool"}}


def test_renaming_a_recipe_repoints_its_picks(store):
    day = date(2026, 9, 28)
    plan_id = store.approve_plan(day, [PlanEntryRow(day=day, recipe_slug="agv")])
    store.set_pick(plan_id, day, "groente", "boerenkool")

    store.rename_recipe("boerenkool", "boerenkool-met-knoflook")

    assert store.plan_picks(plan_id) == {day: {"groente": "boerenkool-met-knoflook"}}


def test_build_add_multiple_url():
    url = build_add_multiple_url([(609371, 1), (61010, 2)])
    assert url == "https://www.ah.nl/mijnlijst/add-multiple?p=609371%3A1&p=61010%3A2"


def test_a_product_fact_round_trips(store):
    store.set_product_fact(
        ProductFact(
            product="broccoli", pack_qty=1, pack_unit="stuk", unit_qty=350, unit_measure="g"
        )
    )

    fact = store.product_fact("BROCCOLI")
    assert fact is not None
    assert fact.pack_contents == 350
    assert fact.shelf_life_days == 3  # the default: fresh produce keeps a few days


def test_a_product_without_a_fact_keeps_its_plain_need(store):
    assert store.product_facts() == {}
    assert store.product_fact("broccoli") is None


def test_redeclaring_a_fact_replaces_it_instead_of_shadowing_it(store):
    store.set_product_fact(ProductFact(product="broccoli", pack_qty=1, pack_unit="stuk"))
    store.set_product_fact(
        ProductFact(product="Broccoli", pack_qty=2, pack_unit="stuk", shelf_life_days=5)
    )

    facts = store.product_facts()
    assert len(facts) == 1
    assert facts["broccoli"].pack_qty == 2
    assert facts["broccoli"].shelf_life_days == 5


def test_forgetting_a_fact_puts_the_product_back_on_its_own(store):
    store.set_product_fact(ProductFact(product="broccoli", pack_qty=1, pack_unit="stuk"))

    store.delete_product_fact("BROCCOLI")

    assert store.product_facts() == {}


def test_build_add_multiple_url_min_quantity():
    url = build_add_multiple_url([(123, 0)])
    assert "123%3A1" in url


def test_generate_qr_svg():
    svg = generate_qr_svg("https://example.com")
    assert "<svg" in svg


def test_store_defaults_match_expected_shape():
    assert DEFAULT_TEMPLATE[0][1] == 25


def test_product_url_uses_wi_prefix():
    assert product_url(4137) == "https://www.ah.nl/producten/product/wi4137"


def test_parse_product_ref_accepts_ids_and_urls():
    assert parse_product_ref("4137") == (4137, "")
    assert parse_product_ref("wi4137") == (4137, "")
    assert parse_product_ref("https://www.ah.nl/producten/product/wi58169") == (58169, "")
    assert parse_product_ref(
        "https://www.ah.nl/producten/product/wi58169/ah-volkoren-spaghetti"
    ) == (58169, "volkoren spaghetti")
    # the old wa-style links still parse (id space is the same)
    assert parse_product_ref("https://www.ah.nl/producten/product/wa4137/ah-tomaten") == (
        4137,
        "tomaten",
    )
    assert parse_product_ref("geen id") is None
    assert parse_product_ref("") is None
