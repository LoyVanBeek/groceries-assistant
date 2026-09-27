"""High-level planning flow: propose -> swap -> approve -> order review.

Pure orchestration over the planner engine, the YAML recipe loader and the
SQLite store; no HTTP concerns live here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta

from groceries_assistant.ah.matching import AHMapping, AHMappingStore, suggest_match
from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.planner.engine import REUSE_PENALTY, PlanEntry, WeeklyPlan, plan_week
from groceries_assistant.planner.shopping import ShoppingItem, aggregate_ingredients
from groceries_assistant.preferences import Preferences
from groceries_assistant.store.db import PlanEntryRow, Store

DEFAULT_SERVINGS = 4
DEFAULT_REPEAT_WINDOW_DAYS = 21


class PlanningError(Exception):
    """Invalid planning input."""


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7)


def parse_start(value: str | None, today: date | None = None) -> date:
    if not value:
        return next_monday(today)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlanningError(f"Invalid date (use YYYY-MM-DD): {value}") from exc


def evenings_for(store: Store, start: date, days: int = 7) -> list[EveningAvailability]:
    """Availability from the weekly template, overridden per explicit date.

    Joiners = all configured people minus that day's absences; None when no
    people are configured (then callers fall back to a fixed serving count).
    """
    template = store.week_template()
    people_ids = [p["id"] for p in store.people()]
    out: list[EveningAvailability] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        cook, minutes, allow_leftovers = template[day.weekday()]
        if (override := store.week_override(day)) is not None:
            cook, minutes, allow_leftovers = override
        joiners: tuple[str, ...] | None = None
        if people_ids:
            absent = store.absences(day)
            joiners = tuple(p for p in people_ids if p not in absent)
        out.append(
            EveningAvailability(
                date=day,
                free_minutes=minutes,
                cook=cook or None,
                allow_leftovers=allow_leftovers,
                joiners=joiners,
            )
        )
    return out


def preferences_from_store(store: Store) -> Preferences:
    return Preferences.model_validate(
        {
            "people": [
                {"id": p["id"], "name": p["name"], "role": p["role"], "weight": p["weight"]}
                for p in store.people()
            ],
            "verdicts": store.verdicts(),
        }
    )


def propose(
    recipes: list[Recipe],
    store: Store,
    start: date,
    servings: int = DEFAULT_SERVINGS,
    repeat_window_days: int = DEFAULT_REPEAT_WINDOW_DAYS,
) -> WeeklyPlan:
    """Run the planner for the week starting at `start`."""
    return plan_week(
        recipes,
        preferences_from_store(store),
        evenings_for(store, start),
        history=store.history(),
        repeat_window_days=repeat_window_days,
    )


def _portions(entry: PlanEntry, servings: int) -> int:
    """Joiners of the evening; fixed servings when no people are configured."""
    return entry.availability.people_count or servings


def apply_absences(
    store: Store, rows: list[PlanEntryRow], default: int = DEFAULT_SERVINGS
) -> list[PlanEntryRow]:
    """Recompute people counts from current absences (joins + leftovers).

    Cook evenings get their current joiners; a leftovers evening adds its
    joiners to the source evening. Call before building the order.
    """
    joiners_by_day: dict[date, int | None] = {}
    for row in rows:
        evening = evenings_for(store, row.day, days=1)[0]
        joiners_by_day[row.day] = len(evening.joiners) if evening.joiners is not None else None

    counts: dict[date, int] = {}
    for row in rows:
        if row.kind == "cook":
            counts[row.day] = (
                joiners_by_day[row.day] if joiners_by_day[row.day] is not None else row.people
            ) or default
    for row in rows:
        if row.kind != "leftovers":
            continue
        own = joiners_by_day[row.day]
        own = own if own is not None else (row.people or default)
        source_row = next(
            (
                r
                for r in rows
                if r.kind == "cook" and r.recipe_slug == row.source_slug and r.day < row.day
            ),
            None,
        )
        if source_row is not None:
            counts[source_row.day] = counts.get(source_row.day, default) + own

    out: list[PlanEntryRow] = []
    for row in rows:
        if row.kind == "cook":
            out.append(replace(row, people=counts.get(row.day, row.people)))
        else:
            own = joiners_by_day[row.day]
            out.append(replace(row, people=own if own is not None else row.people))
    return out


def plan_to_rows(plan: WeeklyPlan, servings: int = DEFAULT_SERVINGS) -> list[PlanEntryRow]:
    """Convert a WeeklyPlan to storable rows.

    A cook evening is planned for its joiners; a leftovers evening adds its
    joiners to the evening the dish was cooked on (cook once, eat twice).
    """
    rows: list[PlanEntryRow] = []
    for entry in sorted(plan.entries, key=lambda e: e.date):
        if entry.kind == "cook":
            assert entry.recipe is not None
            rows.append(
                PlanEntryRow(
                    day=entry.date,
                    kind="cook",
                    recipe_slug=entry.recipe.slug,
                    people=_portions(entry, servings),
                )
            )
            continue
        source = next(
            (
                row
                for row in rows
                if row.kind == "cook"
                and row.recipe_slug == entry.source_slug
                and row.day < entry.date
            ),
            None,
        )
        if source is not None:
            source.people += _portions(entry, servings)
        rows.append(
            PlanEntryRow(
                day=entry.date,
                kind="leftovers",
                source_slug=entry.source_slug,
                people=_portions(entry, servings),
            )
        )
    return rows


def rows_to_entries(
    rows: list[PlanEntryRow],
    recipes_by_slug: dict[str, Recipe],
    dishes_by_slug: dict[str, ComposedDish] | None = None,
    picks: dict[date, dict[str, str]] | None = None,
) -> list[PlanEntry]:
    """Rehydrate stored rows into planner entries (dropping deleted recipes).

    'no_cook' evenings (eating elsewhere) carry no dish and no groceries, so
    they are skipped here.

    A row whose slug is a composed dish resolves into that dish's parts, using
    the plan's picks (day -> {part_key: recipe_slug}). Parts without a pick are
    reported in `missing` rather than silently dropped. A dish with nothing
    filled at all falls back to being treated as an unknown slug.
    """
    dishes_by_slug = dishes_by_slug or {}
    picks = picks or {}
    entries: list[PlanEntry] = []
    for row in rows:
        if row.kind == "no_cook":
            continue
        if row.kind == "leftovers":
            entries.append(
                PlanEntry(
                    date=row.day,
                    recipe=None,
                    availability=EveningAvailability(date=row.day, free_minutes=0),
                    minutes=0,
                    kind="leftovers",
                    source_slug=row.source_slug,
                    score=0.0,
                    reason="restjes",
                )
            )
            continue
        dish = dishes_by_slug.get(row.recipe_slug or "")
        if dish is not None:
            entry = _dish_entry(row, dish, picks.get(row.day, {}), recipes_by_slug)
            if entry is not None:
                entries.append(entry)
            continue
        recipe = recipes_by_slug.get(row.recipe_slug or "")
        if recipe is None:
            continue  # recipe deleted since; drop the evening
        entries.append(
            PlanEntry(
                date=row.day,
                recipe=recipe,
                availability=EveningAvailability(
                    date=row.day, free_minutes=recipe.time.same_day_minutes
                ),
                minutes=recipe.time.same_day_minutes,
                score=0.0,
                reason="",
                people=row.people,
            )
        )
    return entries


def _dish_entry(
    row: PlanEntryRow,
    dish: ComposedDish,
    day_picks: dict[str, str],
    recipes_by_slug: dict[str, Recipe],
) -> PlanEntry | None:
    """One evening of a composed dish: the picked parts, plus what is missing."""
    parts: list[Recipe] = []
    missing: list[str] = []
    for part in dish.parts:
        recipe = recipes_by_slug.get(day_picks.get(part.key, ""))
        if recipe is None:
            missing.append(part.label)
        else:
            parts.append(recipe)
    if not parts:
        return None  # nothing picked (yet): no groceries for this evening
    minutes = max(part.time.same_day_minutes for part in parts)
    return PlanEntry(
        date=row.day,
        recipe=None,
        availability=EveningAvailability(date=row.day, free_minutes=minutes),
        minutes=minutes,
        score=0.0,
        reason="",
        people=row.people,
        dish_slug=dish.slug,
        parts=parts,
        missing=missing,
    )


def replace_entry(plan: WeeklyPlan, day: date, recipe: Recipe, prefs: Preferences) -> PlanEntry:
    """Swap one evening's dish manually; warn when it exceeds the budget."""
    entry = next(e for e in plan.entries if e.date == day)
    planned = {e.recipe.slug for e in plan.entries if e.recipe is not None and e.date != day}
    minutes = recipe.time.same_day_minutes if recipe.time.prep_ahead else recipe.time.total_minutes
    score = prefs.recipe_score(recipe.slug) - (REUSE_PENALTY if recipe.slug in planned else 0.0)
    warnings: list[str] = []
    if minutes > entry.availability.free_minutes:
        warnings.append(
            f"{recipe.name} past niet in {entry.availability.free_minutes} min "
            f"(duurt {minutes} min)"
        )
    return PlanEntry(
        date=day,
        recipe=recipe,
        availability=entry.availability,
        score=score,
        minutes=minutes,
        reason=f"handmatig gekozen; {minutes}/{entry.availability.free_minutes} min",
        warnings=warnings,
    )


def replace_with_leftovers(plan: WeeklyPlan, day: date) -> PlanEntry | None:
    """Plan leftovers (yesterday's dish) on one evening."""
    entry = next(e for e in plan.entries if e.date == day)
    earlier = sorted(
        (e for e in plan.entries if e.date < day and e.kind == "cook"),
        key=lambda e: e.date,
    )
    if not earlier or not getattr(earlier[-1], "recipe", None):
        return None
    source = earlier[-1]
    assert source.recipe is not None
    return PlanEntry(
        date=day,
        recipe=None,
        availability=entry.availability,
        score=0.0,
        minutes=0,
        kind="leftovers",
        source_slug=source.recipe.slug,
        reason=f"restjes van '{source.recipe.name}'",
    )


@dataclass
class Order:
    shopping: list[ShoppingItem]
    staples: list[ShoppingItem]
    warnings: list[str]


def _entry_slug(entry: PlanEntry) -> str | None:
    """The slug a plan row points at: a composed dish, or the recipe itself."""
    if entry.dish_slug:
        return entry.dish_slug
    return entry.recipe.slug if entry.recipe is not None else None


def incomplete_dish_warnings(entries: list[PlanEntry]) -> list[str]:
    """Composed dishes that are planned with parts still unfilled.

    Their groceries are not on the list, so say so rather than quietly
    under-buying. Derived from the plan itself, so the order page can show it
    without storing anything.
    """
    warnings: list[str] = []
    for entry in entries:
        if entry.missing:
            name = entry.dish_slug or _entry_slug(entry) or ""
            warnings.append(
                f"{entry.date}: '{name}' zonder {' en '.join(entry.missing)}; "
                "die boodschappen ontbreken op de lijst"
            )
    return warnings


def build_order(
    entries: list[PlanEntry],
    recipes_by_slug: dict[str, Recipe],
    staples: list[str],
    servings: int = DEFAULT_SERVINGS,
) -> Order:
    """Aggregate the planned week into shopping list items.

    Each cook evening is planned for its stored people count (its joiners;
    leftovers evenings already added theirs). Falls back to `servings` when
    entries carry no people count. Staple products are split out.

    A composed dish contributes one entry per chosen part, each scaled by its
    own `servings` -- a 2-person side dish next to a 4-person main stays a
    2-person side dish. A dish with unfilled parts warns instead of quietly
    under-buying.
    """
    entries = sorted(entries, key=lambda e: e.date)
    people: dict[date, int] = {}
    for entry in entries:
        if entry.kind == "cook" and entry.dishes:
            people[entry.date] = entry.people or servings
    for entry in entries:
        if entry.kind != "leftovers" or not entry.source_slug:
            continue
        candidates = [
            e
            for e in entries
            if e.kind == "cook" and _entry_slug(e) == entry.source_slug and e.date < entry.date
        ]
        if candidates:
            source = max(candidates, key=lambda e: e.date)
            people[source.date] = people.get(source.date, servings) + (entry.people or servings)

    work: list[tuple[date, Recipe, int]] = []
    for entry in entries:
        if entry.kind != "cook":
            continue
        count = people.get(entry.date, servings)
        for recipe in entry.dishes:
            work.append((entry.date, recipes_by_slug.get(recipe.slug, recipe), count))

    items, warnings = aggregate_ingredients(work)
    warnings = incomplete_dish_warnings(entries) + warnings
    staple_set = {s.lower() for s in staples}
    shopping: list[ShoppingItem] = []
    staple_items: list[ShoppingItem] = []
    for item in items:
        if item.product.lower() in staple_set:
            staple_items.append(item)
        else:
            shopping.append(item)
    return Order(shopping=shopping, staples=staple_items, warnings=warnings)


def match_products(
    products: list[str],
    search: Callable[[str], list],
    mappings: AHMappingStore,
    min_score: float = 0.45,
) -> dict[str, tuple[int, str]]:
    """Match ingredient product names to AH webshop ids.

    `search(product)` returns AHProduct-like candidates; pass
    AHClient(...).search or a stub in tests. Confirmed pins in the mapping
    store always win; fresh suggestions are cached as unconfirmed.
    """
    matched: dict[str, tuple[int, str]] = {}
    for product in products:
        if (known := mappings.get(product)) is not None:
            matched[product] = (known.ah_webshop_id, known.ah_title)
            continue
        try:
            candidates = search(product)
        except Exception:
            candidates = []
        hit, score = suggest_match(product, candidates, min_score=min_score)
        if hit is None:
            continue
        matched[product] = (hit.webshop_id, hit.title)
        mappings.put(
            AHMapping(
                product=product,
                ah_webshop_id=hit.webshop_id,
                ah_title=hit.title,
                score=score,
            )
        )
    return matched
