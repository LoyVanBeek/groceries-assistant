"""SQLite state store: people, verdicts, week template, staples, plans, history.

Recipes stay as YAML files; everything mutable lives here.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'adult',
    weight REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS verdicts (
    recipe_slug TEXT NOT NULL,
    person_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('love','ok','dislike','never')),
    PRIMARY KEY (recipe_slug, person_id)
);

CREATE TABLE IF NOT EXISTS week_template (
    weekday INTEGER PRIMARY KEY,          -- 0 = Monday
    cook TEXT NOT NULL DEFAULT '',
    free_minutes INTEGER NOT NULL DEFAULT 45,
    allow_leftovers INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS week_override (
    day TEXT PRIMARY KEY,                 -- ISO date
    cook TEXT NOT NULL DEFAULT '',
    free_minutes INTEGER NOT NULL,
    allow_leftovers INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS staples (
    product TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS absences (
    day TEXT NOT NULL,                    -- ISO date
    person_id TEXT NOT NULL,
    PRIMARY KEY (day, person_id)
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'approved',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_entries (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cook',    -- cook | leftovers
    recipe_slug TEXT,                     -- NULL for leftovers
    source_slug TEXT,                     -- leftovers source
    people INTEGER NOT NULL DEFAULT 4,
    PRIMARY KEY (plan_id, day)
);

CREATE INDEX IF NOT EXISTS idx_plan_entries_slug ON plan_entries(recipe_slug);

CREATE TABLE IF NOT EXISTS order_items (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    product TEXT NOT NULL,
    unit TEXT NOT NULL,
    amount REAL NOT NULL,
    webshop_id INTEGER,                   -- NULL when not matched to AH
    have INTEGER NOT NULL DEFAULT 0,      -- ticked off in the review screen
    PRIMARY KEY (plan_id, product, unit)
);
"""

DEFAULT_TEMPLATE: dict[int, tuple[str, int, bool]] = {
    0: ("", 25, True),  # Monday: cramped, leftovers allowed
    1: ("", 45, False),
    2: ("", 45, False),
    3: ("", 45, False),
    4: ("", 45, False),
    5: ("", 45, False),
    6: ("", 90, False),  # Sunday: relaxed, meal prep
}


@dataclass
class PlanEntryRow:
    day: date
    kind: str = "cook"
    recipe_slug: str | None = None
    source_slug: str | None = None
    people: int = 4


@dataclass
class ApprovedPlan:
    id: int
    start_date: date
    entries: list[PlanEntryRow]


class Store:
    """One connection per instance; not thread-safe by itself."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._ensure_default_template()

    def _ensure_default_template(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM week_template").fetchone()[0] == 0:
            for weekday, (cook, minutes, leftovers) in DEFAULT_TEMPLATE.items():
                self.set_evening(
                    weekday, cook=cook, free_minutes=minutes, allow_leftovers=leftovers
                )

    def close(self) -> None:
        self.conn.close()

    # --- people & verdicts ------------------------------------------------------

    def people(self) -> list[dict]:
        rows = self.conn.execute("SELECT id, name, role, weight FROM people ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def upsert_person(self, person_id: str, name: str, role: str, weight: float) -> None:
        self.conn.execute(
            "INSERT INTO people(id, name, role, weight) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role,"
            " weight=excluded.weight",
            (person_id, name, role, weight),
        )
        self.conn.commit()

    def set_verdict(self, recipe_slug: str, person_id: str, verdict: str) -> None:
        self.conn.execute(
            "INSERT INTO verdicts(recipe_slug, person_id, verdict) VALUES (?,?,?)"
            " ON CONFLICT(recipe_slug, person_id) DO UPDATE SET verdict=excluded.verdict",
            (recipe_slug, person_id, verdict),
        )
        self.conn.commit()

    def verdicts(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for slug, person, verdict in self.conn.execute(
            "SELECT recipe_slug, person_id, verdict FROM verdicts"
        ):
            out.setdefault(slug, {})[person] = verdict
        return out

    def clear_verdict(self, recipe_slug: str, person_id: str) -> None:
        self.conn.execute(
            "DELETE FROM verdicts WHERE recipe_slug = ? AND person_id = ?",
            (recipe_slug, person_id),
        )
        self.conn.commit()

    def rename_recipe(self, old_slug: str, new_slug: str) -> None:
        """Re-point verdicts and plan references after a rename."""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE verdicts SET recipe_slug = ? WHERE recipe_slug = ?", (new_slug, old_slug)
        )
        cur.execute(
            "UPDATE plan_entries SET recipe_slug = ? WHERE recipe_slug = ?",
            (new_slug, old_slug),
        )
        cur.execute(
            "UPDATE plan_entries SET source_slug = ? WHERE source_slug = ?",
            (new_slug, old_slug),
        )
        self.conn.commit()

    # --- absences ("not joining for dinner") -------------------------------------

    def absences(self, day: date) -> set[str]:
        rows = self.conn.execute("SELECT person_id FROM absences WHERE day = ?", (day.isoformat(),))
        return {row[0] for row in rows}

    def set_absence(self, day: date, person_id: str, absent: bool) -> None:
        if absent:
            self.conn.execute(
                "INSERT OR IGNORE INTO absences(day, person_id) VALUES (?,?)",
                (day.isoformat(), person_id),
            )
        else:
            self.conn.execute(
                "DELETE FROM absences WHERE day = ? AND person_id = ?",
                (day.isoformat(), person_id),
            )
        self.conn.commit()

    # --- weekly template ---------------------------------------------------------

    def week_template(self) -> dict[int, tuple[str, int, bool]]:
        rows = self.conn.execute(
            "SELECT weekday, cook, free_minutes, allow_leftovers FROM week_template"
        ).fetchall()
        return {
            row["weekday"]: (row["cook"], row["free_minutes"], bool(row["allow_leftovers"]))
            for row in rows
        }

    def set_evening(
        self, weekday: int, cook: str, free_minutes: int, allow_leftovers: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO week_template(weekday, cook, free_minutes, allow_leftovers)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(weekday) DO UPDATE SET cook=excluded.cook,"
            " free_minutes=excluded.free_minutes, allow_leftovers=excluded.allow_leftovers",
            (weekday, cook, free_minutes, int(allow_leftovers)),
        )
        self.conn.commit()

    def week_override(self, day: date) -> tuple[str, int, bool] | None:
        row = self.conn.execute(
            "SELECT cook, free_minutes, allow_leftovers FROM week_override WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        return row["cook"], row["free_minutes"], bool(row["allow_leftovers"])

    def set_week_override(
        self, day: date, cook: str, free_minutes: int, allow_leftovers: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO week_override(day, cook, free_minutes, allow_leftovers)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET cook=excluded.cook,"
            " free_minutes=excluded.free_minutes, allow_leftovers=excluded.allow_leftovers",
            (day.isoformat(), cook, free_minutes, int(allow_leftovers)),
        )
        self.conn.commit()

    # --- staples -----------------------------------------------------------------

    def staples(self) -> list[str]:
        return [row[0] for row in self.conn.execute("SELECT product FROM staples ORDER BY product")]

    def set_staple(self, product: str, on: bool) -> None:
        if on:
            self.conn.execute("INSERT OR IGNORE INTO staples(product) VALUES (?)", (product,))
        else:
            self.conn.execute("DELETE FROM staples WHERE product = ?", (product,))
        self.conn.commit()

    # --- proposals & approved plans ---------------------------------------------------

    def save_proposal(self, start: date, entries: list[PlanEntryRow]) -> int:
        """Replace the active proposal (plans with status 'proposed')."""
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM plans WHERE status = 'proposed'")
        for (old_id,) in cur.fetchall():
            cur.execute("DELETE FROM plan_entries WHERE plan_id = ?", (old_id,))
            cur.execute("DELETE FROM plans WHERE id = ?", (old_id,))
        cur.execute(
            "INSERT INTO plans(start_date, status) VALUES (?, 'proposed')", (start.isoformat(),)
        )
        plan_id = cur.lastrowid
        assert plan_id is not None
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries(plan_id, day, kind, recipe_slug, source_slug, people)"
                " VALUES (?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                ),
            )
        self.conn.commit()
        return plan_id

    def load_proposal(self) -> tuple[int, date, list[PlanEntryRow]] | None:
        row = self.conn.execute(
            "SELECT id, start_date FROM plans WHERE status = 'proposed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        plan_id = row["id"]
        return plan_id, date.fromisoformat(row["start_date"]), self.plan_entries(plan_id)

    def approve_proposal(self, plan_id: int) -> None:
        self.conn.execute("UPDATE plans SET status = 'approved' WHERE id = ?", (plan_id,))
        self.conn.commit()

    def update_proposal_rows(self, plan_id: int, entries: list[PlanEntryRow]) -> None:
        """Replace the entries of one proposal (after swaps)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plan_entries WHERE plan_id = ?", (plan_id,))
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries(plan_id, day, kind, recipe_slug, source_slug, people)"
                " VALUES (?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                ),
            )
        self.conn.commit()

    def approve_plan(self, start: date, entries: list[PlanEntryRow]) -> int:
        cur = self.conn.cursor()
        cur.execute("INSERT INTO plans(start_date) VALUES (?)", (start.isoformat(),))
        plan_id = cur.lastrowid
        assert plan_id is not None
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries(plan_id, day, kind, recipe_slug, source_slug, people)"
                " VALUES (?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                ),
            )
        self.conn.commit()
        return plan_id

    def history(self) -> dict[str, date]:
        """slug -> most recent planned day (approved plans only)."""
        rows = self.conn.execute(
            "SELECT recipe_slug, MAX(day) AS last_day FROM plan_entries"
            " WHERE recipe_slug IS NOT NULL GROUP BY recipe_slug"
        ).fetchall()
        return {row["recipe_slug"]: date.fromisoformat(row["last_day"]) for row in rows}

    def latest_approved(self) -> tuple[int, date] | None:
        row = self.conn.execute(
            "SELECT id, start_date FROM plans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return row["id"], date.fromisoformat(row["start_date"])

    def latest_plan(self) -> tuple[int, date, str] | None:
        """Most recent plan regardless of status; drives the order flow."""
        row = self.conn.execute(
            "SELECT id, start_date, status FROM plans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return row["id"], date.fromisoformat(row["start_date"]), row["status"]

    def plan_entries(self, plan_id: int) -> list[PlanEntryRow]:
        rows = self.conn.execute(
            "SELECT day, kind, recipe_slug, source_slug, people FROM plan_entries"
            " WHERE plan_id = ? ORDER BY day",
            (plan_id,),
        ).fetchall()
        return [
            PlanEntryRow(
                day=date.fromisoformat(row["day"]),
                kind=row["kind"],
                recipe_slug=row["recipe_slug"],
                source_slug=row["source_slug"],
                people=row["people"],
            )
            for row in rows
        ]

    # --- order review ------------------------------------------------------------

    def save_order_items(
        self, plan_id: int, items: list[tuple[str, str, float, int | None]]
    ) -> None:
        """Replace the review items for a plan: (product, unit, amount, webshop_id)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM order_items WHERE plan_id = ?", (plan_id,))
        for product, unit, amount, webshop_id in items:
            cur.execute(
                "INSERT INTO order_items(plan_id, product, unit, amount, webshop_id, have)"
                " VALUES (?,?,?,?,?,0)",
                (plan_id, product, unit, amount, webshop_id),
            )
        self.conn.commit()

    def order_items(self, plan_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT product, unit, amount, webshop_id, have FROM order_items"
            " WHERE plan_id = ? ORDER BY product",
            (plan_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_order_item_have(self, plan_id: int, product: str, unit: str, have: bool) -> None:
        self.conn.execute(
            "UPDATE order_items SET have = ? WHERE plan_id = ? AND product = ? AND unit = ?",
            (int(have), plan_id, product, unit),
        )
        self.conn.commit()

    def set_order_item_webshop_id(
        self, plan_id: int, product: str, unit: str, webshop_id: int | None
    ) -> None:
        self.conn.execute(
            "UPDATE order_items SET webshop_id = ? WHERE plan_id = ? AND product = ? AND unit = ?",
            (webshop_id, plan_id, product, unit),
        )
        self.conn.commit()
