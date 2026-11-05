"""AH bonus (aanbieding) products: a cached, hand-editable list plus the
ingredient matching that feeds the planner's bonus preference.

The list comes from the anonymous mobile API (bonuspage/v1: periods ->
sections -> products and bonus groups) and is cached to data/bonus.yaml with
its fetch date; without a cache the bonus preference is simply off.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import yaml

from groceries_assistant.ah.matching import normalize


class BonusStore:
    """The bonus list in data/bonus.yaml, fetched on demand, hand-editable."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._products: list[tuple[int, str]] = []
        self._normalized_titles: list[str] = []
        self._fetched_at: date | None = None
        self._counts: dict[str, int] = {}

    def load(self) -> None:
        """Read the cache; a missing or broken file simply means 'no bonus'."""
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        products: list[tuple[int, str]] = []
        for item in raw.get("products") or []:
            try:
                products.append((int(item["webshop_id"]), str(item["title"])))
            except (KeyError, TypeError, ValueError):
                continue
        self._set(products, _parse_date(raw.get("fetched_at")))

    def save(self, products: Iterable[tuple[int, str]], fetched: date) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        products = list(products)
        self._path.write_text(
            yaml.safe_dump(
                {
                    "fetched_at": fetched.isoformat(),
                    "products": [
                        {"webshop_id": webshop_id, "title": title} for webshop_id, title in products
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self._set(products, fetched)

    def _set(self, products: list[tuple[int, str]], fetched: date | None) -> None:
        self._products = list(products)
        self._normalized_titles = [normalize(title) for _id, title in self._products]
        self._fetched_at = fetched
        self._counts = {}

    @property
    def products(self) -> list[tuple[int, str]]:
        return list(self._products)

    @property
    def fetched_at(self) -> date | None:
        return self._fetched_at

    def count_for(self, slug: str, ingredients: Iterable) -> int:
        """How many of the recipe's ingredients are (probably) in the bonus;
        cached per slug until the list changes."""
        if slug not in self._counts:
            self._counts[slug] = sum(
                1 for ingredient in ingredients if self._in_bonus(ingredient.product)
            )
        return self._counts[slug]

    def _in_bonus(self, product: str) -> bool:
        name = normalize(product)
        if not name or not self._normalized_titles:
            return False
        for title_name in self._normalized_titles:
            if name == title_name:
                return True
            # whole-word containment: 'tomatenblokjes' matches 'hak
            # tomatenblokjes', but 'ui' must not match 'uitgeperste'
            if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", title_name):
                return True
        return False


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
