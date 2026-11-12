"""The bonus list cache and the recipe-ingredient matching behind it."""

from datetime import date

import pytest

from groceries_assistant.ah.bonus import BonusStore
from groceries_assistant.models import Ingredient
from groceries_assistant.planner.engine import bonus_score
from groceries_assistant.store.db import Store


@pytest.fixture
def store(tmp_path) -> BonusStore:
    return BonusStore(Store(tmp_path / "state.db"))


def test_bonus_points_are_a_capped_nudge():
    assert bonus_score(0) == 0.0
    assert bonus_score(1) == pytest.approx(0.5)
    assert bonus_score(2) == pytest.approx(1.0)
    assert bonus_score(10) == pytest.approx(1.5)  # capped at 3 ingredients


def test_missing_cache_means_no_bonus(store):
    assert store.products == []
    assert store.fetched_at is None
    assert store.count_for("pasta", [Ingredient(product="spaghetti", amount=500, unit="g")]) == 0


def test_save_and_load_round_trip(tmp_path):
    bonus = BonusStore(Store(tmp_path / "state.db"))
    bonus.save([(514008, "AH Spaghetti 500 g"), (1, "Hak tomatenblokjes")], date(2026, 11, 5))

    reloaded = BonusStore(Store(tmp_path / "state.db"))
    reloaded.load()
    assert reloaded.products == [(514008, "AH Spaghetti 500 g"), (1, "Hak tomatenblokjes")]
    assert reloaded.fetched_at == date(2026, 11, 5)


def test_broken_cache_never_crashes(tmp_path):
    """Een kapotte/half-geldige oude bonus.yaml breekt de migratie niet: de
    tabel blijft (of wordt) gewoon gevuld met wat leesbaar is."""
    path = tmp_path / "bonus.yaml"
    path.write_text("{onzin: [", encoding="utf-8")
    bonus = BonusStore(Store(tmp_path / "state.db"))
    assert bonus.products == []
    path.write_text(
        "fetched_at: niet-een-datum\nproducts: [{webshop_id: 1, title: x}]", encoding="utf-8"
    )
    fresh = BonusStore(Store(tmp_path / "state.db"))
    assert fresh.products == [(1, "x")]
    assert fresh.fetched_at is None


def test_ingredient_matching_is_whole_word_containment(store, tmp_path_factory):
    store.save([(1, "Hak tomatenblokjes uitgeperst 400 g")], date(2026, 11, 5))
    # containment: the ingredient is a whole word inside the bonus title
    assert store.is_in_bonus("tomatenblokjes") is True
    assert store.is_in_bonus("Tomatenblokjes") is True  # normalization lowercases
    # 'ui' is too short for containment: 'uitgeperst' must not match it
    assert store.is_in_bonus("ui") is False
    # exact short names still match on normalized equality
    short = BonusStore(Store(tmp_path_factory.mktemp("bonus") / "state.db"))
    short.save([(2, "Ui")], date(2026, 11, 5))
    assert short.is_in_bonus("ui") is True
    # and something unrelated matches nothing
    assert store.is_in_bonus("chocolade") is False


def test_count_for_caches_per_slug_until_the_list_changes(store):
    store.save([(1, "Hak tomatenblokjes")], date(2026, 11, 5))
    ingredients = [Ingredient(product="tomatenblokjes", amount=400, unit="g")]

    assert store.count_for("pasta", ingredients) == 1
    assert store.count_for("pasta", ingredients) == 1  # cached

    store.save([], date(2026, 11, 6))  # a refresh resets the cache
    assert store.count_for("pasta", ingredients) == 0
