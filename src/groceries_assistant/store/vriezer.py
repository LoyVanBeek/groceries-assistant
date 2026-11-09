"""The freezer store: data/vriezer.yaml maps a dish slug to the portions of
restjes stored away.

Frozen restjes survive week clearing (the file is never touched by leegmaken)
and can be eaten on any later day — even in a later week. Hand-editable.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class VriezerStore:
    """dish_slug -> portions, YAML-backed, hand-editable."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._map: dict[str, int] = {}

    def load(self) -> None:
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        self._map = {}
        for slug, portions in raw.items():
            try:
                if int(portions) > 0:
                    self._map[str(slug)] = int(portions)
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(self._map, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

    def portions(self, dish_slug: str) -> int:
        return self._map.get(dish_slug, 0)

    def add(self, dish_slug: str, portions: int) -> None:
        """Store (or take, with a negative number) portions of a dish."""
        total = self._map.get(dish_slug, 0) + portions
        if total > 0:
            self._map[dish_slug] = total
        else:
            self._map.pop(dish_slug, None)
        self.save()

    def take(self, dish_slug: str, portions: int) -> None:
        self.add(dish_slug, -portions)

    def all(self) -> dict[str, int]:
        return dict(self._map)
