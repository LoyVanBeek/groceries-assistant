"""SQLite state store: people, verdicts, week template, staples, plans, history.

Recipes stay as YAML files; everything mutable lives here.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from groceries_assistant.models import ProductFact

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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    approved_through TEXT,                 -- legacy, migrated to plan_approvals
    ordered_through TEXT,                  -- legacy, migrated to plan_ordered
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_entries (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cook',    -- cook | leftovers
    recipe_slug TEXT,                     -- NULL for leftovers
    source_slug TEXT,                     -- leftovers source
    people INTEGER NOT NULL DEFAULT 4,
    frozen INTEGER NOT NULL DEFAULT 0,    -- restjes uit de vriezer: bron is leidend
    course TEXT NOT NULL DEFAULT '',      -- '' = hoofdgerecht-slot; soep/nagerecht/... = extra gang
    PRIMARY KEY (plan_id, day, course)
);

CREATE INDEX IF NOT EXISTS idx_plan_entries_slug ON plan_entries(recipe_slug);

-- Which recipe fills which part of a composed dish, per evening.
-- plan_entries.recipe_slug holds the dish slug for such an evening.
CREATE TABLE IF NOT EXISTS plan_picks (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    part_key TEXT NOT NULL,
    recipe_slug TEXT NOT NULL,
    PRIMARY KEY (plan_id, day, part_key)
);

CREATE INDEX IF NOT EXISTS idx_plan_picks_slug ON plan_picks(recipe_slug);

-- Per-day 'goedkeuren': a day present here is approved (and may be ordered).
-- A single boundary (plans.approved_through) used to track this; the column
-- is kept for the one-time migration below.
CREATE TABLE IF NOT EXISTS plan_approvals (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    PRIMARY KEY (plan_id, day)
);

-- Days whose groceries have actually been ordered (exact per-day set, so a
-- day never looks 'besteld' unless it really was bought).
CREATE TABLE IF NOT EXISTS plan_ordered (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    PRIMARY KEY (plan_id, day)
);

-- Which AH deep-link on /order/link was confirmed 'besteld'. seq is the
-- zero-based index of the link. The approved days lock only once *every*
-- current link is confirmed, so ticking a few links never locks early.
CREATE TABLE IF NOT EXISTS plan_links_confirmed (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    PRIMARY KEY (plan_id, seq)
);

-- What one purchase of a product looks like, and how long the rest keeps.
-- Opt-in: a product without a row is bought as the summed need, as before.
CREATE TABLE IF NOT EXISTS product_facts (
    product TEXT PRIMARY KEY,
    pack_qty REAL NOT NULL,
    pack_unit TEXT NOT NULL DEFAULT 'stuk',
    unit_qty REAL,
    unit_measure TEXT,
    shelf_life_days INTEGER NOT NULL DEFAULT 3
);

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
    frozen: bool = False
    """True for leftovers evenings that eat freezer stock: their source is
    leading and never follows the nearest cook."""
    course: str = ""
    """Which course slot this row fills: '' = the day's main course (the slot
    'Vul de week in' fills); 'soep'/'nagerecht'/... = an extra course on the
    same day. A day always keeps its main row; extra courses are extra rows."""


@dataclass
class ApprovedPlan:
    id: int
    start_date: date
    entries: list[PlanEntryRow]


# Columns that were added to a table after it shipped. `CREATE TABLE IF NOT
# EXISTS` leaves an existing database alone, and ADD COLUMN is the only change
# SQLite can make to a table that already holds rows.
ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "order_items": (("pack_count", "REAL"), ("pack_unit", "TEXT"), ("note", "TEXT")),
    "plans": (("approved_through", "TEXT"), ("ordered_through", "TEXT")),
    "plan_entries": (("frozen", "INTEGER NOT NULL DEFAULT 0"),),
}


class Store:
    """One connection per instance; not thread-safe by itself."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self._migrate_legacy_through_columns()
        self._ensure_default_template()

    def _add_missing_columns(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.conn.commit()
        self._migrate_plan_entries_for_courses()

    def _migrate_plan_entries_for_courses(self) -> None:
        """Widen the plan_entries key from (plan_id, day) to (plan_id, day, course).

        A day can hold several courses (soep + hoofdgerecht + nagerecht), one
        row each; the main-course slot keeps course ''. SQLite cannot change a
        primary key in place, so the table is rebuilt once.
        """
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(plan_entries)")}
        if "course" in have:
            return
        self.conn.execute("ALTER TABLE plan_entries RENAME TO plan_entries_old")
        self.conn.execute(
            """CREATE TABLE plan_entries (
                plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'cook',
                recipe_slug TEXT,
                source_slug TEXT,
                people INTEGER NOT NULL DEFAULT 4,
                frozen INTEGER NOT NULL DEFAULT 0,
                course TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (plan_id, day, course)
            )"""
        )
        self.conn.execute(
            "INSERT INTO plan_entries"
            " (plan_id, day, kind, recipe_slug, source_slug, people, frozen, course)"
            " SELECT plan_id, day, kind, recipe_slug, source_slug, people, frozen, ''"
            " FROM plan_entries_old"
        )
        self.conn.execute("DROP TABLE plan_entries_old")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plan_entries_slug ON plan_entries(recipe_slug)"
        )
        self.conn.commit()

    def _ensure_default_template(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM week_template").fetchone()[0] == 0:
            for weekday, (cook, minutes, leftovers) in DEFAULT_TEMPLATE.items():
                self.set_evening(
                    weekday, cook=cook, free_minutes=minutes, allow_leftovers=leftovers
                )

    def _migrate_legacy_through_columns(self) -> None:
        """Fold the old approved_through/ordered_through boundaries into per-day
        plan_approvals/plan_ordered, then clear them.

        Runs once against DBs that still carry the legacy columns, so a plan
        that was already approved+ordered keeps its state.
        """
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(plans)")}
        if "approved_through" not in have:
            return
        cur = self.conn.cursor()
        for plan_id, through in cur.execute(
            "SELECT id, approved_through FROM plans WHERE approved_through IS NOT NULL"
        ):
            days = [
                row[0]
                for row in cur.execute(
                    "SELECT day FROM plan_entries WHERE plan_id = ? AND day <= ?",
                    (plan_id, through),
                ).fetchall()
            ]
            for day in days:
                cur.execute(
                    "INSERT OR IGNORE INTO plan_approvals(plan_id, day) VALUES (?,?)",
                    (plan_id, day),
                )
            cur.execute("UPDATE plans SET approved_through = NULL WHERE id = ?", (plan_id,))
        for plan_id, through in cur.execute(
            "SELECT id, ordered_through FROM plans WHERE ordered_through IS NOT NULL"
        ):
            days = [
                row[0]
                for row in cur.execute(
                    "SELECT day FROM plan_approvals WHERE plan_id = ? AND day <= ?",
                    (plan_id, through),
                ).fetchall()
            ]
            for day in days:
                cur.execute(
                    "INSERT OR IGNORE INTO plan_ordered(plan_id, day) VALUES (?,?)",
                    (plan_id, day),
                )
            cur.execute("UPDATE plans SET ordered_through = NULL WHERE id = ?", (plan_id,))
        self.conn.commit()

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
        cur.execute(
            "UPDATE plan_picks SET recipe_slug = ? WHERE recipe_slug = ?", (new_slug, old_slug)
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

    # --- app settings -----------------------------------------------------------

    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def week_start_weekday(self) -> int:
        """The weekday a new week starts on; 0 = Monday. The plan anchors here."""
        return int(self.setting("week_start_weekday", "1"))

    def set_week_start_weekday(self, weekday: int) -> None:
        if weekday not in range(7):
            raise ValueError(f"weekday must be 0-6: {weekday}")
        self.set_setting("week_start_weekday", str(weekday))

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

    def save_proposal(
        self,
        start: date,
        entries: list[PlanEntryRow],
        picks: dict[date, dict[str, str]] | None = None,
    ) -> int:
        """Replace the active proposal (plans with status 'proposed').

        `picks` are the parts the planner chose for its composed dishes: a
        proposal lands fully formed, and `/plan/picks` can overwrite them.
        """
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
                "INSERT INTO plan_entries"
                "(plan_id, day, kind, recipe_slug, source_slug, people, frozen, course)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                    int(entry.frozen),
                    entry.course,
                ),
            )
        for day, day_picks in (picks or {}).items():
            for part_key, recipe_slug in day_picks.items():
                if not recipe_slug:
                    continue
                cur.execute(
                    "INSERT INTO plan_picks(plan_id, day, part_key, recipe_slug) VALUES (?,?,?,?)",
                    (plan_id, day.isoformat(), part_key, recipe_slug),
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

    def clear_future_plans(self, today: date) -> int:
        """Delete every plan covering `today` or starting later.

        This week and all planned-ahead weeks are wiped (entries, picks,
        approvals, ordered days and order items cascade along); plans that
        ended before today keep the history intact.
        """
        cutoff = (today - timedelta(days=6)).isoformat()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plans WHERE start_date >= ?", (cutoff,))
        deleted = cur.rowcount
        self.conn.commit()
        return deleted

    def approve_proposal(self, plan_id: int) -> None:
        self.conn.execute("UPDATE plans SET status = 'approved' WHERE id = ?", (plan_id,))
        self.conn.commit()

    def update_proposal_rows(self, plan_id: int, entries: list[PlanEntryRow]) -> None:
        """Replace the entries of one proposal (after swaps)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plan_entries WHERE plan_id = ?", (plan_id,))
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries"
                "(plan_id, day, kind, recipe_slug, source_slug, people, frozen, course)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                    int(entry.frozen),
                    entry.course,
                ),
            )
        self.conn.commit()

    def shift_proposal(self, plan_id: int, days: int) -> None:
        """Move a proposal's start, entries, picks and approvals forward by `days`.

        Entries, part picks and approvals are keyed to the same absolute dates,
        so the tables have to move together or the plan would otherwise fall
        apart. Meals keep their weekday only when `days` is a multiple of 7;
        otherwise the weekly template (cook, minutes, leftovers) rotates with
        the calendar. Picks, swaps and approvals are kept as-is.
        """
        delta = timedelta(days=days)
        cur = self.conn.cursor()
        start_row = cur.execute("SELECT start_date FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if start_row is None:
            raise KeyError(f"no plan {plan_id}")
        cur.execute(
            "UPDATE plans SET start_date = ? WHERE id = ?",
            ((date.fromisoformat(start_row["start_date"]) + delta).isoformat(), plan_id),
        )
        # Update latest days first so a shifted row never collides with a not
        # yet moved one (all keyed by (plan, day)).
        for table in ("plan_entries", "plan_picks", "plan_approvals", "plan_ordered"):
            rows = cur.execute(
                f"SELECT day FROM {table} WHERE plan_id = ? ORDER BY day DESC",
                (plan_id,),
            ).fetchall()
            for (old_day,) in rows:
                cur.execute(
                    f"UPDATE {table} SET day = ? WHERE plan_id = ? AND day = ?",
                    ((date.fromisoformat(old_day) + delta).isoformat(), plan_id, old_day),
                )
        self.conn.commit()

    def extend_proposal(
        self,
        plan_id: int,
        entries: list[PlanEntryRow],
        picks: dict[date, dict[str, str]] | None = None,
    ) -> None:
        """Append days (and their part picks) to an existing proposal.

        The proposal is a continuous run of days; a new week lands right after
        the last one. `plan_entries` and `plan_picks` are keyed by (plan, day),
        so appending cannot collide as long as the caller starts past the last
        day (the /plan/advance flow always does).
        """
        cur = self.conn.cursor()
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries"
                "(plan_id, day, kind, recipe_slug, source_slug, people, frozen, course)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                    int(entry.frozen),
                    entry.course,
                ),
            )
        for day, day_picks in (picks or {}).items():
            for part_key, recipe_slug in day_picks.items():
                if not recipe_slug:
                    continue
                cur.execute(
                    "INSERT INTO plan_picks(plan_id, day, part_key, recipe_slug) VALUES (?,?,?,?)",
                    (plan_id, day.isoformat(), part_key, recipe_slug),
                )
        self.conn.commit()

    def set_day_approved(self, plan_id: int, day: date, approved: bool) -> None:
        """Approve (or revoke approval of) a single day."""
        if approved:
            self.conn.execute(
                "INSERT OR IGNORE INTO plan_approvals(plan_id, day) VALUES (?,?)",
                (plan_id, day.isoformat()),
            )
        else:
            self.conn.execute(
                "DELETE FROM plan_approvals WHERE plan_id = ? AND day = ?",
                (plan_id, day.isoformat()),
            )
        self.conn.commit()

    def plan_approvals(self, plan_id: int) -> set[date]:
        rows = self.conn.execute(
            "SELECT day FROM plan_approvals WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        return {date.fromisoformat(row["day"]) for row in rows}

    def set_day_ordered(self, plan_id: int, day: date, ordered: bool) -> None:
        """Mark a single day's groceries as ordered (or undo the mark)."""
        if ordered:
            self.conn.execute(
                "INSERT OR IGNORE INTO plan_ordered(plan_id, day) VALUES (?,?)",
                (plan_id, day.isoformat()),
            )
        else:
            self.conn.execute(
                "DELETE FROM plan_ordered WHERE plan_id = ? AND day = ?",
                (plan_id, day.isoformat()),
            )
        self.conn.commit()

    def plan_ordered(self, plan_id: int) -> set[date]:
        rows = self.conn.execute(
            "SELECT day FROM plan_ordered WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        return {date.fromisoformat(row["day"]) for row in rows}

    def set_link_confirmed(self, plan_id: int, seq: int) -> None:
        """Record that the order behind one AH link (seq) was placed."""
        self.conn.execute(
            "INSERT OR IGNORE INTO plan_links_confirmed(plan_id, seq) VALUES (?,?)",
            (plan_id, seq),
        )
        self.conn.commit()

    def plan_links_confirmed(self, plan_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT seq FROM plan_links_confirmed WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        return {row["seq"] for row in rows}

    def clear_links_confirmed(self, plan_id: int) -> None:
        """Forget the per-link ticks (called when the days get locked)."""
        self.conn.execute("DELETE FROM plan_links_confirmed WHERE plan_id = ?", (plan_id,))
        self.conn.commit()

    def swap_days(self, plan_id: int, day_a: date, day_b: date) -> None:
        """Exchange the full content of two evenings.

        Every row of the day travels — the dish, any extra courses, part
        picks, approval and order-confirmation — so a confirmed recipe keeps
        its bought groceries no matter which date it ends up on. The rows move
        as wholes (via a temporary day, so the (plan, day, course) key never
        collides). Raises KeyError when a day is not part of the plan.
        """
        if day_a == day_b:
            return
        cur = self.conn.cursor()
        days = {
            r["day"]
            for r in cur.execute("SELECT day FROM plan_entries WHERE plan_id = ?", (plan_id,))
        }
        if day_a.isoformat() not in days or day_b.isoformat() not in days:
            missing = day_a if day_a.isoformat() not in days else day_b
            raise KeyError(f"plan {plan_id} has no day {missing.isoformat()}")
        tmp = "__swap_tmp__"
        for table in ("plan_entries", "plan_picks", "plan_approvals", "plan_ordered"):
            cur.execute(
                f"UPDATE {table} SET day=? WHERE plan_id=? AND day=?",
                (tmp, plan_id, day_a.isoformat()),
            )
            cur.execute(
                f"UPDATE {table} SET day=? WHERE plan_id=? AND day=?",
                (day_a.isoformat(), plan_id, day_b.isoformat()),
            )
            cur.execute(
                f"UPDATE {table} SET day=? WHERE plan_id=? AND day=?",
                (day_b.isoformat(), plan_id, tmp),
            )
        # Restjes dat door de ruil vóór zijn gerecht zou komen: wissen — de
        # dag wordt een lege kookavond (nog te kiezen), zijn goedkeuring
        # vervalt mee.
        dish_days: dict[str, list[date]] = {}
        for r in self.conn.execute(
            "SELECT day, recipe_slug FROM plan_entries"
            " WHERE plan_id=? AND kind='cook' AND recipe_slug IS NOT NULL",
            (plan_id,),
        ):
            dish_days.setdefault(r["recipe_slug"], []).append(date.fromisoformat(r["day"]))
        restjes_rows = self.conn.execute(
            "SELECT day, source_slug FROM plan_entries"
            " WHERE plan_id=? AND kind='leftovers' AND source_slug IS NOT NULL AND frozen=0",
            (plan_id,),
        ).fetchall()
        for row in restjes_rows:
            restjes_day = date.fromisoformat(row["day"])
            source_days = dish_days.get(row["source_slug"], [])
            # alleen wissen als géén enkele keer koken van het gerecht op of
            # vóór de restjesdag valt
            if source_days and all(d > restjes_day for d in source_days):
                self.conn.execute(
                    "UPDATE plan_entries SET kind='cook', recipe_slug=NULL,"
                    " source_slug=NULL WHERE plan_id=? AND day=? AND course=''",
                    (plan_id, row["day"]),
                )
                self.conn.execute(
                    "DELETE FROM plan_approvals WHERE plan_id=? AND day=?",
                    (plan_id, row["day"]),
                )
                self.conn.execute(
                    "DELETE FROM plan_ordered WHERE plan_id=? AND day=?",
                    (plan_id, row["day"]),
                )
        self.conn.commit()

    def add_restjes(self, plan_id: int, source_day: date, day: date) -> None:
        """Plan the restjes of `source_day`'s dish on a later, empty day.

        The source must be a cooked evening with a dish, the target must be
        later and still without a dish. Raises ValueError otherwise.
        """
        entries = {
            date.fromisoformat(r["day"]): r
            for r in self.conn.execute(
                "SELECT day, kind, recipe_slug, source_slug FROM plan_entries"
                " WHERE plan_id=? AND course=''",
                (plan_id,),
            )
        }
        source = entries.get(source_day)
        target = entries.get(day)
        if (
            source is None
            or source["kind"] != "cook"
            or not source["recipe_slug"]
            or target is None
            or target["kind"] != "cook"
            or target["recipe_slug"] is not None
        ):
            raise ValueError("restjes kan alleen op een latere, lege kookavond")
        self.conn.execute(
            "UPDATE plan_entries SET kind='leftovers', recipe_slug=NULL,"
            " source_slug=? WHERE plan_id=? AND day=? AND course=''",
            (source["recipe_slug"], plan_id, day.isoformat()),
        )
        self.conn.execute(
            "DELETE FROM plan_approvals WHERE plan_id=? AND day=?",
            (plan_id, day.isoformat()),
        )
        self.conn.commit()

    def clear_restjes_day(self, plan_id: int, day: date) -> None:
        """Erase the restjes on `day`: the evening becomes empty again."""
        self.conn.execute(
            "UPDATE plan_entries SET kind='cook', recipe_slug=NULL, source_slug=NULL"
            " WHERE plan_id=? AND day=? AND course=''",
            (plan_id, day.isoformat()),
        )
        self.conn.execute(
            "DELETE FROM plan_approvals WHERE plan_id=? AND day=?",
            (plan_id, day.isoformat()),
        )
        self.conn.commit()

    def approve_plan(self, start: date, entries: list[PlanEntryRow]) -> int:
        cur = self.conn.cursor()
        cur.execute("INSERT INTO plans(start_date) VALUES (?)", (start.isoformat(),))
        plan_id = cur.lastrowid
        assert plan_id is not None
        for entry in entries:
            cur.execute(
                "INSERT INTO plan_entries"
                "(plan_id, day, kind, recipe_slug, source_slug, people, frozen, course)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    entry.day.isoformat(),
                    entry.kind,
                    entry.recipe_slug,
                    entry.source_slug,
                    entry.people,
                    int(entry.frozen),
                    entry.course,
                ),
            )
        self.conn.commit()
        return plan_id

    def history(self) -> dict[str, date]:
        """slug -> most recent day this slug was planned for.

        A composed dish is cooked as its parts, so the picks count as history
        too: a component that filled a part last week was still cooked last
        week, and the repetition penalty is about the food, not the label.
        """
        rows = self.conn.execute(
            "SELECT recipe_slug, MAX(day) AS last_day FROM ("
            " SELECT recipe_slug, day FROM plan_entries WHERE recipe_slug IS NOT NULL"
            " UNION ALL"
            " SELECT recipe_slug, day FROM plan_picks"
            ") GROUP BY recipe_slug"
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

    def previous_plan(self, before: int) -> tuple[int, date] | None:
        """The approved plan before `before`; None when this is the first week.

        What last week bought and used is what is still in the fridge, so this
        is where the leftovers of a new week come from.
        """
        row = self.conn.execute(
            "SELECT id, start_date FROM plans WHERE id < ? AND status = 'approved'"
            " ORDER BY id DESC LIMIT 1",
            (int(before),),
        ).fetchone()
        if row is None:
            return None
        return row["id"], date.fromisoformat(row["start_date"])

    def plan_entries(self, plan_id: int) -> list[PlanEntryRow]:
        rows = self.conn.execute(
            "SELECT day, kind, recipe_slug, source_slug, people, frozen, course"
            " FROM plan_entries"
            " WHERE plan_id = ? ORDER BY day,"
            " CASE course WHEN 'soep' THEN 0 WHEN 'voorgerecht' THEN 1"
            " WHEN '' THEN 2 WHEN 'bijgerecht' THEN 3 WHEN 'nagerecht' THEN 4 ELSE 5 END",
            (plan_id,),
        ).fetchall()
        return [
            PlanEntryRow(
                day=date.fromisoformat(row["day"]),
                kind=row["kind"],
                recipe_slug=row["recipe_slug"],
                source_slug=row["source_slug"],
                people=row["people"],
                frozen=bool(row["frozen"]),
                course=row["course"],
            )
            for row in rows
        ]

    # --- composed-dish picks -------------------------------------------------------

    def plan_picks(self, plan_id: int) -> dict[date, dict[str, str]]:
        """day -> {part_key: recipe_slug}; only for composed-dish evenings."""
        rows = self.conn.execute(
            "SELECT day, part_key, recipe_slug FROM plan_picks WHERE plan_id = ? ORDER BY day",
            (plan_id,),
        ).fetchall()
        picks: dict[date, dict[str, str]] = {}
        for row in rows:
            picks.setdefault(date.fromisoformat(row["day"]), {})[row["part_key"]] = row[
                "recipe_slug"
            ]
        return picks

    def set_pick(self, plan_id: int, day: date, part_key: str, recipe_slug: str) -> None:
        """Set (or clear, with an empty slug) the recipe filling one part."""
        if recipe_slug:
            self.conn.execute(
                "INSERT INTO plan_picks(plan_id, day, part_key, recipe_slug) VALUES (?,?,?,?)"
                " ON CONFLICT(plan_id, day, part_key) DO UPDATE SET"
                " recipe_slug=excluded.recipe_slug",
                (plan_id, day.isoformat(), part_key, recipe_slug),
            )
        else:
            self.clear_pick(plan_id, day, part_key)
        self.conn.commit()

    def clear_pick(self, plan_id: int, day: date, part_key: str) -> None:
        self.conn.execute(
            "DELETE FROM plan_picks WHERE plan_id = ? AND day = ? AND part_key = ?",
            (plan_id, day.isoformat(), part_key),
        )
        self.conn.commit()

    def clear_day_picks(self, plan_id: int, day: date) -> None:
        """Forget every pick of one evening (its dish changed or is gone)."""
        self.conn.execute(
            "DELETE FROM plan_picks WHERE plan_id = ? AND day = ?", (plan_id, day.isoformat())
        )
        self.conn.commit()

    def set_picks(self, plan_id: int, day: date, picks: dict[str, str]) -> None:
        """Replace every pick of one evening in one transaction."""
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM plan_picks WHERE plan_id = ? AND day = ?", (plan_id, day.isoformat())
        )
        for part_key, recipe_slug in picks.items():
            if recipe_slug:
                cur.execute(
                    "INSERT INTO plan_picks(plan_id, day, part_key, recipe_slug) VALUES (?,?,?,?)",
                    (plan_id, day.isoformat(), part_key, recipe_slug),
                )
        self.conn.commit()

    # --- product facts --------------------------------------------------------------

    def product_facts(self) -> dict[str, ProductFact]:
        """Every declared fact, by lowercased product name."""
        rows = self.conn.execute(
            "SELECT product, pack_qty, pack_unit, unit_qty, unit_measure, shelf_life_days"
            " FROM product_facts ORDER BY product"
        ).fetchall()
        return {
            row["product"].lower(): ProductFact(
                product=row["product"],
                pack_qty=row["pack_qty"],
                pack_unit=row["pack_unit"],
                unit_qty=row["unit_qty"],
                unit_measure=row["unit_measure"],
                shelf_life_days=row["shelf_life_days"],
            )
            for row in rows
        }

    def product_fact(self, product: str) -> ProductFact | None:
        return self.product_facts().get(product.lower())

    def set_product_fact(self, fact: ProductFact) -> None:
        """Declare (or replace) how one purchase of a product looks.

        Product names are matched case-insensitively (recipes are written in
        whatever case), so an older differently-cased row is replaced, not
        shadowed.
        """
        self.conn.execute(
            "DELETE FROM product_facts WHERE lower(product) = ?", (fact.product.lower(),)
        )
        self.conn.execute(
            "INSERT INTO product_facts"
            " (product, pack_qty, pack_unit, unit_qty, unit_measure, shelf_life_days)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(product) DO UPDATE SET pack_qty=excluded.pack_qty,"
            " pack_unit=excluded.pack_unit, unit_qty=excluded.unit_qty,"
            " unit_measure=excluded.unit_measure, shelf_life_days=excluded.shelf_life_days",
            (
                fact.product.strip(),
                fact.pack_qty,
                fact.pack_unit.strip(),
                fact.unit_qty,
                fact.unit_measure.strip() if fact.unit_measure else None,
                fact.shelf_life_days,
            ),
        )
        self.conn.commit()

    def delete_product_fact(self, product: str) -> None:
        """Forget a fact; the product goes back to being bought as its need."""
        self.conn.execute("DELETE FROM product_facts WHERE lower(product) = ?", (product.lower(),))
        self.conn.commit()

    # --- order review ------------------------------------------------------------

    def save_order_items(self, plan_id: int, items: list[tuple]) -> None:
        """Replace the review items for a plan.

        Each item is (product, unit, amount, webshop_id, pack_count, pack_unit,
        note): the demand-unit quantity, plus how many packs that is and what
        they add up to, for a product bought in whole packs.

        The 'in huis' tick (`have`) is carried over for products that stay on
        the list: rebuilding the list (after a dish change or a re-order) must
        never silently put back items the user already has.
        """
        cur = self.conn.cursor()
        have_now = {
            (row["product"], row["unit"]): row["have"]
            for row in cur.execute(
                "SELECT product, unit, have FROM order_items WHERE plan_id = ?", (plan_id,)
            )
        }
        cur.execute("DELETE FROM order_items WHERE plan_id = ?", (plan_id,))
        for product, unit, amount, webshop_id, pack_count, pack_unit, note in items:
            cur.execute(
                "INSERT INTO order_items"
                " (plan_id, product, unit, amount, webshop_id, have, pack_count, pack_unit, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    product,
                    unit,
                    amount,
                    webshop_id,
                    have_now.get((product, unit), 0),
                    pack_count,
                    pack_unit,
                    note,
                ),
            )
        self.conn.commit()

    def add_order_item(
        self, plan_id: int, product: str, unit: str = "stuk", amount: float = 1.0
    ) -> bool:
        """One extra item on the plan's list (a scanned package); False when
        that product+unit was already on it.

        Note: a rebuild of the list (approve/order/next week) does not carry
        extras along — scan them again for the new week.
        """
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO order_items (plan_id, product, unit, amount)"
            " VALUES (?,?,?,?) ON CONFLICT(plan_id, product, unit) DO NOTHING",
            (plan_id, product.strip(), unit, amount),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def order_items(self, plan_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT product, unit, amount, webshop_id, have, pack_count, pack_unit, note"
            " FROM order_items WHERE plan_id = ? ORDER BY product",
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
