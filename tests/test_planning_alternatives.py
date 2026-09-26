from datetime import date

from groceries_assistant.models import Recipe
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
    assert [r.slug for _, r in scored] == ["pasta"]


def test_excludes_current_slug():
    recipes = [_recipe("pasta"), _recipe("curry")]
    scored = alternatives(recipes, _prefs(), _evening(), exclude_slug="pasta")
    assert [r.slug for _, r in scored] == ["curry"]


def test_orders_by_score_then_slug():
    recipes = [_recipe("bami"), _recipe("pasta"), _recipe("curry")]
    prefs = _prefs(
        {
            "pasta": {"kid1": "love"},  # kid group 2.0 * love 3.0 = +6
            "curry": {"kid1": "ok"},  # +2
        }
    )
    scored = alternatives(recipes, prefs, _evening())
    assert [r.slug for _, r in scored] == ["pasta", "curry", "bami"]


def test_limit_caps_result():
    recipes = [_recipe(f"r{i:02}") for i in range(5)]
    scored = alternatives(recipes, _prefs(), _evening(), limit=3)
    assert [r.slug for _, r in scored] == ["r00", "r01", "r02"]
