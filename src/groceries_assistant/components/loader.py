"""Load and save component YAML files.

A component is a product you buy in the store, used as a part of a composed
dish. It has no steps, tags, servings or ingredients list — it IS the item
('zak krieltjes 750 g', 'brocolli').
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from groceries_assistant.models import Component, slugify


class ComponentError(Exception):
    """A component file is missing, unreadable or invalid."""


def load_component(path: Path) -> Component:
    path = Path(path)
    if not path.is_file():
        raise ComponentError(f"Component file not found: {path}")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ComponentError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ComponentError(f"{path}: expected a YAML mapping at the top level")
    data.setdefault("slug", slugify(path.stem))
    try:
        return Component.model_validate(data)
    except ValidationError as exc:
        raise ComponentError(f"{path}: {exc}") from exc


def load_components(directory: Path) -> list[Component]:
    """Load all *.yaml/*.yml components in a directory; raises on any problem."""
    components, errors = load_components_with_errors(directory)
    if errors:
        raise ComponentError("Failed to load components:\n" + "\n".join(errors))
    return components


def load_components_with_errors(directory: Path) -> tuple[list[Component], list[str]]:
    """Load all components, skipping invalid files; returns (components, errors)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ComponentError(f"Component directory not found: {directory}")

    paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in (".yaml", ".yml"))
    components: list[Component] = []
    errors: list[str] = []
    for path in paths:
        try:
            components.append(load_component(path))
        except ComponentError as exc:
            errors.append(str(exc))
    return components, errors


def save_component(path: Path, component: Component) -> None:
    """Persist a component back to YAML."""
    data = component.model_dump(mode="json", exclude_none=True)
    Path(path).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
