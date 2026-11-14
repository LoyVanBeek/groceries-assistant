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
        """slug -> porties; de echte voorraad (de freezerpagina toont haar)."""
        return {
            row["slug"]: int(row["portions"])
            for row in self._store.conn.execute("SELECT slug, portions FROM vriezer")
        }

    def available_all(self) -> dict[str, int]:
        """slug -> wat er nog beloofd kan worden: de voorraad minus de
        geplande eruit-bewegingen (de planner en de keuzepagina kijken hier
        naar, zodat geplande porties niet dubbel toegezegd worden)."""
        return {
            row["slug"]: max(0, int(row["portions"]) - int(row["gepland"]))
            for row in self._store.conn.execute(
                "SELECT v.slug AS slug, v.portions AS portions,"
                " COALESCE(SUM(g.portions), 0) AS gepland"
                " FROM vriezer v LEFT JOIN vriezer_gepland g"
                " ON g.slug = v.slug AND g.portions < 0"
                " GROUP BY v.slug, v.portions"
            )
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

    # --- geplande bewegingen: pas na menselijke bevestiging bij de voorraad ---

    def plan_add(self, dish_slug: str, portions: int, source: str) -> int:
        """Plan dat er porties IN de vriezer gaan (bv. het overschot van een
        kookavond). De voorraad verandert pas bij bevestiging."""
        return self._plan(dish_slug, portions, source)

    def plan_take(self, dish_slug: str, portions: int, source: str) -> int:
        """Plan dat er porties UIT de vriezer gaan (bv. een geplande
        vriezer-maaltijd). De voorraad verandert pas bij bevestiging."""
        return self._plan(dish_slug, -portions, source)

    def _plan(self, dish_slug: str, portions: int, source: str) -> int:
        if portions == 0:
            raise ValueError("porties mag niet nul zijn")
        cur = self._store.conn.execute(
            "INSERT INTO vriezer_gepland(slug, portions, source, created) VALUES (?,?,?,?)",
            (dish_slug, portions, source, date.today().isoformat()),
        )
        self._store.conn.commit()
        return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        """De geplande bewegingen, oudste eerst: {id, slug, portions, source}."""
        return [
            {
                "id": int(row["id"]),
                "slug": row["slug"],
                "portions": int(row["portions"]),
                "source": row["source"] or "",
            }
            for row in self._store.conn.execute(
                "SELECT id, slug, portions, source FROM vriezer_gepland ORDER BY id"
            )
        ]

    def confirm(self, pending_id: int) -> None:
        """Een geplande beweging uitvoeren: de echte voorraad wijzigt (de
        datum van een nieuwe portie is vandaag) en de planning vervalt."""
        row = self._store.conn.execute(
            "SELECT slug, portions FROM vriezer_gepland WHERE id = ?", (pending_id,)
        ).fetchone()
        if row is None:
            return
        if int(row["portions"]) > 0:
            self.add(row["slug"], int(row["portions"]))
        else:
            self.take(row["slug"], -int(row["portions"]))
        self._store.conn.execute("DELETE FROM vriezer_gepland WHERE id = ?", (pending_id,))
        self._store.conn.commit()

    def discard(self, pending_id: int) -> None:
        """Een geplande beweging annuleren: het is er niet (in) gegaan."""
        self._store.conn.execute("DELETE FROM vriezer_gepland WHERE id = ?", (pending_id,))
        self._store.conn.commit()

    def planned_out(self, dish_slug: str) -> int:
        """Hoeveel porties van dit gerecht al gepland zijn om gegeten te
        worden maar nog niet bevestigd zijn."""
        row = self._store.conn.execute(
            "SELECT COALESCE(SUM(portions), 0) AS total FROM vriezer_gepland"
            " WHERE slug = ? AND portions < 0",
            (dish_slug,),
        ).fetchone()
        return -int(row["total"])

    def available(self, dish_slug: str) -> int:
        """De voorraad minus wat er al gepland is om gegeten te worden: wat
        er nog daadwerkelijk beloofd kan worden."""
        return self.portions(dish_slug) - self.planned_out(dish_slug)
