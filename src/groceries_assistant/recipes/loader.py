"""Load and save recipe YAML files (the user-editable source of truth)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from groceries_assistant.models import Recipe, slugify


class RecipeError(Exception):
    """A recipe file is missing, unreadable or invalid."""


def load_recipe(path: Path) -> Recipe:
    path = Path(path)
    if not path.is_file():
        raise RecipeError(f"Recipe file not found: {path}")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RecipeError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError(f"{path}: expected a YAML mapping at the top level")
    data.setdefault("slug", slugify(path.stem))
    try:
        return Recipe.model_validate(data)
    except ValidationError as exc:
        raise RecipeError(f"{path}: {exc}") from exc


def load_recipes(directory: Path) -> list[Recipe]:
    """Load all *.yaml/*.yml recipes in a directory; raises on any problem."""
    recipes, errors = load_recipes_with_errors(directory)
    if errors:
        raise RecipeError("Failed to load recipes:\n" + "\n".join(errors))
    return recipes


def dinners(recipes: Iterable[Recipe]) -> list[Recipe]:
    """The recipes that are a meal on their own, i.e. everything but a component.

    A component ('kipfilet', 'brocolli') lives in the same directory and is a
    valid part option, but the planner must never offer it as an evening.
    """
    return [recipe for recipe in recipes if not recipe.component]


def load_recipes_with_errors(directory: Path) -> tuple[list[Recipe], list[str]]:
    """Load all recipes, skipping invalid files; returns (recipes, error messages)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise RecipeError(f"Recipe directory not found: {directory}")

    paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in (".yaml", ".yml"))
    recipes: list[Recipe] = []
    errors: list[str] = []
    for path in paths:
        try:
            recipes.append(load_recipe(path))
        except RecipeError as exc:
            errors.append(str(exc))
    return recipes, errors


def save_recipe(path: Path, recipe: Recipe) -> None:
    """Persist a recipe (including importer-written Grocy ids) back to YAML.

    Note: YAML comments are not preserved.
    """
    data = recipe.model_dump(mode="json", exclude_none=True)
    Path(path).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
