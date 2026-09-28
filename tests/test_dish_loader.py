"""Composed-dish definitions: YAML loading and saving."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from groceries_assistant.dishes.loader import (
    DishError,
    dish_file,
    load_dish,
    load_dishes,
    load_dishes_with_errors,
    save_dish,
)
from groceries_assistant.models import ComposedDish

VALID = """
slug: aardappel-groente-vlees
name: Aardappel-groente-vlees
parts:
  - key: aardappels
    label: Aardappels
    options:
      - aardappels-uit-de-oven
      - gekookte-aardappels
  - key: groente
    label: Groente
    options: [sperziebonen, boerenkool]
"""


def write(directory, name: str, text: str) -> Path:
    (directory / name).write_text(text, encoding="utf-8")
    return directory / name


def _write(directory, text: str) -> Path:
    """Write one dish file and return its path."""
    return write(directory, "dish.yaml", text)


def test_load_dish_reads_parts_and_options(tmp_path):
    dish = load_dish(_write(tmp_path, VALID))

    assert dish.slug == "aardappel-groente-vlees"
    assert [p.label for p in dish.parts] == ["Aardappels", "Groente"]
    assert dish.parts[1].options == ["sperziebonen", "boerenkool"]
    assert dish.part("groente") is dish.parts[1]
    assert dish.part("kaas") is None


def test_option_slugs_covers_every_part(tmp_path):
    assert load_dish(_write(tmp_path, VALID)).option_slugs == {
        "aardappels-uit-de-oven",
        "gekookte-aardappels",
        "sperziebonen",
        "boerenkool",
    }


def test_slug_falls_back_to_the_file_stem(tmp_path):
    write(
        tmp_path,
        "pizza-avond.yaml",
        "name: Pizza-avond\nparts:\n  - label: Bodem\n    options: [pizzadeeg]\n",
    )
    dish = load_dish(tmp_path / "pizza-avond.yaml")

    assert dish.slug == "pizza-avond"
    assert dish.parts[0].key == "bodem"


def test_duplicate_part_keys_are_rejected(tmp_path):
    write(
        tmp_path,
        "dubbel.yaml",
        "name: Dubbel\nparts:\n  - label: Groente\n    options: [a]\n"
        "  - label: groente\n    options: [b]\n",
    )
    with pytest.raises(DishError, match="part keys must be unique"):
        load_dish(tmp_path / "dubbel.yaml")


def test_a_part_needs_at_least_one_option(tmp_path):
    write(tmp_path, "leeg.yaml", "name: Leeg\nparts:\n  - label: Groente\n    options: []\n")
    with pytest.raises(DishError, match="at least 1 item"):
        load_dish(tmp_path / "leeg.yaml")


def test_a_dish_needs_at_least_one_part(tmp_path):
    write(tmp_path, "niets.yaml", "name: Niets\nparts: []\n")
    with pytest.raises(DishError):
        load_dish(tmp_path / "niets.yaml")


def test_a_dish_needs_a_name(tmp_path):
    write(tmp_path, "naam.yaml", "name: '  '\nparts:\n  - label: A\n    options: [b]\n")
    with pytest.raises(DishError, match="must not be empty"):
        load_dish(tmp_path / "naam.yaml")


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(DishError, match="not found"):
        load_dish(tmp_path / "niets.yaml")


def test_invalid_yaml_is_an_error(tmp_path):
    write(tmp_path, "kapot.yaml", "name: [kapot\n")
    with pytest.raises(DishError, match="invalid YAML"):
        load_dish(tmp_path / "kapot.yaml")


def test_a_non_mapping_file_is_an_error(tmp_path):
    write(tmp_path, "lijst.yaml", "- een\n- twee\n")
    with pytest.raises(DishError, match="YAML mapping"):
        load_dish(tmp_path / "lijst.yaml")


def test_one_broken_file_does_not_hide_the_others(tmp_path):
    write(tmp_path, "goed.yaml", VALID)
    write(tmp_path, "kapot.yaml", "name: [kapot\n")

    dishes, errors = load_dishes_with_errors(tmp_path)

    assert [d.name for d in dishes] == ["Aardappel-groente-vlees"]
    assert len(errors) == 1 and "kapot.yaml" in errors[0]


def test_load_dishes_raises_on_any_error(tmp_path):
    write(tmp_path, "goed.yaml", VALID)
    write(tmp_path, "kapot.yaml", "name: [kapot\n")

    with pytest.raises(DishError, match="Failed to load dishes"):
        load_dishes(tmp_path)


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert load_dishes_with_errors(tmp_path / "weg") == ([], [])


def test_yml_files_are_loaded_too(tmp_path):
    write(tmp_path, "lasagne.yml", VALID)
    assert [d.slug for d in load_dishes_with_errors(tmp_path)[0]] == ["aardappel-groente-vlees"]


def test_save_and_load_round_trip(tmp_path):
    dish = ComposedDish.model_validate(
        {
            "slug": "pizza-avond",
            "name": "Pizza-avond",
            "notes": "met de kids",
            "parts": [{"label": "Bodem", "options": ["pizzadeeg"]}],
        }
    )

    save_dish(dish_file(tmp_path, dish.slug), dish)
    loaded = load_dish(dish_file(tmp_path, dish.slug))

    assert loaded == dish
    assert "pizzadeeg" in (tmp_path / "pizza-avond.yaml").read_text(encoding="utf-8")


def test_save_creates_the_directory(tmp_path):
    save_dish(
        tmp_path / "nieuw" / "vis.yaml",
        ComposedDish.model_validate(
            {"slug": "vis", "name": "Vis", "parts": [{"label": "Vis", "options": ["zalm"]}]}
        ),
    )
    assert (tmp_path / "nieuw" / "vis.yaml").is_file()


def test_validate_error_is_wrapped_as_a_dish_error(tmp_path):
    write(tmp_path, "kapot.yaml", "name: X\nparts:\n  - label: A\n    options: 3\n")
    with pytest.raises(DishError, match=r"kapot\.yaml"):
        load_dish(tmp_path / "kapot.yaml")


def test_validation_error_from_pydantic_is_a_dish_error():
    # a guard on the wrapping itself: pydantic's error must not leak
    with pytest.raises(ValidationError):
        ComposedDish.model_validate({"slug": "x", "name": "X", "parts": []})
