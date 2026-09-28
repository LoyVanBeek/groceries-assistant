import pytest

from groceries_assistant.models import Ingredient
from groceries_assistant.recipes.textparser import (
    parse_amount,
    parse_ingredient_line,
    parse_recipe_text,
    parse_servings,
    parse_times,
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("400", 400.0),
        ("1,5", 1.5),
        ("1.5", 1.5),
        ("1 1/2", 1.5),
        ("1/2", 0.5),
        ("2-3", 2.0),
        ("½", 0.5),
    ],
)
def test_parse_amount(token, expected):
    assert parse_amount(token) == expected


@pytest.mark.parametrize(
    ("line", "product", "amount", "unit"),
    [
        ("400 g pasta penne", "pasta penne", 400, "g"),
        ("1,5 kg aardappelen", "aardappelen", 1.5, "kg"),
        ("2 el olijfolie", "olijfolie", 2, "el"),
        ("1 tl kaneel", "kaneel", 1, "tl"),
        ("1 blikje tomatenblokjes", "tomatenblokjes", 1, "blikje"),
        ("½ courgette", "courgette", 0.5, "stuk"),
        ("1 1/2 liter melk", "melk", 1.5, "l"),
        ("3 tenen knoflook", "knoflook", 3, "teen"),
        ("1 teen knoflook", "knoflook", 1, "teen"),
        ("een handvol pijnboompitten", "pijnboompitten", 1, "handvol"),
        ("2 grote uien", "grote uien", 2, "stuk"),
        ("300 ml slasaus", "slasaus", 300, "ml"),
        ("1 pakje deeg", "pakje deeg", 1, "stuk"),  # pakje not a unit -> product
        ("150 gram verse spinazie", "verse spinazie", 150, "g"),
        ("- 4 plakken kaas", "kaas", 4, "plak"),
        ("8 borrelglazen jus", "borrelglazen jus", 8, "stuk"),
    ],
)
def test_parse_ingredient_line(line, product, amount, unit):
    ingredient = parse_ingredient_line(line)
    assert ingredient == Ingredient(product=product, amount=amount, unit=unit)


@pytest.mark.parametrize(
    "line",
    [
        "Snijd de ui in halve maantjes.",
        "Kook de pasta volgens de aanwijzingen.",
        "",
        "Bereiding",
    ],
)
def test_parse_ingredient_line_rejects_steps(line):
    assert parse_ingredient_line(line) is None


def test_parse_servings():
    assert parse_servings("Voor 4 personen") == 4
    assert parse_servings("aantal personen: 6") == 6
    assert parse_servings("2 porties") == 2
    assert parse_servings("heerlijk gerecht") is None


def test_parse_times():
    prep, cook = parse_times("Bereidingstijd: 25 minuten")
    assert prep is None and cook == 25

    prep, cook = parse_times("Voorbereidingstijd: 15 min\nBereidingstijd: 1 uur en 10 min")
    assert prep == 15
    assert cook == 70


def test_parse_full_recipe_text():
    text = """
Spaghetti carbonara

Voor 4 personen
Bereidingstijd: 20 minuten

Ingrediënten:
- 400 g spaghetti
- 150 g pancetta
- 4 eieren
- 100 g geraspte kaas
- snufje peper

Bereiding
1. Kook de spaghetti gaar.
2. Bak de pancetta knapperig.
3. Meng alles door elkaar en serveer direct.
"""
    result = parse_recipe_text(text)
    assert result.name == "Spaghetti carbonara"
    assert result.servings == 4
    assert result.cook_minutes == 20
    assert len(result.ingredients) == 5
    assert result.ingredients[0].product == "spaghetti"
    assert result.ingredients[4].product == "peper"
    assert result.ingredients[4].unit == "snufje"
    assert len(result.steps) == 3
    assert result.steps[0].startswith("Kook de spaghetti")
    assert result.warnings == []


def test_parse_without_headers_uses_heuristics():
    text = """
Erwtensoep

400 g gespleten erwten
1 knolselderij
2 el zout

Snijd de groenten klein.
Kook alles in 1 uur gaar.
"""
    result = parse_recipe_text(text)
    assert result.name == "Erwtensoep"
    assert len(result.ingredients) == 3
    assert result.ingredients[0].product == "gespleten erwten"
    assert len(result.steps) == 2


def test_parse_quantityless_ingredient_warns():
    text = """
Test

Ingrediënten:
zout en peper
1 ui

Bereiding
Snijd de ui.
"""
    result = parse_recipe_text(text)
    assert any("Geen hoeveelheid" in w for w in result.warnings)
    assert result.ingredients[0].product == "zout en peper"


def test_parse_subheader_returns_to_ingredients():
    text = """
Gerecht

Bereiding
Stap een.

Voor de saus:
200 ml room
1 tl mosterd

Roer de saus door.
"""
    result = parse_recipe_text(text)
    assert {i.product for i in result.ingredients} == {"room", "mosterd"}
    assert len(result.steps) == 2


def test_parse_empty_text():
    result = parse_recipe_text("")
    assert result.warnings  # something reported
    assert result.ingredients == []


def test_a_pasted_whole_webpage_trims_to_the_recipe():
    """'Copy the page as a whole' (ah.nl) keeps navigation/FAQ out of the parse."""
    page = """AH.nl - Recepten
Home
Recepten
Hoofdgerecht
Vis
Gerookte zalm met avocadosalsa
4 personen
15 min bereiden
Ingrediënten en bereiding
2 limoenen
1 avocado
200 g gerookte zalm
Bereidingswijze
Boen de helft van de limoenen goed schoon.
Snijd de avocado overlangs doormidden.
Voedingswaarden per portie
energie 420 kcal
Anderen bekeken ook
Pasta pesto
Nieuwsbrief
© 2026 Albert Heijn"""
    result = parse_recipe_text(page)
    assert result.name == "Gerookte zalm met avocadosalsa"
    assert result.servings == 4
    assert result.cook_minutes == 15
    assert [i.product for i in result.ingredients] == ["limoenen", "avocado", "gerookte zalm"]
    assert result.steps == [
        "Boen de helft van de limoenen goed schoon.",
        "Snijd de avocado overlangs doormidden.",
    ]
    assert result.warnings == []


def test_a_short_clean_paste_is_not_trimmed():
    """The existing paste format keeps parsing exactly as before."""
    text = "Pasta pesto\n2 personen\nIngrediënten:\n200 g spaghetti\nBereiding\nKook de pasta."
    result = parse_recipe_text(text)
    assert result.name == "Pasta pesto"
    assert result.servings == 2
    assert result.ingredients[0].product == "spaghetti"
