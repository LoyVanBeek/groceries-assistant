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
    by_slug: dict[str, Recipe],
    meals: list[Recipe],
    prefs: Preferences,
    people: list[dict],
    image_for=None,
    dishes: Sequence[ComposedDish] = (),
    today: date | None = None,
    approved_through: date | None = None,
) -> list[dict]:
    """Per-evening view model: label, dish, cook, joiners, swap options.

    `by_slug` includes all recipes and component-as-recipe objects for
    resolving dish part picks; `meals` is the subset shown in the picker.
    """
    today = today or date.today()
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
            alts = _alternatives(meals, prefs, evening, image_for, dishes, by_slug=by_slug)
        elif row.kind == "cook":
            dish = dishes_by_slug.get(row.recipe_slug or "")
            recipe = by_slug.get(row.recipe_slug) if row.recipe_slug else None
            if dish is not None:
                name = dish.name
                part_options, unfilled, minutes, total_minutes = _dish_parts(
                    dish, picks.get(row.day, {}), by_slug, image_for
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
                meals, prefs, evening, image_for, dishes, exclude=row.recipe_slug, by_slug=by_slug
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
                    dish, picks.get(row.day, {}), by_slug, image_for
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
                "is_today": row.day == today,
                "is_past": row.day < today,
                "is_approved": approved_through is not None and row.day <= approved_through,
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
    *,
    by_slug: dict[str, Recipe] | None = None,
) -> list[dict]:
    """Swap options for the picker, with the thumbnails it shows.

    When dish options reference components, pass the full `by_slug` dict so
    the engine can resolve them. `recipes` is the picker's display list.
    """
    return [
        {
            "slug": row.slug,
            "name": row.name,
            "is_dish": row.is_dish,
            "part_labels": row.part_labels,
            "problem": row.problem,
            "score": row.score,
            "image": image_for(row.slug) if image_for and not row.is_dish else None,
        }
        for row in alternatives(
            recipes, prefs, evening, exclude_slug=exclude, dishes=dishes, by_slug=by_slug
        )
    ]


def _dish_parts(
    dish: ComposedDish, day_picks: dict[str, str], by_slug: dict[str, Recipe], image_for=None
) -> tuple[list[dict], list[str], int, int]:
    """Per part: its options, the pick, the chosen name; plus the times.

    Returns (part rows, labels of unfilled parts, longest part, longest part again).
    Parts are prepared in parallel so the longest one determines the evening.
    """
    rows: list[dict] = []
    unfilled: list[str] = []
    minutes = 0
    # A dish may use more of a shared component than the component's own amount,
    # so the option lists and the chosen time are this dish's, not the default.
    by_dish = dish.recipes(by_slug)
    for part in dish.parts:
        chosen = day_picks.get(part.key, "")
        recipe = by_dish.get(chosen)
        if recipe is None:
            unfilled.append(part.label)
        else:
            minutes = max(minutes, recipe.time.same_day_minutes)
        options = [
            {
                "slug": option.slug,
                "name": by_dish[option.slug].name if option.slug in by_dish else option.slug,
                # what this dish uses the option as, for the dropdown:
                # a component shows the amount this dish asks for
                "label": _option_label(by_dish.get(option.slug)) or option.slug,
                "image": image_for(option.slug) if image_for else None,
            }
            for option in part.options
        ]
        rows.append(
            {
                "key": part.key,
                "label": part.label,
                "chosen": chosen,
                "chosen_name": recipe.name if recipe else "",
                # the pick as this dish uses it, so the closed picker shows the amount
                "chosen_label": next((o["label"] for o in options if o["slug"] == chosen), ""),
                "chosen_image": image_for(chosen) if image_for and chosen else None,
                "options": options,
            }
        )
    return rows, unfilled, minutes, minutes


def _option_label(recipe: Recipe | None) -> str:
    """One option in the week's dropdown, with what this dish uses it as.

    A plain recipe brings its own amounts and is eaten as written, so it shows
    only its time; a component is a purchase whose size this dish chooses, so it
    shows that size too: 'Kipfilet (1 kg, 40 min)'.
    """
    if recipe is None:
        return ""
    minutes = recipe.time.same_day_minutes
    if not recipe.scales_with_servings and len(recipe.ingredients) == 1:
        ingredient = recipe.ingredients[0]
        return f"{recipe.name} ({format_amount(ingredient.amount, ingredient.unit)}, {minutes} min)"
    return f"{recipe.name} ({minutes} min)"


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
