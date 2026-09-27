from datetime import date

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.planner.engine import plan_week
from groceries_assistant.preferences import Person, Preferences

D1 = date(2026, 9, 8)
D2 = date(2026, 9, 9)
D3 = date(2026, 9, 10)


def _evening(day, free_minutes, allow_leftovers=False):
    return EveningAvailability(date=day, free_minutes=free_minutes, allow_leftovers=allow_leftovers)


def _recipe(slug, minutes=30, name=None):
    return Recipe.model_validate(
        {
            "slug": slug,
            "name": name or slug,
            "servings": 4,
            "time": {"cook": minutes},
            "ingredients": [{"product": "x", "amount": 1}],
        }
    )


def _prefs(verdicts=None, people=None):
    return Preferences(
        people=people
        or [
            Person(id="kid1", role="kid"),
            Person(id="kid2", role="kid"),
            Person(id="adult1", role="adult"),
        ],
        verdicts=verdicts or {},
    )


def test_picks_higher_scored_recipe_first():
    recipes = [_recipe("pasta"), _recipe("spruitjes", minutes=60)]
    prefs = _prefs(
        {
            "pasta": {"kid1": "love", "kid2": "love"},
            "spruitjes": {"adult1": "love"},
        }
    )
    plan = plan_week(recipes, prefs, [_evening(D1, 120), _evening(D2, 120)])
    assert plan.entries[0].recipe.slug == "pasta"  # kid love (2 * 2.0 * 3 = +12)
    assert plan.entries[1].recipe.slug == "spruitjes"  # adult love (+3)
    assert plan.entries[0].score == 12.0


def test_never_verdict_excludes_recipe():
    recipes = [_recipe("spruitjes"), _recipe("pasta")]
    prefs = _prefs({"spruitjes": {"kid1": "never", "adult1": "love"}})
    plan = plan_week(recipes, prefs, [_evening(D1, 120)])
    assert plan.entries[0].recipe.slug == "pasta"  # spruitjes excluded despite adult love


def test_dislike_slots_limit():
    recipes = [
        _recipe("spruitjes"),
        _recipe("pasta"),
        _recipe("soep"),
    ]
    prefs = _prefs(
        {
            "spruitjes": {"kid1": "dislike", "adult1": "love"},
            "pasta": {"kid2": "dislike", "adult1": "love"},
        }
    )
    evenings = [_evening(D1, 120), _evening(D2, 120), _evening(D3, 120)]
    plan = plan_week(recipes, prefs, evenings)

    slugs = [e.recipe.slug for e in plan.entries]
    # dislike-cap of 1: only one of the two disliked recipes may appear;
    # the third evening must use the neutral recipe.
    disliked_planned = [s for s in slugs if s in ("spruitjes", "pasta")]
    assert len(disliked_planned) == 1
    assert "soep" in slugs


def test_time_constraint_excludes_long_recipe():
    recipes = [_recipe("stoofpot", minutes=120), _recipe("roerbak", minutes=20)]
    evenings = [_evening(D1, 45)]  # quick evening
    plan = plan_week(recipes, _prefs(), evenings)
    assert plan.entries[0].recipe.slug == "roerbak"


def test_fallback_plans_shortest_when_nothing_fits():
    recipes = [_recipe("stoofpot", minutes=120), _recipe("pasta", minutes=45)]
    evenings = [_evening(D1, 30)]  # too short for both
    plan = plan_week(recipes, _prefs(), evenings)
    assert plan.entries[0].recipe.slug == "pasta"  # the shortest recipe
    assert any("over budget" in w for w in plan.warnings)


def test_repeat_penalty_from_history():
    recipes = [_recipe("pasta"), _recipe("pasta-variante")]
    prefs = _prefs(
        {
            "pasta": {"kid1": "love"},  # +6, minus repeat penalty 6 -> 0
            "pasta-variante": {"adult1": "ok"},  # +1
        }
    )
    history = {"pasta": date(2026, 9, 2)}  # 6 days before D1
    plan = plan_week(recipes, prefs, [_evening(D1, 120)], history=history)
    assert plan.entries[0].recipe.slug == "pasta-variante"
    assert plan.entries[0].score == 1.0


def test_old_history_has_no_penalty():
    recipes = [_recipe("pasta"), _recipe("pasta-variante")]
    prefs = _prefs({"pasta": {"kid1": "love"}})
    history = {"pasta": date(2026, 8, 1)}  # > 10 days before D1
    plan = plan_week(recipes, prefs, [_evening(D1, 120)], history=history)
    assert plan.entries[0].recipe.slug == "pasta"


def test_no_same_recipe_twice_within_week():
    recipes = [_recipe("pasta")]
    evenings = [_evening(D1, 120), _evening(D2, 120)]
    plan = plan_week(recipes, _prefs(), evenings)
    # reuse is allowed as fallback but penalized; with a single recipe it repeats
    assert [e.recipe.slug for e in plan.entries] == ["pasta", "pasta"]
    assert plan.entries[1].score == -4.0  # REUSE_PENALTY applied


def test_tiebreak_prefers_shorter_cook_time():
    recipes = [_recipe("stoofpot", minutes=60), _recipe("roerbak", minutes=20)]
    plan = plan_week(recipes, _prefs(), [_evening(D1, 120)])
    assert plan.entries[0].recipe.slug == "roerbak"


def test_tiebreak_slug_when_same_score_and_time():
    recipes = [_recipe("bami", minutes=20), _recipe("pasta", minutes=20)]
    plan = plan_week(recipes, _prefs(), [_evening(D1, 120)])
    assert plan.entries[0].recipe.slug == "bami"


def test_custom_dislike_slots_allows_second_disliked_evening():
    recipes = [_recipe("spruitjes"), _recipe("soep"), _recipe("pasta")]
    prefs = _prefs({"spruitjes": {"kid1": "dislike"}, "soep": {"kid1": "dislike"}})
    evenings = [_evening(D1, 120), _evening(D2, 120), _evening(D3, 120)]
    plan = plan_week(recipes, prefs, evenings, dislike_slots=2)
    slugs = [e.recipe.slug for e in plan.entries]
    # twice a recipe someone dislikes may appear with dislike_slots=2
    assert sum(s in ("spruitjes", "soep") for s in slugs) == 2


def test_reuse_fallback_when_only_fit_is_already_planned():
    recipes = [_recipe("stoofpot", minutes=120), _recipe("pasta", minutes=20)]
    prefs = _prefs({"pasta": {"adult1": "love"}})
    evenings = [_evening(D1, 45), _evening(D2, 45)]
    plan = plan_week(recipes, prefs, evenings)
    # stoofpot never fits the 45-min evenings: pasta is reused, penalized to -1
    assert [e.recipe.slug for e in plan.entries] == ["pasta", "pasta"]
    assert plan.entries[1].score == -1.0  # love (+3) - REUSE_PENALTY (-4)


def test_no_reuse_when_unplanned_recipe_fits():
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = _prefs({"pasta": {"adult1": "love"}})
    plan = plan_week(recipes, prefs, [_evening(D1, 120), _evening(D2, 120)])
    assert [e.recipe.slug for e in plan.entries] == ["pasta", "curry"]


def test_reason_is_explainable():
    recipes = [_recipe("pasta")]
    prefs = _prefs({"pasta": {"kid1": "love"}})
    reason = plan_week(recipes, prefs, [_evening(D1, 120)]).entries[0].reason
    assert "kid1: love (+6)" in reason  # kid group 2.0 * weight 1.0 * love 3.0
    assert "30/120 min" in reason
    assert "totaal +6" in reason


def test_repeat_window_boundary_exact_21_days():
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = _prefs({"pasta": {"kid1": "love"}})  # +6
    history = {"pasta": date(2026, 8, 18)}  # D1 - 21 days: penalty applies
    plan = plan_week(recipes, prefs, [_evening(D1, 120)], history=history)
    # pasta drops to 0, curry wins the 0-0 slug tie-break
    assert plan.entries[0].recipe.slug == "curry"


def test_repeat_window_boundary_22_days_no_penalty():
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = _prefs({"pasta": {"kid1": "love"}})  # +6
    history = {"pasta": date(2026, 8, 17)}  # D1 - 22 days: outside the window
    plan = plan_week(recipes, prefs, [_evening(D1, 120)], history=history)
    assert plan.entries[0].recipe.slug == "pasta"


def test_protein_repeat_penalty_next_evening():
    def _with_protein(slug, protein, minutes=30):
        return Recipe.model_validate(
            {
                "slug": slug,
                "name": slug,
                "servings": 4,
                "protein": protein,
                "time": {"cook": minutes},
                "ingredients": [{"product": "x", "amount": 1}],
            }
        )

    recipes = [
        _with_protein("pasta-kip", "kip"),
        _with_protein("kip-rijst", "kip"),
        _with_protein("vis", "vis"),
    ]
    prefs = _prefs({"pasta-kip": {"kid1": "love"}, "kip-rijst": {"adult1": "ok"}})
    plan = plan_week(recipes, prefs, [_evening(D1, 120), _evening(D2, 120)])
    # D1: pasta-kip wins on kid love (+6). D2: kip-rijst scores 1 - 5 (protein
    # penalty) = -4, vis scores 0 -> the neutral fish dish wins instead.
    assert [e.recipe.slug for e in plan.entries] == ["pasta-kip", "vis"]


def test_prep_ahead_fits_cramped_evening():
    # 60-min dish of which 40 min can be prepped the evening before:
    # fits a 25-minute evening when there is slack earlier in the week.
    recipe = Recipe.model_validate(
        {
            "slug": "ovenschotel",
            "name": "ovenschotel",
            "servings": 4,
            "time": {"cook": 60, "prep_ahead": 40},
            "ingredients": [{"product": "x", "amount": 1}],
        }
    )
    other = _recipe("roerbak", minutes=30)
    evenings = [_evening(D1, 120), _evening(D2, 25)]
    plan = plan_week([recipe, other], _prefs(), evenings)
    assert plan.entries[1].recipe.slug == "ovenschotel"
    assert plan.entries[1].minutes == 20  # 60 - 40 booked as prep
    assert plan.entries[1].prep_ahead_used == 40


def test_prep_ahead_needs_prep_capacity():
    # No earlier slack available: the 60-min dish needs its full time.
    recipe = Recipe.model_validate(
        {
            "slug": "ovenschotel",
            "name": "ovenschotel",
            "servings": 4,
            "time": {"cook": 60, "prep_ahead": 40},
            "ingredients": [{"product": "x", "amount": 1}],
        }
    )
    other = _recipe("roerbak", minutes=25)
    evenings = [_evening(D1, 30), _evening(D2, 45)]
    plan = plan_week([recipe, other], _prefs(), evenings)
    # day 1: roerbak fits (25), oven Schotel needs full 60; day 2: 60 > 45 -> roerbak reuse
    assert [e.recipe.slug for e in plan.entries] == ["roerbak", "roerbak"]


def test_leftover_slot_on_cramped_evening():
    recipes = [_recipe("stoofpot", minutes=90)]
    evenings = [_evening(D1, 120), _evening(D2, 20, allow_leftovers=True)]
    plan = plan_week(recipes, _prefs(), evenings)
    second = plan.entries[1]
    assert second.kind == "leftovers"
    assert second.source_slug == "stoofpot"
    assert second.recipe is None
    assert "restjes" in second.reason


def test_person_weight_scales_score():
    people = [Person(id="adult1", role="adult", weight=2.0)]
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = Preferences(people=people, verdicts={"pasta": {"adult1": "love"}})
    plan = plan_week(recipes, prefs, [_evening(D1, 120)])
    assert plan.entries[0].recipe.slug == "pasta"
    assert plan.entries[0].score == 6.0  # adult group 1.0 * weight 2.0 * love 3.0


def test_absent_person_verdict_not_counted():
    # kid1 loves pasta but is absent: pasta must not beat curry on their love.
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = _prefs({"pasta": {"kid1": "love"}, "curry": {"adult1": "ok"}})
    prefs.people.append(Person(id="adult1", role="adult"))
    evening = EveningAvailability(date=D1, free_minutes=120, joiners=("adult1",))
    plan = plan_week(recipes, prefs, [evening])
    assert plan.entries[0].recipe.slug == "curry"


def test_absent_person_never_verdict_ignored():
    # kid1 'never' eats spruitjes but is absent that evening: allowed.
    recipes = [_recipe("spruitjes", minutes=20), _recipe("pasta")]
    prefs = _prefs({"spruitjes": {"kid1": "never"}})
    evening = EveningAvailability(date=D1, free_minutes=120, joiners=())
    plan = plan_week(recipes, prefs, [evening])
    assert plan.entries[0].recipe.slug == "spruitjes"


def test_everyone_present_behaves_as_before():
    recipes = [_recipe("pasta"), _recipe("curry")]
    prefs = _prefs({"pasta": {"kid1": "love"}})
    evening = EveningAvailability(date=D1, free_minutes=120, joiners=("kid1",))
    plan = plan_week(recipes, prefs, [evening])
    assert plan.entries[0].recipe.slug == "pasta"
    assert plan.entries[0].score == 6.0


# --- composed dishes ---------------------------------------------------------


def _dish(slug, *parts):
    """A dish from (label, [option slugs]) pairs."""
    return ComposedDish.model_validate(
        {
            "slug": slug,
            "name": slug,
            "parts": [{"label": label, "options": list(options)} for label, options in parts],
        }
    )


def test_a_dish_is_proposed_with_every_part_filled():
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10), _recipe("kip")]
    dish = _dish("aardappel-vlees", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"aardappel-vlees": {"kid1": "love"}})  # +6, the parts are neutral

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    entry = plan.entries[0]
    assert entry.dish_slug == "aardappel-vlees"
    assert entry.recipe is None  # the evening is the dish, not one recipe
    assert [part.slug for part in entry.parts] == ["aardappels", "sperziebonen"]
    assert entry.score == 6.0
    assert "Aardappels: aardappels" in entry.reason
    assert "Groente: sperziebonen" in entry.reason


def test_a_dish_verdict_replaces_the_mean_of_its_parts():
    """Loved by both kids: +12 for the combination, not the +6 of the mean."""
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs(
        {
            "aardappels": {"kid1": "love"},
            "sperziebonen": {"kid2": "love"},
            "av": {"kid1": "love", "kid2": "love"},
        }
    )

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert plan.entries[0].dish_slug == "av"
    assert plan.entries[0].score == 12.0


def test_a_dish_verdict_can_be_worse_than_its_parts():
    """'ok' on the combination is a verdict like any other: it says 0, not +6."""
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs(
        {
            "aardappels": {"kid1": "love"},
            "sperziebonen": {"kid2": "love"},
            "av": {"kid1": "ok"},
        }
    )

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert plan.entries[0].dish_slug is None
    assert plan.entries[0].recipe.slug == "sperziebonen"  # +6, the best part alone


def test_never_on_one_option_still_fills_the_part_with_another():
    recipes = [
        _recipe("aardappels"),
        _recipe("sperziebonen", minutes=10),
        _recipe("boerenkool", minutes=10),
    ]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen", "boerenkool"]))
    prefs = _prefs({"sperziebonen": {"kid1": "never"}, "av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert [part.slug for part in plan.entries[0].parts] == ["aardappels", "boerenkool"]


def test_never_on_every_option_of_a_part_skips_the_dish():
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10), _recipe("soep", 20)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"sperziebonen": {"kid1": "never"}, "av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert plan.entries[0].dish_slug is None
    assert plan.entries[0].recipe is not None  # a plain evening instead of a half dish


def test_never_on_the_dish_slug_skips_the_dish_even_when_its_parts_are_loved():
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs(
        {
            "aardappels": {"kid1": "love", "kid2": "love"},
            "sperziebonen": {"kid2": "love"},
            "av": {"kid1": "never"},
        }
    )

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert plan.entries[0].dish_slug is None
    assert plan.entries[0].recipe.slug == "aardappels"


def test_a_part_of_a_deleted_recipe_is_skipped_over():
    recipes = [_recipe("aardappels"), _recipe("boerenkool", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["weg", "boerenkool"]))
    prefs = _prefs({"av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert [part.slug for part in plan.entries[0].parts] == ["aardappels", "boerenkool"]


def test_two_dishes_never_share_a_component_in_one_week():
    recipes = [
        _recipe("sperziebonen", minutes=10),
        _recipe("boerenkool", minutes=10),
        _recipe("kip", 20),
        _recipe("aardappels"),
    ]
    met_maaltijd = _dish(
        "av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen", "boerenkool"])
    )
    groente_vlees = _dish("gv", ("Groente", ["sperziebonen", "boerenkool"]), ("Vlees", ["kip"]))
    prefs = _prefs(
        {
            "av": {"kid1": "love"},
            "gv": {"kid1": "love"},
            "sperziebonen": {"kid2": "ok"},  # the first green, but not enough to win itself
        }
    )

    plan = plan_week(
        recipes,
        prefs,
        [_evening(D1, 120), _evening(D2, 120)],
        dishes=[met_maaltijd, groente_vlees],
    )

    assert [entry.dish_slug for entry in plan.entries] == ["gv", "av"]
    # the second dish has to make do with the other green
    assert [part.slug for part in plan.entries[1].parts] == ["aardappels", "boerenkool"]
    planned_parts = [part.slug for entry in plan.entries for part in entry.parts]
    assert len(planned_parts) == len(set(planned_parts))


def test_a_dish_that_does_not_fit_the_evening_is_skipped():
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10), _recipe("soep", 20)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 25)], dishes=[dish])

    # the dish is 30 min (its longest part); sperziebonen on its own is 10
    assert plan.entries[0].dish_slug is None
    assert plan.entries[0].recipe.slug == "sperziebonen"


def test_a_dish_is_not_planned_twice_while_another_recipe_fits():
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10), _recipe("soep", 20)]
    dish = _dish("av", ("Groente", ["sperziebonen"]))
    prefs = _prefs({"av": {"kid1": "love"}, "soep": {"kid2": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 120), _evening(D2, 120)], dishes=[dish])

    assert [entry.dish_slug for entry in plan.entries] == ["av", None]
    assert plan.entries[1].recipe.slug == "soep"


def test_a_dish_evening_leaves_the_whole_evening_free():
    """A dish is 30 min on the evening (its longest part), not 30+10."""
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 30)], dishes=[dish])

    entry = plan.entries[0]
    assert entry.dish_slug == "av"
    assert entry.minutes == entry.combined_minutes == 30
    assert entry.total_minutes == 40


def test_a_verdict_does_not_hide_what_the_plan_took_off_the_parts():
    """Reused parts keep their price, even when the dish itself is loved."""
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"av": {"kid1": "love"}})

    plan = plan_week(recipes, prefs, [_evening(D1, 120), _evening(D2, 120)], dishes=[dish])

    assert [entry.dish_slug for entry in plan.entries] == ["av", "av"]
    assert plan.entries[0].score == 6.0
    # +6 for the combination, -4 for each of the two parts already cooked
    assert plan.entries[1].score == -2.0
    assert "totaal -2" in plan.entries[1].reason


def test_a_dish_nobody_judged_is_no_better_than_its_best_part():
    """The mean is the rule: judge the combination if you want it served."""
    recipes = [_recipe("aardappels"), _recipe("sperziebonen", minutes=10)]
    dish = _dish("av", ("Aardappels", ["aardappels"]), ("Groente", ["sperziebonen"]))
    prefs = _prefs({"aardappels": {"kid1": "love"}})  # +6 for one part, 0 for the other

    plan = plan_week(recipes, prefs, [_evening(D1, 120)], dishes=[dish])

    assert plan.entries[0].dish_slug is None
    assert plan.entries[0].recipe.slug == "aardappels"
