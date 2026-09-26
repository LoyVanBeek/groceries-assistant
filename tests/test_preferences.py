from groceries_assistant.preferences import VERDICT_POINTS, Person, Preferences


def test_score_group_times_person_weight():
    prefs = Preferences(
        people=[Person(id="a", role="adult", weight=2.0)],
        verdicts={"r": {"a": "love"}},
    )
    assert prefs.recipe_score("r") == 1.0 * 2.0 * VERDICT_POINTS["love"]  # 6.0


def test_score_kid_group_default_weight():
    prefs = Preferences(people=[Person(id="k", role="kid")], verdicts={"r": {"k": "ok"}})
    assert prefs.recipe_score("r") == 2.0 * VERDICT_POINTS["ok"]  # 2.0


def test_custom_group_weights_override_default():
    prefs = Preferences(
        people=[Person(id="k", role="kid")],
        group_weights={"kid": 3.0, "adult": 1.0},
        verdicts={"r": {"k": "love"}},
    )
    assert prefs.recipe_score("r") == 3.0 * VERDICT_POINTS["love"]  # 9.0


def test_never_not_scored_and_reported():
    prefs = Preferences(
        people=[Person(id="k", role="kid"), Person(id="a", role="adult")],
        verdicts={"r": {"k": "never", "a": "love"}},
    )
    assert prefs.recipe_score("r") == VERDICT_POINTS["love"]  # never contributes 0
    assert prefs.never_by("r") == ["k"]


def test_disliked_by_report():
    prefs = Preferences(people=[Person(id="k", role="kid")], verdicts={"r": {"k": "dislike"}})
    assert prefs.disliked_by("r") == ["k"]


def test_score_zero_without_verdicts():
    prefs = Preferences(people=[Person(id="k", role="kid")])
    assert prefs.recipe_score("anything") == 0.0
