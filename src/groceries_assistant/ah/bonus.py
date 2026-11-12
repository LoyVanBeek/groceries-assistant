"""AH bonus (aanbieding) products: a cached list plus the ingredient matching
that feeds the planner's bonus preference.

The list comes from the anonymous mobile API (bonuspage/v1: periods ->
sections -> products and bonus groups) and is kept in the plandatabase
(tabel bonus_products) with its fetch date; without a cache the bonus
preference is simply off. Oude backups met data/bonus.yaml worden bij het
opstarten/importeren één keer in de tabel ingelezen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from groceries_assistant.ah.matching import normalize
from groceries_assistant.store.db import Store


class BonusStore:
    """The bonus list in the plandatabase, fetched on demand."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._products: list[tuple[int, str]] = []
        self._normalized_titles: list[str] = []
        self._fetched_at: date | None = None
        self._counts: dict[str, int] = {}
        self.load()

    def load(self) -> None:
        """Read the cache; an empty table simply means 'no bonus'."""
        products: list[tuple[int, str]] = [
            (int(row["webshop_id"]), str(row["title"]))
            for row in self._store.conn.execute(
                "SELECT webshop_id, title FROM bonus_products ORDER BY title"
            )
        ]
        meta = self._store.conn.execute(
            "SELECT value FROM bonus_meta WHERE key = 'fetched_at'"
        ).fetchone()
        self._set(products, _parse_date(meta["value"] if meta else None))

    def save(self, products: Iterable[tuple[int, str]], fetched: date) -> None:
        products = list(products)
        self._store.conn.execute("DELETE FROM bonus_products")
        for webshop_id, title in products:
            self._store.conn.execute(
                "INSERT INTO bonus_products(webshop_id, title) VALUES (?,?)",
                (webshop_id, title),
            )
        self._store.conn.execute(
            "INSERT OR REPLACE INTO bonus_meta(key, value) VALUES ('fetched_at', ?)",
            (fetched.isoformat(),),
        )
        self._store.conn.commit()
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
                1 for ingredient in ingredients if self.is_in_bonus(ingredient.product)
            )
        return self._counts[slug]

    def is_in_bonus(self, product: str) -> bool:
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
