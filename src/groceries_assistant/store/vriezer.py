"""De vriezer: ingevroren restjes per gerecht, in de plandatabase.

Wat erin staat overleeft 'week leegmaken' (de tabel wordt door het leegmaken
niet aangeraakt) en kan op elke dag gegeten worden — ook in een latere week.
Per gerecht bewaart de tabel de porties én de datum van de oudste portie,
zodat wat het langst staat het eerst gegeten kan worden. De oude yaml-versie
(data/vriezer.yaml) wordt bij het opstarten één keer ingelezen door de
migratie in db.py; het bestand wordt daarna niet meer gebruikt.
"""

from __future__ import annotations

from datetime import date

from groceries_assistant.store.db import Store


class VriezerStore:
    """De vriezer-tabel: gerecht -> porties + sinds (de oudste portie)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> None:
        """Compatibiliteit: de database leest live, er is niets in te laden."""

    def portions(self, dish_slug: str) -> int:
        row = self._store.conn.execute(
            "SELECT portions FROM vriezer WHERE slug = ?", (dish_slug,)
        ).fetchone()
        return int(row["portions"]) if row else 0

    def since(self, dish_slug: str) -> str | None:
        """De datum waarop de oudste portie van dit gerecht erin ging."""
        row = self._store.conn.execute(
            "SELECT since FROM vriezer WHERE slug = ?", (dish_slug,)
        ).fetchone()
        return row["since"] if row else None

    def add(self, dish_slug: str, portions: int, since: date | None = None) -> None:
        """Bewaar (of neem weg, met een negatief getal) porties van een gerecht.

        De 'sinds'-datum blijft de oudste: bijvullen verschuift haar niet; een
        nieuwe portie zonder datum geldt vanaf vandaag.
        """
        row = self._store.conn.execute(
            "SELECT portions, since FROM vriezer WHERE slug = ?", (dish_slug,)
        ).fetchone()
        current = int(row["portions"]) if row else 0
        since_value = ((row["since"] or None) if row else None) or (
            since or date.today()
        ).isoformat()
        total = current + portions
        if total > 0:
            self._store.conn.execute(
                "INSERT INTO vriezer(slug, portions, since) VALUES (?,?,?)"
                " ON CONFLICT(slug) DO UPDATE SET portions = excluded.portions,"
                " since = excluded.since",
                (dish_slug, total, since_value),
            )
        else:
            self._store.conn.execute("DELETE FROM vriezer WHERE slug = ?", (dish_slug,))
        self._store.conn.commit()

    def take(self, dish_slug: str, portions: int) -> None:
        self.add(dish_slug, -portions)

    def remove(self, dish_slug: str) -> None:
        """Handmatig weggeven: de hele regel eruit."""
        self._store.conn.execute("DELETE FROM vriezer WHERE slug = ?", (dish_slug,))
        self._store.conn.commit()

    def all(self) -> dict[str, int]:
        """slug -> porties; de planner kijkt alleen naar de aantallen."""
        return {
            row["slug"]: int(row["portions"])
            for row in self._store.conn.execute("SELECT slug, portions FROM vriezer")
        }

    def entries(self) -> list[dict]:
        """{slug, portions, since} — het langst ingevroren eerst."""
        return [
            {
                "slug": row["slug"],
                "portions": int(row["portions"]),
                "since": row["since"],
            }
            for row in self._store.conn.execute(
                "SELECT slug, portions, since FROM vriezer ORDER BY since IS NULL, since, slug"
            )
        ]
