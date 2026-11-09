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
from groceries_assistant.store.barcodes import BarcodeStore, valid_ean
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


def test_approval_flags_and_ordered_extent_round_trip(store):
    start = date(2026, 10, 6)
    rows = [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
    plan_id = store.save_proposal(start, rows)
    assert store.plan_approvals(plan_id) == set()
    assert store.plan_ordered(plan_id) == set()
    store.set_day_approved(plan_id, start + timedelta(days=3), True)
    assert store.plan_approvals(plan_id) == {start + timedelta(days=3)}
    store.set_day_approved(plan_id, start + timedelta(days=5), True)
    assert store.plan_approvals(plan_id) == {
        start + timedelta(days=3),
        start + timedelta(days=5),
    }
    store.set_day_approved(plan_id, start + timedelta(days=3), False)
    assert store.plan_approvals(plan_id) == {start + timedelta(days=5)}
    store.set_day_ordered(plan_id, start + timedelta(days=5), True)
    assert store.plan_ordered(plan_id) == {start + timedelta(days=5)}


def test_swap_days_exchanges_entries_picks_approval_and_confirmation(store):
    """Ruil van dag: dishes, part picks and per-day flags move as one block,
    so a confirmed recipe keeps its 'besteld' status on the other date."""
    start = date(2026, 10, 6)
    a = start + timedelta(days=1)
    b = start + timedelta(days=3)
    plan_id = store.save_proposal(
        start,
        [
            PlanEntryRow(day=start),
            PlanEntryRow(day=a, kind="cook", recipe_slug="pasta", people=4),
            PlanEntryRow(day=start + timedelta(days=2)),
            PlanEntryRow(day=b, kind="no_cook", people=0),
            PlanEntryRow(day=start + timedelta(days=4)),
            PlanEntryRow(day=start + timedelta(days=5)),
            PlanEntryRow(day=start + timedelta(days=6)),
        ],
        picks={a: {"groente": "brocolli"}},
    )
    store.set_day_approved(plan_id, a, True)
    store.set_day_ordered(plan_id, a, True)
    store.swap_days(plan_id, a, b)
    by_day = {e.day: e for e in store.plan_entries(plan_id)}
    assert by_day[a].kind == "no_cook"
    assert by_day[a].people == 0
    assert by_day[b].kind == "cook"
    assert by_day[b].recipe_slug == "pasta"
    # the picks and the confirmed/approved flags rode along with the recipe
    assert store.plan_picks(plan_id) == {b: {"groente": "brocolli"}}
    assert store.plan_approvals(plan_id) == {b}
    assert store.plan_ordered(plan_id) == {b}
    # swapping back restores the original arrangement
    store.swap_days(plan_id, b, a)
    assert store.plan_picks(plan_id) == {a: {"groente": "brocolli"}}
    assert store.plan_approvals(plan_id) == {a}
    assert store.plan_ordered(plan_id) == {a}


def test_swap_days_rejects_unknown_days(store):
    start = date(2026, 10, 6)
    plan_id = store.save_proposal(
        start, [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
    )
    stranger = start + timedelta(days=50)
    try:
        store.swap_days(plan_id, start, stranger)
    except KeyError:
        pass
    else:
        raise AssertionError("swap_days should reject a day outside the plan")
    # a same-day swap is a harmless no-op
    store.swap_days(plan_id, start, start)
    assert len(store.plan_entries(plan_id)) == 7


def test_barcode_store_round_trip(tmp_path):
    """data/barcodes.yaml: an EAN maps to the product name it was scanned
    with; the file is hand-editable and survives a reload."""
    barcodes = BarcodeStore(tmp_path / "barcodes.yaml")
    barcodes.load()
    assert barcodes.get("8710600988068") is None

    barcodes.put("8710600988068", "Hak Tomatenblokjes")
    reloaded = BarcodeStore(tmp_path / "barcodes.yaml")
    reloaded.load()
    assert reloaded.get("8710600988068") == "Hak Tomatenblokjes"

    with pytest.raises(ValueError):
        barcodes.put("8710600988068", "   ")


def test_valid_ean():
    assert valid_ean("8710600988068")  # EAN-13
    assert valid_ean("54490000")  # EAN-8
    assert not valid_ean("8710 6009 8806 8")  # spaces are not typed, they are scanned
    assert not valid_ean("geen cijfers")
