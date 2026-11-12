"""View builders and small helpers for the web UI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import TypedDict

from groceries_assistant.models import ComposedDish, Recipe
from groceries_assistant.planner.availability import EveningAvailability
from groceries_assistant.planner.engine import DEFAULT_REPEAT_WINDOW_DAYS
from groceries_assistant.preferences import Preferences
from groceries_assistant.recipes.loader import load_recipes_with_errors
from groceries_assistant.services import planner_service as planner
from groceries_assistant.services.planning import alternatives
from groceries_assistant.store.db import PlanEntryRow, Store

WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


def restjes_surplus(
    row: PlanEntryRow,
    picks_day: Mapping[str, str],
    by_slug: Mapping[str, Recipe],
    dishes_by_slug: Mapping[str, ComposedDish],
    gepland: Mapping[str, int],
    vriezer=None,
) -> tuple[int, str | None]:
    """Hoeveel porties deze kookavond overlaat, en waar ze vandaan komen.

    Returns (porties, slug): een gewoon recept is zijn eigen bron; een
    samengesteld gerecht laat het gekozen onderdeel met de meeste porties over
    achter — de component wordt ingevroren, niet het gerecht als geheel.
    `gepland` telt per slug de porties die al als restjes-avond staan
    (gerechtsbron én onderdeelbron), `vriezer.portions` wat al ingevroren is.
    """
    if row.kind != "cook" or not row.recipe_slug:
        return 0, None
    people = row.people or 4
    dish = dishes_by_slug.get(row.recipe_slug)
    if dish is None:
        recipe = by_slug.get(row.recipe_slug)
        if recipe is None:
            return 0, None
        restjes = recipe.servings - people - gepland.get(recipe.slug, 0)
        restjes -= vriezer.portions(recipe.slug) if vriezer else 0
        return max(0, restjes), recipe.slug
    # samengesteld gerecht: alleen de gekozen onderdelen zijn werkelijk gekookt
    dish_eaten = gepland.get(dish.slug, 0)
    best_portions, best_slug = 0, None
    for pick_slug in dict.fromkeys(picks_day.values()):
        part = by_slug.get(pick_slug)
        if part is None:
            continue
        restjes = part.servings - people - gepland.get(pick_slug, 0)
        restjes -= vriezer.portions(pick_slug) if vriezer else 0
        restjes -= dish_eaten
        if restjes > best_portions:
            best_portions, best_slug = restjes, pick_slug
    return max(0, best_portions), best_slug


class Popularity(TypedDict):
    """Aggregate 'how well liked' badge (label + css key)."""

    key: str
    label: str
    favourite: bool


class VerdictDot(TypedDict):
    """One per-person verdict dot."""

    person: str
    verdict: str


class RecencyBadge(TypedDict):
    """How long ago the recipe was planned (label + css key)."""

    key: str
    label: str
    last: date | None


class SelectOption(TypedDict):
    """One alternative row of the /plan/select page, indicators included."""

    slug: str
    name: str
    is_dish: bool
    part_labels: tuple[str, ...]
    problem: str
    score: float
    minutes: int | None
    image: str | None
    badge: Popularity
    favourite: bool
    dots: list[VerdictDot]
    recency: RecencyBadge
    bonus_ingredients: int
    ingredients: tuple[tuple[str, bool], ...]


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
    people: list[dict],
    image_for=None,
    dishes: Sequence[ComposedDish] = (),
    today: date | None = None,
    approved_days: set[date] | None = None,
    ordered_days: set[date] | None = None,
    vriezer=None,
) -> list[dict]:
    """Per-day view model: label, kok, joiners, the day's courses (in course
    order: soep, voorgerecht, hoofdgerecht, bijgerecht, nagerecht), swap
    options.

    `by_slug` includes all recipes and component-as-recipe objects for
    resolving dish part picks.
    """
    today = today or date.today()
    approved_days = approved_days or set()
    ordered_days = ordered_days or set()
    dishes_by_slug = {d.slug: d for d in dishes}
    picks = store.plan_picks(plan_id)
    view: list[dict] = []
    all_rows = store.plan_entries(plan_id)
    day_targets = []
    for row in all_rows:
        day_targets.append(
            (
                row.day.isoformat(),
                f"{WEEKDAYS[row.day.weekday()].capitalize()} {row.day.strftime('%d-%m')}",
            )
        )
    last_cook_slug: str | None = None
    # porties restjes die per gerecht al als restjes-avonden gepland staan
    restjes_gepland: dict[str, int] = {}
    for r in all_rows:
        if r.kind == "leftovers" and r.source_slug:
            restjes_gepland[r.source_slug] = restjes_gepland.get(r.source_slug, 0) + (r.people or 0)
    prev_day: date | None = None
    day_entry: dict | None = None
    for index, row in enumerate(all_rows):
        first_of_day = row.day != prev_day
        prev_day = row.day
        if first_of_day:
            evening = planner.evenings_for(store, row.day, days=1)[0]
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
            day_entry = {
                "day_iso": row.day.isoformat(),
                "label": f"{WEEKDAYS[row.day.weekday()].capitalize()} {row.day.strftime('%d-%m')}",
                "day_abbr": WEEKDAYS[row.day.weekday()][:2].capitalize(),
                "day_date": row.day.strftime("%d-%m"),
                "kind": row.kind,
                "budget": evening.free_minutes,
                "minutes": 0,
                "total_minutes": 0,
                "people": (len(evening.joiners) if evening.joiners is not None else row.people),
                "members": members,
                "cooking": cooking,
                "is_dish": False,
                "editable_parts": False,
                "part_options": [],
                "unfilled": [],
                "can_leftovers": (row.kind in ("cook", "no_cook") and last_cook_slug is not None),
                "restjes_portions": 0,
                "restjes_frozen": 0,
                "over_budget": False,
                "has_main_dish": False,
                "is_today": row.day == today,
                "is_past": row.day < today,
                "is_approved": row.day in approved_days,
                "is_ordered": row.day in ordered_days,
                "prev_day_iso": (all_rows[index - 1].day.isoformat() if index else None),
                "next_day_iso": (
                    all_rows[index + 1].day.isoformat() if index + 1 < len(all_rows) else None
                ),
                "swap_targets": [
                    {"day_iso": iso, "label": label}
                    for iso, label in day_targets
                    if iso != row.day.isoformat()
                ],
                "courses": [],
            }
            view.append(day_entry)
        assert day_entry is not None
        evening_free = day_entry["budget"]
        name = ""
        minutes = 0
        total_minutes = 0
        parts_label = ""
        part_options: list[dict] = []
        unfilled: list[str] = []
        if row.kind == "no_cook":
            name = "Niet koken (we eten elders)"
            minutes = 0
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
            elif row.recipe_slug is None:
                name = "Nog te kiezen"
            elif recipe is None:
                name = "(recept verwijderd)"
            else:
                name = recipe.name
                minutes = recipe.time.same_day_minutes
                total_minutes = recipe.time.total_minutes
        else:
            source = by_slug.get(row.source_slug or "")
            dish = dishes_by_slug.get(row.source_slug or "")
            label = dish.name if dish else (source.name if source else row.source_slug)
            # uit de vriezer = geen restjes van gisteren: eigen naam + icoon
            name = f"{label} (uit vriezer)" if row.frozen else f"Restjes ({label})"
            minutes = 15
            if dish is not None:
                # cook once, eat twice: show the combination the source cooks
                part_options, unfilled, _longest, _total = _dish_parts(
                    dish, picks.get(row.day, {}), by_slug, image_for
                )
                parts_label = " + ".join(
                    part["chosen_name"] for part in part_options if part["chosen_name"]
                )
        restjes_portions, _freeze_slug = restjes_surplus(
            row, picks.get(row.day, {}), by_slug, dishes_by_slug, restjes_gepland, vriezer
        )
        if row.kind == "cook" and row.recipe_slug in dishes_by_slug:
            restjes_frozen = sum(
                (vriezer.portions(slug) if vriezer else 0)
                for slug in set(picks.get(row.day, {}).values())
            )
        else:
            restjes_frozen = vriezer.portions(row.recipe_slug) if vriezer and row.recipe_slug else 0
        # de versleepbare ❄-chips: bij een gewoon recept het recept zelf, bij
        # een dag-gerecht elk gekozen onderdeel dat ingevroren voorraad heeft
        frozen_slugs: list[str] = []
        if vriezer and row.kind == "cook" and row.recipe_slug:
            if row.recipe_slug in dishes_by_slug:
                frozen_slugs = [
                    s for s in dict.fromkeys(picks.get(row.day, {}).values()) if vriezer.portions(s)
                ]
            elif vriezer.portions(row.recipe_slug):
                frozen_slugs = [row.recipe_slug]
        frozen_chips = []
        for s in frozen_slugs:
            since = vriezer.since(s)
            frozen_chips.append(
                {
                    "slug": s,
                    "name": by_slug[s].name if s in by_slug else s,
                    "portions": vriezer.portions(s),
                    "age": (date.today() - date.fromisoformat(since)).days if since else None,
                }
            )
        # de gekozen onderdelen als {slug, naam}: de weekweergave linkt ze naar
        # de receptpagina's (dat is wat je kookt bij een dag-gerecht)
        parts_linked = [
            {"slug": p["chosen"], "name": p["chosen_name"]}
            for p in part_options
            if p["chosen_name"]
        ]
        course_entry = {
            "course": row.course,
            "course_label": row.course.capitalize() if row.course else "",
            "kind": row.kind,
            "slug": row.recipe_slug,
            "source_slug": row.source_slug,
            "name": name,
            "minutes": minutes,
            "total_minutes": total_minutes,
            "is_dish": (row.kind == "cook" and row.recipe_slug in dishes_by_slug)
            or (row.kind == "leftovers" and row.source_slug in dishes_by_slug),
            "editable_parts": row.kind == "cook" and row.recipe_slug in dishes_by_slug,
            "parts": parts_linked,
            "parts_label": parts_label,
            "part_options": part_options,
            "unfilled": unfilled,
            "image": (
                image_for(row.source_slug)
                if image_for and row.kind == "leftovers" and row.source_slug
                else (image_for(row.recipe_slug) if image_for and row.recipe_slug else None)
            ),
            "frozen": row.frozen,
            "frozen_chips": frozen_chips,
            "over_budget": row.kind == "cook" and minutes > evening_free,
            "day_iso": row.day.isoformat(),
        }
        day_entry["courses"].append(course_entry)
        if not row.course:
            # het hoofdslot voorziet de dagteksten en de hoofdslot-acties
            day_entry["kind"] = row.kind
            day_entry["minutes"] = minutes
            day_entry["total_minutes"] = total_minutes
            day_entry["is_dish"] = course_entry["is_dish"]
            day_entry["editable_parts"] = course_entry["editable_parts"]
            day_entry["part_options"] = part_options
            day_entry["unfilled"] = unfilled
            day_entry["restjes_portions"] = restjes_portions
            day_entry["restjes_frozen"] = restjes_frozen
            day_entry["over_budget"] = course_entry["over_budget"]
            day_entry["has_main_dish"] = bool(row.recipe_slug or row.source_slug)
            day_entry["can_leftovers"] = (
                row.kind in ("cook", "no_cook") and last_cook_slug is not None
            )
        if row.kind == "cook":
            last_cook_slug = row.recipe_slug
    return view


def popularity(verdicts: dict[str, str]) -> Popularity:
    """Aggregate 'how well liked' badge from the per-person verdicts.

    'never' hard-excludes a recipe from the picker, so badge colours only
    ever describe the rows that are actually on offer.
    """
    loves = any(verdict == "love" for verdict in verdicts.values())
    dislikes = any(verdict == "dislike" for verdict in verdicts.values())
    if loves and not dislikes:
        return {"key": "love", "label": "geliefd", "favourite": True}
    if loves:
        return {"key": "mixed", "label": "gemengd", "favourite": True}
    if dislikes:
        return {"key": "hate", "label": "niet geliefd", "favourite": False}
    return {"key": "ok", "label": "ok", "favourite": False}


def recency(last_used: date | None, today: date) -> RecencyBadge:
    """How recently a recipe was planned, as a coloured badge.

    The orange bucket mirrors the planner's repeat window
    (DEFAULT_REPEAT_WINDOW_DAYS): eating within it is the repetition the
    planner normally hedges, so the badge makes it a conscious choice here.
    """
    if last_used is None:
        return {"key": "never", "label": "nog nooit", "last": None}
    days = max((today - last_used).days, 0)
    if days <= 7:
        return {"key": "week", "label": "deze week", "last": last_used}
    if days <= DEFAULT_REPEAT_WINDOW_DAYS:
        return {"key": "recent", "label": "2-3 weken geleden", "last": last_used}
    return {"key": "old", "label": "langer geleden", "last": last_used}


def select_options(
    recipes: list[Recipe],
    prefs: Preferences,
    evening: EveningAvailability,
    image_for,
    dishes: Sequence[ComposedDish],
    exclude_slug: str,
    *,
    by_slug: dict[str, Recipe],
    history: dict[str, date],
    verdicts_by_slug: dict[str, dict[str, str]],
    people: list[dict],
    today: date,
    q: str = "",
    sort: str = "score",
    tags_by_slug: Mapping[str, Sequence[str]] | None = None,
    tag: str = "",
    course: str = "",
    course_by_slug: Mapping[str, str] | None = None,
    bonus: Mapping[str, int] | None = None,
    bonus_store=None,
) -> list[SelectOption]:
    """Full option list for the /plan/select page, indicators included.

    One row per alternative (composed dishes first, then recipes, blocked
    rows at the bottom), enriched with the aggregate popularity badge, the
    per-person verdict dots, the favourite marker and the last-used badge.
    `exclude_slug` keeps the evening's current dish/recipe off the list.
    """
    rows: list[SelectOption] = []
    for row in alternatives(
        recipes,
        prefs,
        evening,
        exclude_slug=exclude_slug,
        dishes=dishes,
        by_slug=by_slug,
        bonus=bonus,
        bonus_store=bonus_store,
    ):
        verdicts = verdicts_by_slug.get(row.slug, {})
        pop = popularity(verdicts)
        rows.append(
            {
                "slug": row.slug,
                "name": row.name,
                "is_dish": row.is_dish,
                "part_labels": row.part_labels,
                "problem": row.problem,
                "score": row.score,
                "minutes": row.minutes,
                "image": image_for(row.slug) if image_for and not row.is_dish else None,
                "badge": pop,
                "favourite": pop["favourite"],
                "dots": [
                    VerdictDot(person=p["id"], verdict=verdicts.get(p["id"], "")) for p in people
                ],
                "recency": recency(history.get(row.slug), today),
                "bonus_ingredients": getattr(row, "bonus_ingredients", 0),
                "ingredients": getattr(row, "ingredients", ()),
            }
        )
    query = q.strip().lower()
    if query:
        rows = [row for row in rows if query in row["name"].lower()]
    if tag and tags_by_slug:
        wanted = tag.strip().lower()
        rows = [row for row in rows if wanted in tags_by_slug.get(row["slug"], ())]
    if course and course != "alle":
        # standaard alleen hoofdgerechten: een soep is geen hoofdgerecht
        rows = [
            row for row in rows if (course_by_slug or {}).get(row["slug"], "hoofdgerecht") == course
        ]
    if sort == "recent":
        # usable rows first, then blocked; within each, most recently planned
        # first so the repeat is visible; never-used rows sink to the bottom
        def _sort_key(row: SelectOption):
            last = row["recency"]["last"]
            return (
                1 if row["problem"] else 0,
                1 if last is None else 0,
                -last.toordinal() if last is not None else 0,
            )

        rows.sort(key=_sort_key)
    return rows


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
