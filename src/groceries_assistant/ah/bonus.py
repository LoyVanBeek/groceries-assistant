"""AH bonus (aanbieding) products: a cached list of webshop ids plus the
matching that feeds the planner's bonus preference.

The list comes from the anonymous mobile API (searched per ingredient name,
keeping the hits flagged isBonus) and is kept in the plandatabase (tabel
bonus_products) with its fetch date; without a cache the bonus preference is
simply off. Oude backups met data/bonus.yaml worden bij het opstarten/
importeren één keer in de tabel ingelezen.

Matching is by webshop id only: an ingredient counts as "in bonus" exactly
when its coupled AH product (ah_mappings) is in the fetched list. No name
matching — a generic ingredient like 'olijfolie' must not light up just
because some other olijfolie pack is on sale.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from groceries_assistant.ah.matching import AHMappingStore
from groceries_assistant.store.db import Store

# The daily automatic fetch fires at one random moment in this window
# (local time): early enough that the new bonus week is live, randomized so
# every instance does not hammer the AH API at the same minute.
BONUS_WINDOW_START = time(4, 0)
BONUS_WINDOW_END = time(9, 0)


def random_bonus_slot(day: date, rng: random.Random | None = None) -> datetime:
    """A random moment within the bonus window on `day` (local time)."""
    if rng is None:
        rng = random.Random()
    start = datetime.combine(day, BONUS_WINDOW_START)
    span = (datetime.combine(day, BONUS_WINDOW_END) - start).total_seconds()
    return start + timedelta(seconds=rng.uniform(0, span))


def fetch_due(
    fetched_at: date | None,
    last_attempt: datetime | None,
    *,
    now: datetime,
    slot: datetime,
    min_retry_seconds: int = 3600,
) -> bool:
    """Whether the daily bonus fetch should run now: not fetched today yet,
    the day's (random) slot has passed, and the last attempt is old enough."""
    if fetched_at == now.date():
        return False
    if now < slot:
        return False
    return last_attempt is None or (now - last_attempt).total_seconds() >= min_retry_seconds


class BonusStore:
    """The bonus list in the plandatabase, fetched on demand."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._products: list[tuple[int, str]] = []
        self._ids: set[int] = set()
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
        self._ids = {webshop_id for webshop_id, _title in products}
        self._fetched_at = fetched
        self._counts = {}

    @property
    def products(self) -> list[tuple[int, str]]:
        return list(self._products)

    @property
    def fetched_at(self) -> date | None:
        return self._fetched_at

    @property
    def last_attempt(self) -> datetime | None:
        """When the last fetch was attempted (success or failure), if ever."""
        row = self._store.conn.execute(
            "SELECT value FROM bonus_meta WHERE key = 'last_attempt'"
        ).fetchone()
        return _parse_datetime(row["value"] if row else None)

    def note_attempt(self, when: datetime | None = None) -> None:
        when = when or datetime.now()
        self._store.conn.execute(
            "INSERT OR REPLACE INTO bonus_meta(key, value) VALUES ('last_attempt', ?)",
            (when.isoformat(),),
        )
        self._store.conn.commit()

    def count_for(self, slug: str, ingredients: Iterable, mappings: AHMappingStore) -> int:
        """How many of the recipe's ingredients are in the bonus — matched by
        the coupled webshop id only; unmapped ingredients never count.
        Cached per slug until the list changes."""
        if slug not in self._counts:
            self._counts[slug] = sum(
                1
                for ingredient in ingredients
                if (mapping := mappings.get(ingredient.product)) is not None
                and self.webshop_id_in_bonus(mapping.ah_webshop_id)
            )
        return self._counts[slug]

    def webshop_id_in_bonus(self, webshop_id: int) -> bool:
        """Whether this exact AH product is in the fetched bonus list."""
        return webshop_id in self._ids


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
