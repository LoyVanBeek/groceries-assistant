"""FastAPI web app: plan -> review -> AH deep-link, recipe ingestion, settings."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData

from groceries_assistant.ah.client import AHClient
from groceries_assistant.ah.deeplink import (
    build_add_multiple_url,
    chunk_items,
    generate_qr_svg,
    parse_product_ref,
    product_url,
)
from groceries_assistant.ah.matching import AHMapping, AHMappingStore, fact_from_unit_size
from groceries_assistant.components.loader import (
    ComponentError,
    load_component,
    load_components_with_errors,
    save_component,
)
from groceries_assistant.dishes.loader import (
    DishError,
    dish_file,
    load_dish,
    load_dishes_with_errors,
    save_dish,
)
from groceries_assistant.models import (
    Component,
    ComposedDish,
    DishPart,
    Ingredient,
    ProductFact,
    Recipe,
    TimeInfo,
    slugify,
)
from groceries_assistant.planner.pantry import StockEntry
from groceries_assistant.recipes.extract import extract_from_images
from groceries_assistant.recipes.loader import (
    RecipeError,
    load_recipe,
    load_recipes_with_errors,
    save_recipe,
)
from groceries_assistant.recipes.textparser import (
    LIST_MARKER,
    ParseResult,
    parse_ingredient_line,
    parse_recipe_text,
)
from groceries_assistant.recipes.urlimport import extract_from_url
from groceries_assistant.services.planner_service import (
    apply_absences,
    build_order,
    evenings_for,
    incomplete_dish_warnings,
    last_weeks_stock,
    match_products,
    parse_start,
    plan_to_picks,
    plan_to_rows,
    preferences_from_store,
    propose,
    rows_to_entries,
)
from groceries_assistant.store.db import PlanEntryRow, Store
from groceries_assistant.web.views import WEEKDAYS, dutch_date, format_amount, plan_view

SERVINGS = 4
LEFTOVERS_CHOICE = "__leftovers__"
COOK_CHOICE = "__cook__"
NO_COOK_CHOICE = "__no_cook__"
KEEP_CHOICE = ""

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# staged uploads get a random id as name, so they can never collide
STAGED_UID_RE = re.compile(r"[0-9a-f]{32}\Z")
PHOTO_STAGING = "photo-staging"
OCR_STAGING = "ocr-staging"


def _image_ext(filename: str | None) -> str:
    """Lowercase extension of an upload, if we serve that type."""
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_IMAGE_EXTS else ""


# --- the dish form ------------------------------------------------------------
#
# A dish is a list of parts, each part a name and a list of recipes that can fill
# it. The form offers a fixed number of part slots: a name field and one checkbox
# per recipe, so picking three options is three clicks instead of typing slugs.

MIN_PART_SLOTS = 3
"""Slots a fresh dish form shows: enough for 'aardappel, groente, vlees'."""


class PartSlot(TypedDict):
    """One row of the dish form: a name, and the recipes ticked for it."""

    label: str
    options: list[str]


def empty_slot() -> PartSlot:
    """One blank part row: a name and nothing chosen yet."""
    return PartSlot(label="", options=[])


def _text_field(form: FormData, name: str) -> str:
    """One submitted text field; a file part is never a field we asked for."""
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _part_slots(form: FormData) -> list[PartSlot]:
    """The submitted part rows, in order, as the form sent them."""
    indices = sorted(
        {
            int(matched.group(1))
            for key in form
            if (matched := re.fullmatch(r"(?:part|opt)_(\d+)", key)) is not None
        }
    )
    return [
        PartSlot(
            label=_text_field(form, f"part_{index}"),
            # a checkbox only ever submits its value as a string
            options=[value for value in form.getlist(f"opt_{index}") if isinstance(value, str)],
        )
        for index in indices
    ]


def _is_row_change(form: FormData) -> bool:
    """Did the click come from 'another part' or 'remove' rather than save?

    Both are buttons in the same form, so the only thing that tells them apart is
    which button carried the name. A row change only reshapes the form: it must
    never write the file, or removing a part would save the dish without it.
    """
    return bool(_text_field(form, "more") or _text_field(form, "remove"))


def _slot_rows(form: FormData) -> list[PartSlot]:
    """The rows to redisplay: what was submitted, minus a removed one, plus a spare.

    'Another part' and 'remove' both re-render the form with everything typed so
    far, so neither click loses work; only the number of rows changes.
    """
    rows = _part_slots(form)
    remove = _text_field(form, "remove")
    if remove:
        try:
            index = int(remove)
        except ValueError:
            index = -1
        if 0 <= index < len(rows):
            rows.pop(index)
    if form.get("more"):
        rows.append(empty_slot())
    while len(rows) < MIN_PART_SLOTS:
        rows.append(empty_slot())
    return rows


def _slots_from_parts(parts: list[DishPart]) -> list[PartSlot]:
    """A dish's parts as form rows, with one spare row to add to."""
    return [PartSlot(label=part.label, options=list(part.options)) for part in parts] + [
        empty_slot()
    ]


def _parts_from_slots(slots: list[PartSlot]) -> tuple[list[DishPart], str]:
    """Turn the form's rows into parts, or explain what is missing.

    A row nobody filled in is not an error: the form always shows a spare slot or
    two, and the user is not obliged to use them.
    """
    parts: list[DishPart] = []
    seen: dict[str, str] = {}
    for slot in slots:
        label, options = slot["label"], slot["options"]
        if not label and not options:
            continue
        if not label:
            return [], "geef elk onderdeel een naam, of vink niets aan"
        if not options:
            return [], f"kies minstens één optie voor '{label}'"
        key = slugify(label)
        if key in seen:
            return [], f"'{label}' en '{seen[key]}' zijn hetzelfde onderdeel; geef ze andere namen"
        seen[key] = label
        parts.append(DishPart.model_validate({"key": key, "label": label, "options": options}))
    if not parts:
        return [], "minstens één onderdeel nodig"
    return parts, ""


class OptionChoice(TypedDict):
    """One checkbox in a part row: a recipe and what choosing it costs."""

    slug: str
    name: str
    minutes: int


def _choices(recipes: list[Recipe]) -> list[OptionChoice]:
    """The checkboxes a part row offers, with the time each option costs.

    A composed dish is mostly components ('kipfilet', 'brocolli') with a few whole
    recipes to swap in, so the two lists are shown separately -- and the time is
    on the label, because a part's time is the component's own time.
    """
    return [
        OptionChoice(slug=recipe.slug, name=recipe.name, minutes=recipe.time.total_minutes)
        for recipe in sorted(recipes, key=lambda r: r.name.lower())
    ]


def _component_choices(components: list[Component]) -> list[OptionChoice]:
    """Component checkboxes for the dish form."""
    return sorted(
        (OptionChoice(slug=c.slug, name=c.name, minutes=c.minutes) for c in components),
        key=lambda o: o["name"].lower(),
    )


def _stage_dir(root: Path, folder: str) -> Path:
    """Staging directory for uploads, created on demand."""
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _staged_files(directory: Path) -> list[Path]:
    """Uploads waiting in a staging directory, oldest first."""
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.glob("*") if STAGED_UID_RE.match(p.stem) and _image_ext(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


def _staged_file(directory: Path, uid: str) -> Path | None:
    """Path of a staged upload by id, if the id is one of ours and it exists."""
    if not STAGED_UID_RE.match(uid):
        return None
    return next((p for p in directory.glob(f"{uid}.*") if _image_ext(p.name)), None)


def create_app(
    data_dir: str | os.PathLike[str],
    search_fn=None,
    tesseract_binary: str = "tesseract",
) -> FastAPI:
    """search_fn: injectable AH search(product) -> candidates; None = real client."""
    data = Path(data_dir)
    (data / "recipes").mkdir(parents=True, exist_ok=True)
    (data / "components").mkdir(parents=True, exist_ok=True)
    # staged uploads are scratch space for the current run only
    for folder in (PHOTO_STAGING, OCR_STAGING):
        for stale in (data / folder).glob("*"):
            stale.unlink(missing_ok=True)
    app = FastAPI(title="Groceries Assistant")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    holder: list[Store] = []

    def store() -> Store:
        if not holder:
            holder.append(Store(data / "state.db"))
        return holder[0]

    _mappings_cache: list[AHMappingStore] = []

    def mappings() -> AHMappingStore:
        if not _mappings_cache:
            _mappings_cache.append(AHMappingStore(data / "ah_mappings.yaml"))
        return _mappings_cache[0]

    def load_all() -> tuple[list[Recipe], list[str]]:
        return load_recipes_with_errors(data / "recipes")

    def recipes_by_slug() -> dict[str, Recipe]:
        all_recipes, _errors = load_all()
        return {r.slug: r for r in all_recipes}

    def load_components_all() -> tuple[list[Component], list[str]]:
        return load_components_with_errors(data / "components")

    def components_by_slug() -> dict[str, Component]:
        all_components, _errors = load_components_all()
        return {c.slug: c for c in all_components}

    def all_known() -> dict[str, Recipe]:
        """Recipes and components together, the latter converted to Recipe shape.

        Downstream code (engine, planning, views) works with Recipe objects, so a
        component is converted to a minimal Recipe whose sole ingredient is its own
        product and whose cook time is its minutes. This way the dish's part
        options (component slugs) resolve without touching the engine.
        """
        result = {r.slug: r for r in load_all()[0]}
        for c in load_components_all()[0]:
            if c.slug not in result:
                result[c.slug] = Recipe(
                    slug=c.slug,
                    name=c.name,
                    servings=4,
                    time=TimeInfo(cook=c.minutes),
                    ingredients=[Ingredient(product=c.name, amount=c.amount, unit=c.unit)],
                )
        return result

    templates.env.filters["dutch_date"] = dutch_date

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(
            request, template, {"request": request, "theme": request.cookies.get("theme"), **ctx}
        )

    def _previous_cook_slug(rows: list[PlanEntryRow], day: date) -> str | None:
        earlier = [r for r in rows if r.day < day and r.kind == "cook" and r.recipe_slug]
        return earlier[-1].recipe_slug if earlier else None

    # --- plan ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/plan", status_code=303)

    @app.post("/theme")
    def toggle_theme(request: Request, back: str = Form("/plan"), to: str = Form("")):
        target = (
            to
            if to in ("light", "dark")
            else ("light" if request.cookies.get("theme") == "dark" else "dark")
        )
        response = RedirectResponse(back or "/plan", status_code=303)
        response.set_cookie("theme", target, max_age=365 * 24 * 3600)
        return response

    @app.get("/plan", response_class=HTMLResponse)
    def plan_page(request: Request):
        all_recipes, errors = load_all()
        if not all_recipes:
            return render(request, "plan.html", no_recipes=True, errors=errors)
        proposal = store().load_proposal()
        if proposal is None:
            start = parse_start(None)
            dishes = dishes_by_slug()
            components = list(all_known().values())
            dinners_only = all_recipes
            plan = propose(
                dinners_only, store(), start, dishes=list(dishes.values()), components=components
            )
            plan_id = store().save_proposal(start, plan_to_rows(plan), plan_to_picks(plan, dishes))
            proposal = (plan_id, start)
        plan_id, start = proposal[0], proposal[1]
        prefs = preferences_from_store(store())
        evenings = plan_view(
            store(),
            plan_id,
            all_known(),
            [r for r in all_recipes],
            prefs,
            store().people(),
            image_for=image_path,
            dishes=list(dishes_by_slug().values()),
        )
        return render(
            request,
            "plan.html",
            plan_id=plan_id,
            start=start.isoformat(),
            evenings=evenings,
            errors=errors,
        )

    @app.post("/plan/swap")
    async def plan_swap(request: Request, day: str = Form(...), choice: str = Form("")):
        """Swap one evening's dish; empty/'__keep__' keeps the current pick.

        Note: the default must be Form('') — FastAPI treats an empty form
        value as missing, and the browser posts an empty value when the
        '(behoud)' option is selected.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel om te wijzigen")
        plan_id = proposal[0]
        rows = store().plan_entries(plan_id)
        target = date.fromisoformat(day)
        if choice in (KEEP_CHOICE, "__keep__"):
            return RedirectResponse("/plan", status_code=303)
        new_rows: list[PlanEntryRow] = []
        for row in rows:
            if row.day != target:
                new_rows.append(row)
                continue
            if choice == COOK_CHOICE:
                new_rows.append(_best_cook_row(rows, target))
            elif choice == LEFTOVERS_CHOICE:
                source = _previous_cook_slug(rows, target)
                if source is None:
                    raise HTTPException(400, "Geen gerecht op de avond ervoor")
                new_rows.append(
                    PlanEntryRow(day=target, kind="leftovers", source_slug=source, people=4)
                )
            elif choice == NO_COOK_CHOICE:
                # eating elsewhere: no dish, no groceries, no cook needed
                new_rows.append(PlanEntryRow(day=target, kind="no_cook", people=0))
            else:
                new_rows.append(PlanEntryRow(day=target, kind="cook", recipe_slug=choice, people=4))
        store().update_proposal_rows(plan_id, new_rows)
        _reconcile_picks(plan_id, new_rows, target, _row_dish_slug(rows, target))
        return RedirectResponse("/plan", status_code=303)

    def _row_dish_slug(rows: list[PlanEntryRow], day: date) -> str:
        """The composed dish (if any) an evening had before the swap."""
        row = next((r for r in rows if r.day == day), None)
        return (row.recipe_slug or row.source_slug or "") if row else ""

    def _reconcile_picks(plan_id: int, rows: list[PlanEntryRow], day: date, was: str) -> None:
        """Keep this evening's picks in step with the dish it now has.

        Swapping to another dish (or to a plain recipe, or to no cooking at all)
        drops the picks: they described a combination that is no longer planned.
        A leftovers evening eats what its source evening cooks, so it copies
        those picks instead -- display only, the extra portions are bought on
        the source evening.
        """
        row = next((r for r in rows if r.day == day), None)
        dishes = dishes_by_slug()
        current = store().plan_picks(plan_id).get(day, {})
        if row is not None and row.kind == "leftovers":
            source = next(
                (
                    r
                    for r in rows
                    if r.kind == "cook" and r.day < day and r.recipe_slug == row.source_slug
                ),
                None,
            )
            copied = store().plan_picks(plan_id).get(source.day, {}) if source else {}
            if copied != current:
                store().set_picks(plan_id, day, copied)
            return
        if row is None or row.recipe_slug not in dishes or row.recipe_slug != was:
            store().clear_day_picks(plan_id, day)
            return
        kept = {k: v for k, v in current.items() if dishes[row.recipe_slug].part(k) is not None}
        if kept != current:
            store().set_picks(plan_id, day, kept)

    @app.post("/plan/picks")
    async def plan_picks(request: Request, day: str = Form(...)):
        """Choose which recipe fills each part of one evening's dish.

        One `<select name="part_<key>">` per part, so an empty value clears that
        part. Only a cook evening of a composed dish takes picks; anything else
        is ignored rather than half-applied.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel om te wijzigen")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        row = next((r for r in store().plan_entries(plan_id) if r.day == target), None)
        dish = dishes_by_slug().get(row.recipe_slug or "") if row and row.kind == "cook" else None
        if dish is None:
            return RedirectResponse("/plan", status_code=303)
        form = await request.form()
        picks: dict[str, str] = {}
        for part in dish.parts:
            chosen = str(form.get(f"part_{part.key}", "")).strip()
            # an option that is not on offer is ignored, not stored
            if chosen and chosen in part.options:
                picks[part.key] = chosen
        store().set_picks(plan_id, target, picks)
        return RedirectResponse("/plan", status_code=303)

    def _best_cook_row(rows: list[PlanEntryRow], target: date) -> PlanEntryRow:
        """Convert a leftovers evening into a cook evening with the best fit.

        Only plain recipes: a composed dish would still need its parts filled, and
        an empty dish would buy nothing.
        """
        from groceries_assistant.services.planning import alternatives

        evening = evenings_for(store(), target, days=1)[0]
        prefs = preferences_from_store(store())
        all_recipes, _ = load_all()
        meals = all_recipes
        source = _previous_cook_slug(rows, target)
        # rows with a reason cannot be planned, so they are not candidates here
        usable = [
            row
            for row in alternatives(meals, prefs, evening, exclude_slug=source)
            if not row.problem
        ]
        if usable:
            slug = usable[0].slug
        elif meals:
            slug = min(meals, key=lambda r: (r.time.same_day_minutes, r.slug)).slug
        else:
            raise HTTPException(400, "Geen recepten om te koken")
        return PlanEntryRow(day=target, kind="cook", recipe_slug=slug, people=4)

    @app.post("/plan/evening")
    async def plan_evening(request: Request, day: str = Form(...)):
        """Per-evening adjustments: who cooks, who joins, how much time.

        The cook must be an adult who joins that evening; violations reject
        the whole save so no partial state (override/absences) is written.
        """
        form = await request.form()
        cook = str(form.get("cook", "")).strip()
        joined = {str(v) for v in form.getlist("joined")}
        minutes_raw = str(form.get("minutes", "")).strip()
        target = date.fromisoformat(day)
        if cook:
            people_by_id = {p["id"]: p for p in store().people()}
            person = people_by_id.get(cook)
            if person is None or person["role"] != "adult" or cook not in joined:
                return RedirectResponse("/plan?error=kok", status_code=303)
        minutes = int(minutes_raw) if minutes_raw.isdigit() else evening_minutes(store(), target)
        store().set_week_override(
            target,
            cook=cook,
            free_minutes=max(minutes, 0),
            allow_leftovers=evening_allows_leftovers(store(), target),
        )
        for person in store().people():
            store().set_absence(target, person["id"], person["id"] not in joined)
        return RedirectResponse("/plan", status_code=303)

    def evening_minutes(s: Store, day: date) -> int:
        template = s.week_template()[day.weekday()]
        override = s.week_override(day)
        return override[1] if override else template[1]

    def evening_allows_leftovers(s: Store, day: date) -> bool:
        template = s.week_template()[day.weekday()]
        override = s.week_override(day)
        return override[2] if override else template[2]

    @app.post("/plan/approve")
    def plan_approve():
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        rows = apply_absences(store(), store().plan_entries(plan_id))
        store().update_proposal_rows(plan_id, rows)
        by_slug = all_known()
        dishes = dishes_by_slug()
        entries = rows_to_entries(rows, by_slug, dishes, store().plan_picks(plan_id))
        if not any(e.kind == "cook" for e in entries):
            raise HTTPException(400, "Minstens één kookavond nodig")
        facts = store().product_facts()
        order = build_order(
            entries,
            by_slug,
            store().staples(),
            servings=SERVINGS,
            facts=facts,
            carried=last_weeks_stock(store(), plan_id, by_slug, dishes, facts, servings=SERVINGS),
        )
        matched = _match([i.product for i in order.shopping])
        store().save_order_items(
            plan_id,
            [
                (
                    item.product,
                    item.unit,
                    item.amount,
                    matched.get(item.product, (None, ""))[0],
                    item.pack_count,
                    item.pack_unit,
                    item.note,
                )
                for item in order.shopping
            ],
        )
        store().approve_proposal(plan_id)
        return RedirectResponse("/order", status_code=303)

    def _match(product_names: list[str]) -> dict[str, tuple[int, str]]:
        if search_fn is not None:
            return match_products(product_names, search_fn, mappings())
        with AHClient() as client:
            return match_products(product_names, client.search, mappings())

    # --- order ---------------------------------------------------------------

    @app.get("/order", response_class=HTMLResponse)
    def order_page(request: Request):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        return render(
            request,
            "order.html",
            items=store().order_items(plan_id),
            warnings=_plan_warnings(plan_id),
            leftovers=_plan_leftovers(plan_id),
            fmt=format_amount,
        )

    def _plan_leftovers(plan_id: int) -> list[StockEntry]:
        """What this week's purchases leave over, recomputed from the plan.

        Read-only, and never stored: the same arithmetic as approving the week
        ran, so the list cannot drift from the plan it came from.
        """
        by_slug = all_known()
        dishes = dishes_by_slug()
        facts = store().product_facts()
        if not facts:
            return []
        entries = rows_to_entries(
            store().plan_entries(plan_id), by_slug, dishes, store().plan_picks(plan_id)
        )
        order = build_order(
            entries,
            by_slug,
            store().staples(),
            servings=SERVINGS,
            facts=facts,
            carried=last_weeks_stock(store(), plan_id, by_slug, dishes, facts, servings=SERVINGS),
        )
        return order.leftovers

    def _plan_warnings(plan_id: int) -> list[str]:
        """What the approved plan is still missing, read straight from the plan.

        Composed dishes can be approved with parts unfilled; their groceries are
        not on the list, so the list has to say so. Read-only: nothing is stored.
        """
        entries = rows_to_entries(
            store().plan_entries(plan_id),
            all_known(),
            dishes_by_slug(),
            store().plan_picks(plan_id),
        )
        return incomplete_dish_warnings(entries)

    @app.post("/order/have")
    def order_have(product: str = Form(...), unit: str = Form(...), have: str = Form("")):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        store().set_order_item_have(latest[0], product, unit, bool(have))
        return RedirectResponse("/order", status_code=303)

    @app.get("/order/match", response_class=HTMLResponse)
    def order_match_page(request: Request):
        """Pin order items to AH article numbers, with live suggestions."""
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        items = store().order_items(plan_id)
        facts = store().product_facts()
        rows = []
        for item in items:
            suggestions = []
            error = ""
            if not item["webshop_id"]:
                try:
                    suggestions = [
                        {
                            "webshop_id": s.webshop_id,
                            "title": s.title,
                            "unit_size": s.unit_size,
                            "url": product_url(s.webshop_id),
                            "image": getattr(s, "image_url", None),
                        }
                        for s in _search(item["product"])[:5]
                    ]
                except Exception as exc:
                    error = f"AH onbereikbaar ({exc})"
            mapping = mappings().get(item["product"])
            matched_url = product_url(item["webshop_id"]) if item["webshop_id"] else None
            # A pack size AH states, read into a fact to offer. Only offered when
            # it reads cleanly: a guess here becomes a wrong list later.
            suggestion = (
                fact_from_unit_size(item["product"], mapping.ah_unit_size) if mapping else None
            )
            rows.append(
                {
                    **item,
                    "suggestions": suggestions,
                    "search_error": error,
                    "matched_url": matched_url,
                    "matched_title": mapping.ah_title if mapping else "",
                    "matched_image": mapping.ah_image_url if mapping else None,
                    "pack_label": (
                        f"{suggestion.pack_qty:g} {suggestion.pack_unit}" if suggestion else ""
                    ),
                    "pack_unit_size": mapping.ah_unit_size if mapping else "",
                    "has_fact": item["product"].lower() in facts,
                }
            )
        return render(request, "order_match.html", items=rows, fmt=format_amount)

    def _search(query: str) -> list:
        """AH product search, using the injected stub or the real client."""
        if search_fn is not None:
            return search_fn(query)
        with AHClient() as client:
            return client.search(query)

    @app.post("/order/match/pin")
    def order_match_pin(
        product: str = Form(...),
        unit: str = Form(...),
        webshop_id: str = Form(...),
        title: str = Form(""),
        image: str = Form(""),
        unit_size: str = Form(""),
    ):
        """Pin an order item to an AH product.

        `webshop_id` accepts a bare id or a pasted ah.nl product URL; the id is
        taken from the URL and its slug fills in the title when none is given.
        AH's own pack size is kept alongside, to offer as a suggestion later.
        """
        ref = parse_product_ref(webshop_id)
        if ref is None:
            return RedirectResponse("/order/match?error=id", status_code=303)
        article_id, slug_title = ref
        label = title.strip() or slug_title or f"webshop-id {article_id}"
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        mapping_store = mappings()
        mapping_store.put(
            AHMapping(
                product=product,
                ah_webshop_id=article_id,
                ah_title=label,
                score=1.0,
                confirmed=True,
                ah_image_url=image.strip() or None,
                ah_unit_size=unit_size.strip() or None,
            )
        )
        store().set_order_item_webshop_id(plan_id, product, unit, article_id)
        return RedirectResponse("/order/match", status_code=303)

    @app.post("/order/match/unpin")
    def order_match_unpin(product: str = Form(...), unit: str = Form(...)):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        mappings().remove(product)
        store().set_order_item_webshop_id(plan_id, product, unit, None)
        return RedirectResponse("/order/match", status_code=303)

    @app.post("/order/match/pack")
    def order_match_pack(product: str = Form(...), unit_size: str = Form("")):
        """Store a pack size for a matched product, read from AH's own size.

        Only the plain shapes are read; anything else is refused rather than
        guessed, and the user types it on the settings page instead. The shelf
        life is left at its default: a pack size is not a claim about freshness.
        """
        mapping = mappings().get(product)
        text = unit_size.strip() or (mapping.ah_unit_size if mapping else "") or ""
        fact = fact_from_unit_size(product, text)
        if fact is None:
            return RedirectResponse("/order/match?error=pack", status_code=303)
        store().set_product_fact(fact)
        return RedirectResponse("/order/match", status_code=303)

    @app.get("/order/link", response_class=HTMLResponse)
    def order_link(request: Request):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        pairs = [
            (int(i["webshop_id"]), 1)
            for i in store().order_items(plan_id)
            if i["webshop_id"] and not i["have"]
        ]
        links = [build_add_multiple_url(chunk) for chunk in chunk_items(pairs)]
        qr = generate_qr_svg(links[0]) if links else None
        return render(request, "link.html", links=links, qr=qr)

    # --- composed dishes ---------------------------------------------------------

    def dishes_dir() -> Path:
        directory = data / "dishes"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def dishes_by_slug() -> dict[str, ComposedDish]:
        dishes, _errors = load_dishes_with_errors(dishes_dir())
        return {d.slug: d for d in dishes}

    def _dish_form_ctx(
        name: str = "",
        slots: list[PartSlot] | None = None,
        error: str = "",
        *,
        form_title: str = "Nieuw gerecht",
        form_action: str = "/dishes",
    ) -> dict:
        """The dish page with its form filled in (also used to redisplay errors)."""
        loaded, errors = load_dishes_with_errors(dishes_dir())
        dishes = sorted(loaded, key=lambda d: d.name.lower())
        all_recipes, _load_errors = load_all()
        known = {r.slug: r.name for r in all_recipes}
        all_components, _component_errors = load_components_all()
        known.update({c.slug: c.name for c in all_components})
        missing: list[str] = []
        for dish in loaded:
            missing += [slug for slug in sorted(dish.option_slugs) if slug not in known]
        rows = list(slots or [])
        while len(rows) < MIN_PART_SLOTS:
            rows.append(empty_slot())
        people = store().people()
        verdicts = store().verdicts()
        return {
            "dishes": dishes,
            "errors": errors,
            "recipe_names": known,
            "unknown": missing,
            "form_title": form_title,
            "form_action": form_action,
            "name": name,
            "slots": rows,
            "min_part_slots": MIN_PART_SLOTS,
            "components": _component_choices(all_components),
            "dinners": _choices(all_recipes),
            "error": error,
            "people": people,
            "verdicts": {
                dish.slug: {
                    person["id"]: verdicts.get(dish.slug, {}).get(person["id"], "")
                    for person in people
                }
                for dish in dishes
            },
        }

    @app.get("/dishes", response_class=HTMLResponse)
    def dishes_page(request: Request):
        return render(request, "dishes.html", **_dish_form_ctx())

    @app.post("/dishes")
    async def dishes_create(request: Request):
        """Create one dish; the slug comes from the name."""
        form = await request.form()
        slots = _slot_rows(form)
        if _is_row_change(form):
            return _redisplay(request, str(form.get("name") or ""), slots, slug=None)
        return _save_dish(request, str(form.get("name") or ""), slots, slug=None)

    def _redisplay(
        request: Request, name: str, slots: list[PartSlot], *, slug: str | None
    ) -> HTMLResponse:
        """The form again, with the rows as they are now and no error to show."""
        return render(
            request,
            "dishes.html",
            **_dish_form_ctx(
                name,
                slots,
                form_title="Nieuw gerecht" if slug is None else "Wijzigen",
                form_action="/dishes" if slug is None else f"/dishes/{slug}",
            ),
        )

    def _save_dish(
        request: Request,
        name: str,
        slots: list[PartSlot],
        *,
        slug: str | None,
    ) -> HTMLResponse | RedirectResponse:
        """Check and store one dish, redisplaying the form when something is off.

        `slug` is None for a new dish, whose slug comes from its name. An
        existing dish keeps its slug, so plans keep pointing at it.
        """
        title = "Nieuw gerecht" if slug is None else "Wijzigen"
        action = "/dishes" if slug is None else f"/dishes/{slug}"

        def again(error: str) -> HTMLResponse:
            return render(
                request,
                "dishes.html",
                **_dish_form_ctx(name, slots, error, form_title=title, form_action=action),
            )

        parts, problem = _parts_from_slots(slots)
        if problem:
            return again(problem)
        stale = _unknown_options(parts, {**recipes_by_slug(), **components_by_slug()})
        if stale:
            # A browser can replay a form from before a component was renamed.
            return again(stale)
        target = slug or slugify(name)
        if not name.strip():
            return again("geef het gerecht een naam")
        if dish_file(dishes_dir(), target).exists() and slug != target:
            return again(f"'{target}' bestaat al")
        if target in recipes_by_slug() or target in components_by_slug():
            # A dish shares its slug space with recipes and components,
            # so a name clash would make them indistinguishable.
            return again(f"'{target}' is al een recept of onderdeel; kies een andere naam")
        _save_dish_file(target, name, parts)
        return RedirectResponse("/dishes", status_code=303)

    @app.post("/dishes/verdict")
    def dish_verdict_route(slug: str = Form(...), person: str = Form(...), verdict: str = Form("")):
        """Say something about a whole dish, the combination and not its parts."""
        _set_verdict(slug, person, verdict)
        return RedirectResponse("/dishes", status_code=303)

    @app.get("/dishes/{slug}", response_class=HTMLResponse)
    def dish_edit_page(request: Request, slug: str):
        try:
            dish = load_dish(dish_file(dishes_dir(), slug))
        except DishError as exc:
            raise HTTPException(404, str(exc)) from exc
        return render(
            request,
            "dishes.html",
            **_dish_form_ctx(
                dish.name,
                _slots_from_parts(dish.parts),
                form_title=f"{dish.name} wijzigen",
                form_action=f"/dishes/{slug}",
            ),
        )

    @app.post("/dishes/{slug}")
    async def dish_save(request: Request, slug: str):
        """Save a dish in place; the slug stays fixed, so plans keep pointing at it."""
        try:
            load_dish(dish_file(dishes_dir(), slug))  # 404 on a slug that is gone
        except DishError as exc:
            raise HTTPException(404, str(exc)) from exc
        form = await request.form()
        slots = _slot_rows(form)
        if _is_row_change(form):
            return _redisplay(request, str(form.get("name") or ""), slots, slug=slug)
        return _save_dish(request, str(form.get("name") or ""), slots, slug=slug)

    @app.post("/dishes/{slug}/delete")
    def dish_delete(slug: str):
        path = dish_file(dishes_dir(), slug)
        if not path.is_file():
            raise HTTPException(404, "Onbekend gerecht")
        path.unlink()
        return RedirectResponse("/dishes", status_code=303)

    def _unknown_options(parts: list[DishPart], known: Mapping[str, object]) -> str:
        """Part options that name no recipe, as a message for the dish form.

        Caught on save rather than at plan time: an option that resolves to
        nothing leaves the part with no options at all, and the planner then
        drops the dish from every list without saying why.
        """
        missing = sorted({slug for part in parts for slug in part.options if slug not in known})
        if not missing:
            return ""
        return (
            "dit zijn geen recepten of onderdelen, dus dit gerecht kan nooit gevuld "
            f"worden: {', '.join(missing)}"
        )

    def _save_dish_file(slug: str, name: str, parts: list[DishPart]) -> None:
        try:
            dish = ComposedDish.model_validate(
                {"slug": slug, "name": name.strip(), "parts": [p.model_dump() for p in parts]}
            )
        except ValidationError as exc:
            raise HTTPException(400, f"Ongeldig gerecht: {exc}") from exc
        save_dish(dish_file(dishes_dir(), slug), dish)

    # --- recipes ---------------------------------------------------------------

    def photo_staging() -> Path:
        return data / PHOTO_STAGING

    def ocr_staging() -> Path:
        return data / OCR_STAGING

    @app.get("/recipes/photos", response_class=HTMLResponse)
    def photos_page(request: Request):
        """Bulk photo import: stage a pile of photos, then assign them."""
        all_recipes, errors = load_all()
        return render(
            request,
            "photos.html",
            staged=_staged_files(photo_staging()),
            recipes=sorted(all_recipes, key=lambda r: r.name.lower()),
            errors=errors,
        )

    @app.post("/recipes/photos/add")
    async def photos_add(photos: Annotated[list[UploadFile] | None, File()] = None):
        """Stage uploads; wrong types are skipped instead of failing the batch."""
        added = skipped = 0
        for upload in photos or []:
            ext = _image_ext(upload.filename)
            if not ext:
                skipped += 1
                continue
            (_stage_dir(data, PHOTO_STAGING) / f"{uuid4().hex}{ext}").write_bytes(
                await upload.read()
            )
            added += 1
        query = f"toegevoegd={added}" + (f"&genegeerd={skipped}" if skipped else "")
        return RedirectResponse(f"/recipes/photos?{query}", status_code=303)

    @app.post("/recipes/photos/assign")
    async def photos_assign(request: Request):
        """Move staged photos into the chosen recipes; others wait for next time."""
        staging = photo_staging()
        form = await request.form()
        if form.get("clear"):
            for path in _staged_files(staging):
                path.unlink(missing_ok=True)
            return RedirectResponse("/recipes/photos", status_code=303)
        known = set(recipes_by_slug())
        assigned = 0
        for field, slug in form.multi_items():
            if not field.startswith("photo_") or slug not in known:
                continue
            photo = _staged_file(staging, field[len("photo_") :])
            if photo is None:
                continue
            _store_image(
                slug, photo.read_bytes(), photo.suffix, as_featured=image_path(slug) is None
            )
            photo.unlink(missing_ok=True)
            assigned += 1
        for field in form:
            if field.startswith("drop_"):
                photo = _staged_file(staging, field[len("drop_") :])
                if photo is not None:
                    photo.unlink(missing_ok=True)
        return RedirectResponse(f"/recipes/photos?toegewezen={assigned}", status_code=303)

    @app.get("/recipes/photos/staged/{name}")
    def staged_photo(name: str):
        photo = _staged_file(photo_staging(), Path(name).stem)
        if photo is None:
            raise HTTPException(404, "Geen foto")
        return FileResponse(photo)

    @app.get("/recipes/ocr", response_class=HTMLResponse)
    def ocr_queue_page(request: Request):
        """Bulk OCR import: a pile of photos, each to become its own recipe."""
        staged = _staged_files(ocr_staging())
        return render(request, "ocr.html", staged=staged, count=len(staged))

    @app.post("/recipes/ocr/add")
    async def ocr_add(photos: Annotated[list[UploadFile] | None, File()] = None):
        """Queue photos for OCR; the reading happens when you open one."""
        added = skipped = 0
        for upload in photos or []:
            ext = _image_ext(upload.filename)
            if not ext:
                skipped += 1
                continue
            (_stage_dir(data, OCR_STAGING) / f"{uuid4().hex}{ext}").write_bytes(await upload.read())
            added += 1
        query = f"toegevoegd={added}" + (f"&genegeerd={skipped}" if skipped else "")
        return RedirectResponse(f"/recipes/ocr?{query}", status_code=303)

    @app.post("/recipes/ocr/discard")
    def ocr_discard(uid: str = Form("")):
        staging = ocr_staging()
        if not uid:  # the 'alles weggooien' button
            for path in _staged_files(staging):
                path.unlink(missing_ok=True)
            return RedirectResponse("/recipes/ocr", status_code=303)
        photo = _staged_file(staging, uid)
        if photo is not None:
            photo.unlink(missing_ok=True)
        return RedirectResponse("/recipes/ocr", status_code=303)

    @app.get("/recipes/ocr/staged/{name}")
    def ocr_staged_photo(name: str):
        photo = _staged_file(ocr_staging(), Path(name).stem)
        if photo is None:
            raise HTTPException(404, "Geen foto")
        return FileResponse(photo)

    @app.get("/recipes/ocr/{uid}", response_class=HTMLResponse)
    def ocr_review_page(request: Request, uid: str):
        """Read one queued photo and show it in the editor, like a photo import."""
        photo = _staged_file(ocr_staging(), uid)
        if photo is None:
            raise HTTPException(404, "Geen foto meer in de rij")
        try:
            parsed, _raw = extract_from_images([photo], binary=tesseract_binary, lang="nld")
        except Exception as exc:
            staged = _staged_files(ocr_staging())
            return render(
                request,
                "ocr.html",
                staged=staged,
                count=len(staged),
                error=f"Uitlezen mislukt: {exc}",
            )
        stems = [p.stem for p in _staged_files(ocr_staging())]
        position = stems.index(uid) if uid in stems else 0
        following = stems[position + 1] if position + 1 < len(stems) else ""
        return render(
            request,
            "recipe_edit.html",
            **{
                **_editor_ctx_from_parse(
                    parsed,
                    note="" if parsed.ingredients else "Geen ingrediënten herkend; vul ze zelf in",
                ),
                "staged_photo": f"/recipes/ocr/staged/{photo.name}",
                "stage_uid": uid,
                "stage_position": position + 1,
                "stage_total": len(stems),
                "stage_next": f"/recipes/ocr/{following}" if following else "",
            },
            original_slug=None,
        )

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request):
        all_recipes, errors = load_all()
        people = store().people()
        verdicts = store().verdicts()
        rows = [
            {
                "slug": r.slug,
                "name": r.name,
                "minutes": r.time.total_minutes,
                "protein": r.protein,
                "image": image_path(r.slug),
                "verdicts": {p["id"]: verdicts.get(r.slug, {}).get(p["id"], "") for p in people},
            }
            for r in sorted(all_recipes, key=lambda r: r.name.lower())
        ]
        return render(
            request,
            "recipes.html",
            recipes=rows,
            people=people,
            errors=errors,
        )

    @app.get("/recipes/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html")

    @app.post("/recipes/import")
    async def do_import(
        request: Request,
        photos: Annotated[list[UploadFile] | None, File()] = None,
        text: str = Form(""),
        url: str = Form(""),
    ):
        """Read one recipe from photos (all pages of it), pasted text or a URL."""
        uploads = [p for p in (photos or []) if p.filename]
        try:
            if uploads:
                temp_paths = [await _temp_upload(p) for p in uploads]
                try:
                    parsed, _raw = extract_from_images(
                        temp_paths, binary=tesseract_binary, lang="nld"
                    )
                finally:
                    for temp_path in temp_paths:
                        temp_path.unlink(missing_ok=True)
                used_photo = True
            else:
                used_photo = False
            if not used_photo:
                if url.strip():
                    _recipe, parsed = extract_from_url(url.strip())
                    raw = "\n".join(
                        [parsed.name or "", *(i.product for i in parsed.ingredients), *parsed.steps]
                    )
                else:
                    raw = text
        except Exception as exc:
            return render(request, "import.html", error=f"Importeren mislukt: {exc}")
        if not used_photo:
            parsed = parse_recipe_text(raw)
        return render(
            request,
            "recipe_edit.html",
            **_editor_ctx_from_parse(parsed, url_value=url.strip()),
            original_slug=None,
        )

    async def _temp_upload(upload: UploadFile) -> Path:
        """An upload on disk, for the OCR binary to read."""
        with tempfile.NamedTemporaryFile(
            suffix=_image_ext(upload.filename) or ".jpg", delete=False
        ) as tmp:
            tmp.write(await upload.read())
            return Path(tmp.name)

    def _editor_ctx_from_parse(parsed: ParseResult, url_value: str = "", note: str = "") -> dict:
        """Editor form fields from a parse result (photo, text or URL import)."""
        return editor_ctx(
            parsed=parsed,
            note=note,
            name_value=parsed.name or "",
            servings_value=parsed.servings or 4,
            prep_value=parsed.prep_minutes or 0,
            cook_value=parsed.cook_minutes or 0,
            ingredient_lines="\n".join(
                f"{i.amount:g} {i.unit} {i.product}" for i in parsed.ingredients
            ),
            steps_text="\n".join(number_steps(parsed.steps)),
            protein_value="",
            url_value=url_value,
        )

    def number_steps(lines: list[str]) -> list[str]:
        """Strip existing markers, then number sequentially (stored in YAML)."""
        cleaned = [LIST_MARKER.sub("", line).strip() for line in lines]
        return [f"{i}. {step}" for i, step in enumerate((s for s in cleaned if s), 1)]

    def editor_ctx(**fields) -> dict:
        """Defaults for the edit form; explicit fields win."""
        defaults = {
            "name_value": "",
            "servings_value": 4,
            "prep_value": 0,
            "cook_value": 0,
            "prep_ahead_value": 0,
            "protein_value": "",
            "url_value": "",
            "ingredient_lines": "",
            "steps_text": "",
            "parsed": None,
            "note": "",
            "has_image": False,
            "image_ext": "",
            "images": [],
            "staged_photo": "",
            "stage_uid": "",
            "stage_position": 0,
            "stage_total": 0,
            "stage_next": "",
        }
        return {**defaults, **fields}

    @app.get("/recipes/{slug}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, slug: str):
        try:
            recipe = load_recipe(data / "recipes" / f"{slug}.yaml")
        except RecipeError as exc:
            raise HTTPException(404, str(exc)) from exc
        image = image_path(slug)
        images = [
            {"name": f.name, "featured": f == image, "ext": f.suffix} for f in image_files(slug)
        ]
        return render(
            request,
            "recipe_edit.html",
            **editor_ctx(
                parsed=None,
                name_value=recipe.name,
                servings_value=recipe.servings,
                prep_value=recipe.time.prep,
                cook_value=recipe.time.cook,
                prep_ahead_value=recipe.time.prep_ahead,
                protein_value=recipe.protein or "",
                url_value=recipe.url or "",
                ingredient_lines="\n".join(
                    f"{i.amount:g} {i.unit} {i.product}" for i in recipe.ingredients
                ),
                steps_text="\n".join(number_steps(recipe.steps)),
                has_image=image is not None,
                image_ext=image.suffix if image else "",
                images=images,
            ),
            original_slug=recipe.slug,
        )

    @app.get("/recipes/new", response_class=HTMLResponse)
    def new_recipe_page(request: Request):
        return render(
            request,
            "recipe_edit.html",
            **editor_ctx(),
            original_slug=None,
        )

    @app.get("/recipes/{slug}", response_class=HTMLResponse)
    def recipe_view_page(request: Request, slug: str):
        try:
            recipe = load_recipe(data / "recipes" / f"{slug}.yaml")
        except RecipeError as exc:
            raise HTTPException(404, str(exc)) from exc
        image = image_path(slug)
        return render(
            request,
            "recipe_view.html",
            recipe=recipe,
            image_url=f"/recipes/img/{image.name}" if image else None,
            ingredients=[
                format_amount(i.amount, i.unit) + " " + i.product for i in recipe.ingredients
            ],
        )

    def image_path(slug: str) -> Path | None:
        """Featured image file for a recipe slug, if any."""
        directory = data / "recipes" / "img"
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob(f"{slug}.*")):
            if candidate.suffix.lower() in ALLOWED_IMAGE_EXTS:
                return candidate
        return None

    def image_files(slug: str) -> list[Path]:
        """All images of a recipe: featured first, then the extras."""
        directory = data / "recipes" / "img"
        if not directory.is_dir():
            return []
        extras = sorted(
            p for p in directory.glob(f"{slug}--*") if p.suffix.lower() in ALLOWED_IMAGE_EXTS
        )
        featured = image_path(slug)
        return ([featured] if featured else []) + extras

    def _owned_image(slug: str, filename: str) -> Path:
        """Validate that filename belongs to this slug and return its path."""
        prefix = f"{slug}." if "--" not in filename else f"{slug}--"
        if not filename.startswith(prefix) or Path(filename).suffix.lower() not in (
            ALLOWED_IMAGE_EXTS
        ):
            raise HTTPException(400, "Onbekende afbeelding")
        path = data / "recipes" / "img" / filename
        if not path.is_file():
            raise HTTPException(404, "Geen afbeelding")
        return path

    def _serve_image(slug: str, filename: str):
        if not (filename.startswith(f"{slug}.") or filename.startswith(f"{slug}--")):
            raise HTTPException(404, "Geen afbeelding")
        path = data / "recipes" / "img" / filename
        if not path.is_file():
            raise HTTPException(404, "Geen afbeelding")
        return FileResponse(path)

    @app.get("/recipes/img/{filename}")
    def recipe_image(filename: str):
        return _serve_image(Path(filename).stem, filename)

    def _store_image(slug: str, content: bytes, ext: str, *, as_featured: bool) -> None:
        """Write one photo into the recipe's image dir.

        As featured it replaces the current one (any extension); otherwise it is
        appended as an extra: <slug>--2.<ext>, <slug>--3.<ext>, ...
        """
        directory = data / "recipes" / "img"
        directory.mkdir(parents=True, exist_ok=True)
        if as_featured:
            for stale in directory.glob(f"{slug}.*"):
                stale.unlink(missing_ok=True)
            name = f"{slug}{ext}"
        else:
            number = 2
            while (directory / f"{slug}--{number}{ext}").exists():
                number += 1
            name = f"{slug}--{number}{ext}"
        (directory / name).write_bytes(content)

    async def _save_image_uploads(slug: str, uploads: list[UploadFile]) -> None:
        """Store uploaded images: first becomes featured, the rest extras."""
        uploads = [u for u in uploads if u is not None and u.filename]
        for index, upload in enumerate(uploads):
            ext = _image_ext(upload.filename)
            if not ext:
                raise HTTPException(400, "Afbeelding moet jpg, jpeg, png of webp zijn")
            _store_image(slug, await upload.read(), ext, as_featured=index == 0)

    def _move_images(old_slug: str, new_slug: str) -> None:
        for old in (data / "recipes" / "img").glob(f"{old_slug}*"):
            if old.suffix.lower() not in ALLOWED_IMAGE_EXTS:
                continue
            old.rename(data / "recipes" / "img" / f"{new_slug}{old.name[len(old_slug) :]}")

    @app.post("/recipes/save")
    async def save_form(
        request: Request,
        original_slug: str = Form(""),
        name: str = Form(...),
        servings: int = Form(4),
        prep: int = Form(0),
        cook: int = Form(0),
        prep_ahead: int = Form(0),
        protein: str = Form(""),
        url: str = Form(""),
        ingredients: str = Form(""),
        steps: str = Form(""),
        image: Annotated[list[UploadFile] | None, File()] = None,
        ocr_stage: str = Form(""),
        card_photo: str = Form(""),
    ):
        """Save the editor form; `ocr_stage` retires the reviewed card photo."""
        ingredient_lines = [line.strip() for line in ingredients.splitlines() if line.strip()]
        parsed_ingredients: list[dict] = []
        bad_lines: list[str] = []
        for line in ingredient_lines:
            parsed = parse_ingredient_line(line)
            if parsed is None:
                bad_lines.append(line)
            else:
                parsed_ingredients.append(
                    {"product": parsed.product, "amount": parsed.amount, "unit": parsed.unit}
                )
        if bad_lines:
            return render(
                request,
                "recipe_edit.html",
                **editor_ctx(
                    name_value=name,
                    servings_value=servings,
                    prep_value=prep,
                    cook_value=cook,
                    prep_ahead_value=prep_ahead,
                    protein_value=protein,
                    url_value=url,
                    ingredient_lines=ingredients,
                    steps_text=steps,
                    staged_photo=ocr_staged_url(ocr_stage),
                    stage_uid=ocr_stage,
                ),
                original_slug=original_slug,
                error="Niet herkend (verwacht: <hoeveelheid> <eenheid> <product>): "
                + "; ".join(f"'{line}'" for line in bad_lines),
            )
        recipe = Recipe(
            slug=slugify(name),
            name=name.strip(),
            servings=max(servings, 1),
            time={"prep": prep, "cook": cook, "prep_ahead": prep_ahead},
            protein=protein.strip() or None,
            url=url.strip() or None,
            steps=number_steps(steps.splitlines()),
            ingredients=parsed_ingredients,
        )
        old_path = data / "recipes" / f"{original_slug}.yaml" if original_slug else None
        new_path = data / "recipes" / f"{recipe.slug}.yaml"
        if old_path != new_path and new_path.exists():
            raise HTTPException(400, f"Recept '{recipe.slug}' bestaat al")
        save_recipe(new_path, recipe)
        await _save_image_uploads(recipe.slug, list(image or []))
        if old_path is not None and old_path != new_path:
            old_path.unlink(missing_ok=True)
            store().rename_recipe(original_slug, recipe.slug)
            _move_images(original_slug, recipe.slug)
        if _retire_ocr_card(ocr_stage, recipe.slug, keep=bool(card_photo)):
            # back to the queue: the next card is waiting
            return RedirectResponse("/recipes/ocr?opgeslagen=1", status_code=303)
        return RedirectResponse("/recipes", status_code=303)

    def ocr_staged_url(uid: str) -> str:
        photo = _staged_file(ocr_staging(), uid)
        return f"/recipes/ocr/staged/{photo.name}" if photo else ""

    def _retire_ocr_card(uid: str, slug: str, *, keep: bool) -> bool:
        """Take the reviewed card out of the OCR queue.

        The card leaves the queue either way; keeping it stores it as the
        recipe's photo when that recipe has none yet.
        """
        photo = _staged_file(ocr_staging(), uid)
        if photo is None:
            return False
        if keep:
            _store_image(
                slug, photo.read_bytes(), photo.suffix, as_featured=image_path(slug) is None
            )
        photo.unlink(missing_ok=True)
        return True

    @app.post("/recipes/img/select")
    def select_featured_image(
        slug: str = Form(...), filename: str = Form(...), back: str = Form("")
    ):
        """Make the chosen file the featured image (swap names with the current)."""
        chosen = _owned_image(slug, filename)
        featured = image_path(slug)
        if featured is not None and featured.name != chosen.name:
            tmp = chosen.with_name(chosen.name + ".tmp")
            featured.rename(tmp)
            chosen.rename(featured)
            tmp.rename(chosen)
        return RedirectResponse(back or f"/recipes/{slug}/edit", status_code=303)

    @app.post("/recipes/img/delete")
    def delete_image(slug: str = Form(...), filename: str = Form(...), back: str = Form("")):
        path = _owned_image(slug, filename)
        path.unlink(missing_ok=True)
        return RedirectResponse(back or f"/recipes/{slug}/edit", status_code=303)

    def parse_ingredient_lines(lines: list[str]) -> list[dict]:
        out: list[dict] = []
        for line in lines:
            parsed = parse_ingredient_line(line.strip())
            if parsed is not None:
                out.append(
                    {"product": parsed.product, "amount": parsed.amount, "unit": parsed.unit}
                )
        return out

    def _set_verdict(slug: str, person: str, verdict: str) -> None:
        """Store a verdict, or clear it when the field is back to empty."""
        if verdict in ("love", "ok", "dislike", "never"):
            store().set_verdict(slug, person, verdict)
        else:
            store().clear_verdict(slug, person)

    @app.post("/recipes/verdict")
    def set_verdict_route(slug: str = Form(...), person: str = Form(...), verdict: str = Form("")):
        _set_verdict(slug, person, verdict)
        return RedirectResponse("/recipes", status_code=303)

    # --- components ----------------------------------------------------------

    @app.get("/components", response_class=HTMLResponse)
    def components_page(request: Request):
        all_components, errors = load_components_all()
        people = store().people()
        verdicts = store().verdicts()
        rows = [
            {
                "slug": c.slug,
                "name": c.name,
                "amount": c.amount,
                "unit": c.unit,
                "minutes": c.minutes,
                "verdicts": {p["id"]: verdicts.get(c.slug, {}).get(p["id"], "") for p in people},
            }
            for c in sorted(all_components, key=lambda c: c.name.lower())
        ]
        return render(
            request,
            "components.html",
            components=rows,
            people=people,
            errors=errors,
        )

    def _component_edit_ctx(
        *, title: str, action: str, component: Component | None = None, error: str = ""
    ):
        return {
            "title": title,
            "action": action,
            "name": component.name if component else "",
            "amount": component.amount if component else 1.0,
            "unit": component.unit if component else "stuk",
            "minutes": component.minutes if component else 0,
            "original_slug": component.slug if component else "",
            "error": error,
        }

    @app.get("/components/new", response_class=HTMLResponse)
    def new_component_page(request: Request):
        return render(
            request,
            "component_edit.html",
            **_component_edit_ctx(title="Nieuw onderdeel", action="/components/save"),
        )

    @app.get("/components/{slug}/edit", response_class=HTMLResponse)
    def edit_component_page(request: Request, slug: str):
        try:
            c = load_component(data / "components" / f"{slug}.yaml")
        except ComponentError as exc:
            raise HTTPException(404, str(exc)) from exc
        return render(
            request,
            "component_edit.html",
            **_component_edit_ctx(title=f"Wijzig {c.name}", action="/components/save", component=c),
        )

    @app.post("/components/save")
    def save_component_route(
        name: str = Form(...),
        amount: float = Form(1.0),
        unit: str = Form("stuk"),
        minutes: int = Form(0),
        original_slug: str = Form(""),
    ):
        slug = original_slug if original_slug else slugify(name)
        c = Component(
            slug=slug, name=name.strip(), amount=amount, unit=unit.strip(), minutes=minutes
        )
        save_component(data / "components" / f"{slug}.yaml", c)
        return RedirectResponse("/components", status_code=303)

    @app.post("/components/{slug}/delete")
    def delete_component(slug: str):
        path = data / "components" / f"{slug}.yaml"
        if not path.exists():
            raise HTTPException(404)
        path.unlink()
        return RedirectResponse("/components", status_code=303)

    @app.post("/components/verdict")
    def component_verdict_route(
        slug: str = Form(...), person: str = Form(...), verdict: str = Form("")
    ):
        _set_verdict(slug, person, verdict)
        return RedirectResponse("/components", status_code=303)

    # --- settings ---------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        template = store().week_template()
        evenings = [
            {
                "weekday": wd,
                "name": WEEKDAYS[wd],
                "cook": template[wd][0],
                "minutes": template[wd][1],
                "leftovers": template[wd][2],
            }
            for wd in range(7)
        ]
        return render(
            request,
            "settings.html",
            evenings=evenings,
            people=store().people(),
            adults=[p for p in store().people() if p["role"] == "adult"],
            staples=store().staples(),
            facts=sorted(store().product_facts().values(), key=lambda f: f.product.lower()),
        )

    @app.post("/settings/template")
    async def save_template(request: Request):
        form = await request.form()
        for wd in range(7):
            store().set_evening(
                wd,
                cook=str(form.get(f"cook_{wd}", "")),
                free_minutes=int(str(form.get(f"minutes_{wd}", "45"))),
                allow_leftovers=form.get(f"leftovers_{wd}") == "on",
            )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/people")
    def save_person(
        person_id: str = Form(...),
        name: str = Form(""),
        role: str = Form("adult"),
        weight: float = Form(1.0),
    ):
        store().upsert_person(person_id.strip(), name.strip(), role, float(weight))
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/staples")
    def add_staple(product: str = Form(...)):
        store().set_staple(product.strip(), True)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/staples/remove")
    def remove_staple(product: str = Form(...)):
        store().set_staple(product, False)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/facts")
    def save_fact(
        product: str = Form(...),
        pack_qty: float = Form(...),
        pack_unit: str = Form("stuk"),
        unit_qty: str = Form(""),
        unit_measure: str = Form(""),
        shelf_life_days: int = Form(3),
    ):
        """Declare how one purchase of a product looks, and how long it keeps.

        A product with a fact is bought in whole packs; without one it is bought
        as the summed need, as before. The piece weight is optional and never
        guessed -- an empty field means 'I do not know how much one weighs'.
        """
        try:
            fact = ProductFact(
                product=product.strip(),
                pack_qty=pack_qty,
                pack_unit=pack_unit.strip() or "stuk",
                unit_qty=float(unit_qty) if unit_qty.strip() else None,
                unit_measure=unit_measure.strip() or None,
                shelf_life_days=shelf_life_days,
            )
        except ValidationError as exc:
            return RedirectResponse(
                f"/settings?fact_error={quote(_fact_error(exc))}", status_code=303
            )
        store().set_product_fact(fact)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/facts/delete")
    def delete_fact(product: str = Form(...)):
        store().delete_product_fact(product)
        return RedirectResponse("/settings", status_code=303)

    return app


def _fact_error(exc: ValidationError) -> str:
    """One line out of a validation error, for the settings page."""
    first = exc.errors()[0]
    loc = first.get("loc") or ()
    field = str(loc[-1]) if loc else "verpakking"
    return f"{field}: {first.get('msg', 'ongeldig')}"
