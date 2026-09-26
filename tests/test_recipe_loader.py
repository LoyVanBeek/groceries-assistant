import pytest

from groceries_assistant.models import Recipe, slugify
from groceries_assistant.recipes.loader import RecipeError, load_recipe, load_recipes, save_recipe

VALID = """
name: Boerenkool stamppot
servings: 4
time: {prep: 15, cook: 30}
tags: [stamppot]
steps:
  - Kook de aardappelen gaar.
ingredients:
  - {product: aardappelen, amount: 1200, unit: g}
  - {product: boerenkool, amount: 400, unit: g}
"""


def _write(tmp_path, name="stamppot.yaml", content=VALID):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_slugify():
    assert slugify("Pasta met pesto!") == "pasta-met-pesto"
    assert slugify("  Boerenkool_Stamppot  ") == "boerenkool-stamppot"
    assert slugify("!!!") == "recipe"


def test_load_recipe_valid(tmp_path):
    path = _write(tmp_path)
    recipe = load_recipe(path)
    assert isinstance(recipe, Recipe)
    assert recipe.slug == "stamppot"
    assert recipe.name == "Boerenkool stamppot"
    assert recipe.time.total_minutes == 45
    assert len(recipe.ingredients) == 2
    assert recipe.ingredients[0].unit == "g"
    assert recipe.protein is None


def test_load_recipe_defaults(tmp_path):
    path = _write(
        tmp_path,
        content="name: X\nservings: 2\ningredients:\n  - {product: eieren, amount: 6}\n",
    )
    recipe = load_recipe(path)
    assert recipe.time.total_minutes == 0
    assert recipe.ingredients[0].unit == "stuk"
    assert recipe.tags == []


def test_load_recipe_missing_file(tmp_path):
    with pytest.raises(RecipeError, match="not found"):
        load_recipe(tmp_path / "nope.yaml")


def test_load_recipe_invalid_yaml(tmp_path):
    path = _write(tmp_path, content="name: [unclosed")
    with pytest.raises(RecipeError, match="invalid YAML"):
        load_recipe(path)


def test_load_recipe_missing_name(tmp_path):
    path = _write(tmp_path, content="servings: 4\ningredients: [{product: x, amount: 1}]\n")
    with pytest.raises(RecipeError, match="name"):
        load_recipe(path)


def test_load_recipe_empty_ingredients(tmp_path):
    path = _write(tmp_path, content="name: X\nservings: 4\ningredients: []\n")
    with pytest.raises(RecipeError, match="ingredients"):
        load_recipe(path)


def test_load_recipes_collects_errors(tmp_path):
    _write(tmp_path)
    _write(tmp_path, name="bad.yaml", content="servings: 4\n")
    with pytest.raises(RecipeError, match=r"bad\.yaml"):
        load_recipes(tmp_path)


def test_load_recipes_empty_dir(tmp_path):
    assert load_recipes(tmp_path) == []


def test_save_recipe_round_trip(tmp_path):
    path = _write(tmp_path)
    recipe = load_recipe(path)
    recipe.protein = "vegetarisch"
    save_recipe(path, recipe)

    reloaded = load_recipe(path)
    assert reloaded.protein == "vegetarisch"
    assert reloaded.name == recipe.name
    assert reloaded.steps == recipe.steps
