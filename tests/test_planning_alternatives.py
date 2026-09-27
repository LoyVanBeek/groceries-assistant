from datetime import date

import pytest

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import Person, Preferences
from groceries_assistant.services.planning import alternatives


def _evening(free_minutes=120):
    return EveningAvailability(date=date(2026, 9, 8), free_minutes=free_minutes)


def _recipe(slug, minutes=30):
    return Recipe.model_validate(
        {
            "slug": slug,
            "name": slug,
            "servings": 4,
            "time": {"cook": minutes},
            "ingredients": [{"product": "x", "amount": 1}],
        }
    )


def _prefs(verdicts=None):
    return Preferences(people=[Person(id="kid1", role="kid")], verdicts=verdicts or {})


def test_filters_over_budget_and_never():
    recipes = [_recipe("pasta", 20), _recipe("stoofpot", 120), _recipe("spruitjes", 20)]
    prefs = _prefs({"spruitjes": {"kid1": "never"}})
    scored = alternatives(recipes, prefs, _evening(45))
    assert [r.slug for r in scored] == ["pasta"]


def test_excludes_current_slug():
    recipes = [_recipe("pasta"), _recipe("curry")]
    scored = alternatives(recipes, _prefs(), _evening(), exclude_slug="pasta")
    assert [r.slug for r in scored] == ["curry"]


def test_orders_by_score_then_slug():
    recipes = [_recipe("bami"), _recipe("pasta"), _recipe("curry")]
    prefs = _prefs(
        {
            "pasta": {"kid1": "love"},  # kid group 2.0 * love 3.0 = +6
            "curry": {"kid1": "ok"},  # +2
        }
    )
    scored = alternatives(recipes, prefs, _evening())
    assert [r.slug for r in scored] == ["pasta", "curry", "bami"]


def test_limit_caps_result():
    recipes = [_recipe(f"r{i:02}") for i in range(5)]
    scored = alternatives(recipes, _prefs(), _evening(), limit=3)
    assert [r.slug for r in scored] == ["r00", "r01", "r02"]


# --- composed dishes ----------------------------------------------------------


def _dish(slug="aardappel-groente-vlees", **parts):
    """A dish with one option per part, keyed by the part's label."""
    return ComposedDish.model_validate(
        {
            "slug": slug,
            "name": slug,
            "parts": [
                {"label": label.capitalize(), "options": [option]}
                for label, option in parts.items()
            ],
        }
    )


def test_a_dish_is_offered_above_the_plain_recipes():
    recipes = [_recipe("pasta"), _recipe("curry"), _recipe("boerenkool"), _recipe("kip")]
    dish = _dish(groente="boerenkool", vlees="kip")
    prefs = _prefs({"pasta": {"kid1": "love"}})

    scored = alternatives(recipes, prefs, _evening(), dishes=[dish])

    assert scored[0].slug == dish.slug
    assert scored[0].is_dish
    assert scored[0].part_labels == ("Groente", "Vlees")
    assert scored[0].name == dish.name
    assert not any(r.is_dish for r in scored[1:])


def test_a_dish_that_does_not_fit_the_evening_is_not_offered():
    """The longest part decides, so a 90-minute part rules the dish out."""
    dish = _dish(groente="boerenkool", vlees="kip")
    recipes = [_recipe("pasta"), _recipe("boerenkool"), _recipe("kip", 90)]

    scored = alternatives(recipes, _prefs(), _evening(45), dishes=[dish])

    assert dish.slug not in [r.slug for r in scored]
    assert "pasta" in [r.slug for r in scored]


def test_a_dish_marked_never_is_not_offered():
    dish = _dish(groente="boerenkool")
    recipes = [_recipe("boerenkool"), _recipe("pasta")]
    prefs = _prefs({dish.slug: {"kid1": "never"}})

    scored = alternatives(recipes, prefs, _evening(), dishes=[dish])

    assert [r.slug for r in scored] == ["boerenkool", "pasta"]  # the dish itself is gone


def test_a_dish_whose_part_recipe_was_deleted_is_not_offered():
    dish = _dish(groente="weg-recept")

    scored = alternatives([_recipe("pasta")], _prefs(), _evening(), dishes=[dish])

    assert [r.slug for r in scored] == ["pasta"]


def test_the_current_dish_is_excluded_from_the_picker():
    dish = _dish(groente="boerenkool")
    recipes = [_recipe("pasta")]

    scored = alternatives(recipes, _prefs(), _evening(), exclude_slug=dish.slug, dishes=[dish])

    assert [r.slug for r in scored] == ["pasta"]


def test_a_dish_scores_as_the_mean_of_its_parts():
    """love(+6) and dislike(-8) average to -1; the dish is offered, just ranked below."""
    dish = _dish(groente="boerenkool", vlees="kip")
    recipes = [_recipe("boerenkool"), _recipe("kip"), _recipe("pasta")]
    prefs = _prefs(
        {
            "boerenkool": {"kid1": "love"},
            "kip": {"kid1": "dislike"},
            "pasta": {"kid1": "ok"},
        }
    )

    scored = alternatives(recipes, prefs, _evening(), dishes=[dish])

    dish_row = next(r for r in scored if r.is_dish)
    assert dish_row.score == pytest.approx(-1.0)
    assert next(r for r in scored if r.slug == "pasta").score == pytest.approx(2.0)


def test_a_verdict_on_the_dish_itself_replaces_the_mean():
    dish = _dish(groente="boerenkool")
    recipes = [_recipe("boerenkool"), _recipe("pasta")]
    prefs = _prefs({"boerenkool": {"kid1": "never"}, dish.slug: {"kid1": "love"}})

    scored = alternatives(recipes, prefs, _evening(), dishes=[dish])

    assert scored[0].slug == "pasta"
    assert dish.slug not in [r.slug for r in scored]
