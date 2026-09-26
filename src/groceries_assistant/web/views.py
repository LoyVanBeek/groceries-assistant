"""View builders and small helpers for the web UI."""

from __future__ import annotations

from pathlib import Path

from groceries_assistant.models import Recipe
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
) -> list[dict]:
    """Per-evening view model: label, dish, cook, joiners, swap options."""
    by_slug = {r.slug: r for r in recipes}
    view: list[dict] = []
    last_cook_slug: str | None = None
    for row in store.plan_entries(plan_id):
        evening = planner.evenings_for(store, row.day, days=1)[0]
        budget = evening.free_minutes
        name = ""
        minutes = 0
        alts: list[dict] = []
        if row.kind == "cook":
            recipe = by_slug.get(row.recipe_slug) if row.recipe_slug else None
            if recipe is None:
                name = "(recept verwijderd)"
            else:
                name = recipe.name
                minutes = recipe.time.same_day_minutes
                alts = [
                    {"slug": r.slug, "name": r.name}
                    for _s, r in alternatives(
                        list(recipes), prefs, evening, exclude_slug=row.recipe_slug
                    )
                ]
        else:
            source = by_slug.get(row.source_slug or "")
            name = f"Restjes ({source.name if source else row.source_slug})"
            minutes = 15
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
                "budget": budget,
                "people": (len(evening.joiners) if evening.joiners is not None else row.people),
                "image": image_for(row.recipe_slug) if image_for else None,
                "members": members,
                "cooking": cooking,
                "alternatives": alts,
                "can_leftovers": row.kind == "cook" and last_cook_slug is not None,
                "over_budget": row.kind == "cook" and minutes > budget,
            }
        )
        if row.kind == "cook":
            last_cook_slug = row.recipe_slug
    return view


def format_amount(amount: float, unit: str) -> str:
    value = f"{amount:g}".replace(".", ",")
    return f"{value} {unit}".strip()
