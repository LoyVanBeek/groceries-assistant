"""View builders and small helpers for the web UI."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.preferences import Preferences
from groceries_assistant.recipes.loader import load_recipes_with_errors
from groceries_assistant.services import planner_service as planner
from groceries_assistant.services.planning import alternatives
from groceries_assistant.store.db import Store

WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


def load_recipes(data_dir: Path) -> tuple[list[Recipe], list[str]]:
    return load_recipes_with_errors(data_dir / "recipes")


def cook_view(stored_cook: str, members: list[dict], joiners: list[str]) -> dict:
    """Cook inference for one evening.

    Only adults who join can cook. Priority: stored cook (if a joining
    adult) > the single joining adult (auto, display-only) > nobody (shown
    as '? (A of B)'). The auto cook is never submitted as a select value so
    unchecking that person can never invalidate a save.
    """
    available = [m["id"] for m in members if m["role"] == "adult" and m["id"] in joiners]
    names = {m["id"]: m["name"] for m in members}
    stored_valid = stored_cook in available
    if stored_valid:
        effective = stored_cook
        auto_cook = None
    elif len(available) == 1:
        effective = available[0]
        auto_cook = names.get(available[0], available[0])
    else:
        effective = ""
        auto_cook = None
    return {
        "effective_cook": effective,
        "stored_cook": stored_cook,
        "stored_valid": stored_valid,
        "auto_cook": auto_cook,
        "available_cooks": available,
        "cook_options": [
            {"id": person_id, "name": names.get(person_id, person_id)} for person_id in available
        ],
        "cook_unset_label": " of ".join(names.get(c, c) for c in available),
        "no_cook_available": not available,
    }


def plan_view(
    store: Store,
    plan_id: int,
    recipes: list[Recipe],
    prefs: Preferences,
    people: list[dict],
    image_for=None,
    dishes: Sequence[ComposedDish] = (),
) -> list[dict]:
    """Per-evening view model: label, dish, cook, joiners, swap options.

    A composed dish evening carries one pick per part: the row shows the parts
    joined with ' + ' under the dish's name, and offers a select per part.
    """
    by_slug = {r.slug: r for r in recipes}
    dishes_by_slug = {d.slug: d for d in dishes}
    picks = store.plan_picks(plan_id)
    view: list[dict] = []
    last_cook_slug: str | None = None
    for row in store.plan_entries(plan_id):
        evening = planner.evenings_for(store, row.day, days=1)[0]
        budget = evening.free_minutes
        name = ""
        minutes = 0
        total_minutes = 0
        parts_label = ""
        part_options: list[dict] = []
        unfilled: list[str] = []
        alts: list[dict] = []
        if row.kind == "no_cook":
            name = "Niet koken (we eten elders)"
            minutes = 0
            # offer dishes again so the evening can be switched back to cooking
            alts = _alternatives(list(recipes), prefs, evening, image_for, dishes)
        elif row.kind == "cook":
            dish = dishes_by_slug.get(row.recipe_slug or "")
            recipe = by_slug.get(row.recipe_slug) if row.recipe_slug else None
            if dish is not None:
                name = dish.name
                part_options, unfilled, minutes, total_minutes = _dish_parts(
                    dish, picks.get(row.day, {}), by_slug
                )
                parts_label = " + ".join(
                    part["chosen_name"] for part in part_options if part["chosen_name"]
                )
            elif recipe is None:
                name = "(recept verwijderd)"
            else:
                name = recipe.name
                minutes = recipe.time.same_day_minutes
                total_minutes = recipe.time.total_minutes
            alts = _alternatives(
                list(recipes), prefs, evening, image_for, dishes, exclude=row.recipe_slug
            )
        else:
            source = by_slug.get(row.source_slug or "")
            dish = dishes_by_slug.get(row.source_slug or "")
            label = dish.name if dish else (source.name if source else row.source_slug)
            name = f"Restjes ({label})"
            minutes = 15
            if dish is not None:
                # cook once, eat twice: show the combination the source cooks
                part_options, unfilled, _longest, _total = _dish_parts(
                    dish, picks.get(row.day, {}), by_slug
                )
                parts_label = " + ".join(
                    part["chosen_name"] for part in part_options if part["chosen_name"]
                )
        absent = {p["id"] for p in people} - set(evening.joiners or ())
        members = [
            {
                "id": p["id"],
                "name": p["name"] or p["id"],
                "role": p["role"],
                "absent": p["id"] in absent,
            }
            for p in people
        ]
        cooking = cook_view(evening.cook or "", members, list(evening.joiners or ()))
        view.append(
            {
                "day_iso": row.day.isoformat(),
                "label": f"{WEEKDAYS[row.day.weekday()].capitalize()} {row.day.strftime('%d-%m')}",
                "kind": row.kind,
                "slug": row.recipe_slug,
                "name": name,
                "minutes": minutes,
                "total_minutes": total_minutes,
                "budget": budget,
                "is_dish": (row.kind == "cook" and row.recipe_slug in dishes_by_slug)
                or (row.kind == "leftovers" and row.source_slug in dishes_by_slug),
                "editable_parts": row.kind == "cook" and row.recipe_slug in dishes_by_slug,
                "parts_label": parts_label,
                "part_options": part_options,
                "unfilled": unfilled,
                "people": (len(evening.joiners) if evening.joiners is not None else row.people),
                "image": (image_for(row.recipe_slug) if image_for and row.recipe_slug else None),
                "members": members,
                "cooking": cooking,
                "alternatives": alts,
                "can_leftovers": (row.kind in ("cook", "no_cook") and last_cook_slug is not None),
                "over_budget": row.kind == "cook" and minutes > budget,
                "over_total": row.kind == "cook"
                and total_minutes > budget
                and not (minutes > budget),
            }
        )
        if row.kind == "cook":
            last_cook_slug = row.recipe_slug
    return view


def _alternatives(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    image_for,
    dishes: Sequence[ComposedDish],
    exclude: str | None = None,
) -> list[dict]:
    """Swap options for the picker, with the thumbnails it shows."""
    return [
        {
            "slug": row.slug,
            "name": row.name,
            "is_dish": row.is_dish,
            "part_labels": row.part_labels,
            "image": image_for(row.slug) if image_for and not row.is_dish else None,
        }
        for row in alternatives(recipes, prefs, evening, exclude_slug=exclude, dishes=dishes)
    ]


def _dish_parts(
    dish: ComposedDish, day_picks: dict[str, str], by_slug: dict[str, Recipe]
) -> tuple[list[dict], list[str], int, int]:
    """Per part: its options, the pick, the chosen name; plus the times.

    Returns (part rows, labels of unfilled parts, longest part, sum of parts).
    """
    rows: list[dict] = []
    unfilled: list[str] = []
    minutes = 0
    total = 0
    for part in dish.parts:
        chosen = day_picks.get(part.key, "")
        recipe = by_slug.get(chosen)
        if recipe is None:
            unfilled.append(part.label)
        else:
            minutes = max(minutes, recipe.time.same_day_minutes)
            total += recipe.time.total_minutes
        rows.append(
            {
                "key": part.key,
                "label": part.label,
                "chosen": chosen,
                "chosen_name": recipe.name if recipe else "",
                "options": [
                    {"slug": slug, "name": by_slug[slug].name if slug in by_slug else slug}
                    for slug in part.options
                ],
            }
        )
    return rows, unfilled, minutes, total


def format_amount(amount: float, unit: str) -> str:
    value = f"{amount:g}".replace(".", ",")
    return f"{value} {unit}".strip()


WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)


def dutch_date(value: date) -> str:
    """'donderdag 1 oktober', for a date a person has to act on."""
    return f"{WEEKDAYS[value.weekday()]} {value.day} {MONTHS[value.month - 1]}"
