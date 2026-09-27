from datetime import date

import pytest

from groceries_assistant.ah.deeplink import (
    build_add_multiple_url,
    chunk_items,
    generate_qr_svg,
    parse_product_ref,
    product_url,
)
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


def test_chunk_items_splits():
    items = [(i, 1) for i in range(60)]
    chunks = chunk_items(items, max_items=25)
    assert [len(c) for c in chunks] == [25, 25, 10]


def test_build_add_multiple_url():
    url = build_add_multiple_url([(609371, 1), (61010, 2)])
    assert url == "https://www.ah.nl/mijnlijst/add-multiple?p=609371%3A1&p=61010%3A2"


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
