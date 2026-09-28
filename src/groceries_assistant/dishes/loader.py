"""Load and save composed-dish YAML files (the user-editable definitions)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from groceries_assistant.models import ComposedDish, slugify

DISH_SUFFIXES = (".yaml", ".yml")


class DishError(Exception):
    """A dish file is missing, unreadable or invalid."""


def dish_file(directory: Path, slug: str) -> Path:
    """Path of one dish's file inside its directory."""
    return Path(directory) / f"{slug}.yaml"


def load_dish(path: Path) -> ComposedDish:
    path = Path(path)
    if not path.is_file():
        raise DishError(f"Dish file not found: {path}")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DishError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DishError(f"{path}: expected a YAML mapping at the top level")
    data.setdefault("slug", slugify(path.stem))
    return _validate(path, data)


def load_dishes_with_errors(directory: Path) -> tuple[list[ComposedDish], list[str]]:
    """Load all dishes, skipping invalid files; returns (dishes, error messages)."""
    directory = Path(directory)
    if not directory.is_dir():
        return [], []

    paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in DISH_SUFFIXES)
    dishes: list[ComposedDish] = []
    errors: list[str] = []
    for path in paths:
        try:
            dishes.append(load_dish(path))
        except DishError as exc:
            errors.append(str(exc))
    return dishes, errors


def load_dishes(directory: Path) -> list[ComposedDish]:
    """Load all dishes in a directory; raises on any problem."""
    dishes, errors = load_dishes_with_errors(directory)
    if errors:
        raise DishError("Failed to load dishes:\n" + "\n".join(errors))
    return dishes


def save_dish(path: Path, dish: ComposedDish) -> None:
    """Persist a dish back to YAML.

    Note: YAML comments are not preserved.
    """
    data = dish.model_dump(mode="json", exclude_none=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _validate(path: Path, data: Any) -> ComposedDish:
    try:
        return ComposedDish.model_validate(data)
    except ValidationError as exc:
        raise DishError(f"{path}: {exc}") from exc
