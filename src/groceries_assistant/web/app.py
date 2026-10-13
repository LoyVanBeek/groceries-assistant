"""FastAPI web app: plan -> review -> AH deep-link, recipe ingestion, settings."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import quote, urlparse
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
    DishOption,
    DishPart,
    Ingredient,
    ProductFact,
    Recipe,
    TimeInfo,
    slugify,
)
from groceries_assistant.planner.pantry import StockEntry
from groceries_assistant.planner.shopping import order_product
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
AH_CANDIDATES = 8
"""AH products offered as a component's photo: enough to recognise the right one."""
# staged uploads get a random id as name, so they can never collide
STAGED_UID_RE = re.compile(r"[0-9a-f]{32}\Z")
PHOTO_STAGING = "photo-staging"
OCR_STAGING = "ocr-staging"


def _image_ext(filename: str | None) -> str:
    """Lowercase extension of an upload, if we serve that type."""
    ext = Path(filename or "").suffix.lower()
    return ext if ext in ALLOWED_IMAGE_EXTS else ""


def _fetch_image(url: str) -> tuple[bytes, str] | None:
    """Download an image; returns (bytes, extension), or None if it did not work.

    Uses httpx with HTTP/2 and browser headers because many image CDNs
    (Unilever Assets, Akamai) block HTTP/1.1 requests.
    """
    import httpx

    try:
        ua = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
        with httpx.Client(http2=True, timeout=15, follow_redirects=True) as c:
            resp = c.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5",
                    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.5",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "")
    except Exception:
        return None
    if not content:
        return None
    ext = "".join(Path(urlparse(url).path).suffixes)
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
            content_type.split(";")[0].strip().lower(), ""
        )
    if not ext:
        return None
    return content, ext


# --- the dish form ------------------------------------------------------------
#
# A dish is a list of parts, each part a name and a list of recipes that can fill
# it. The form offers a fixed number of part slots: a name field and one checkbox
# per recipe, so picking three options is three clicks instead of typing slugs.

MIN_PART_SLOTS = 3
"""Slots a fresh dish form shows: enough for 'aardappel, groente, vlees'."""


class OptionRow(TypedDict):
    """One option row of the dish form, as typed.

    The numbers are strings because a row that says nothing is not the same as a
    row that says zero: an empty field inherits the component's own value, which
    is what keeps a shared component shared.
    """

    name: str
    amount: str
    unit: str
    minutes: str


class PartSlot(TypedDict):
    """One row of the dish form: a part's name and the options that can fill it."""

    label: str
    options: list[OptionRow]


def empty_slot() -> PartSlot:
    """One blank part row: a name and nothing chosen yet."""
    return PartSlot(label="", options=[])


def empty_option() -> OptionRow:
    """One blank option row, with the amount and unit a component defaults to."""
    return OptionRow(name="", amount="", unit="", minutes="")


def _text_field(form: FormData, name: str) -> str:
    """One submitted text field; a file part is never a field we asked for."""
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _part_slots(form: FormData) -> list[PartSlot]:
    """The submitted part rows, in order, as the form sent them.

    Each part carries one row per option: a name, plus the amount, unit and
    minutes this dish uses it with. An option with no name is a row the user
    started and left empty, which counts as no option at all.
    """
    indices = sorted(
        {
            int(matched.group(1))
            for key in form
            if (matched := re.fullmatch(r"part_(\d+)", key)) is not None
        }
    )
    rows = []
    for index in indices:
        rows.append(
            PartSlot(
                label=_text_field(form, f"part_{index}"),
                options=_option_rows(form, index),
            )
        )
    return rows


def _option_indices(form: FormData, part: int) -> list[int]:
    """Which option rows part `part` submitted, in order."""
    return sorted(
        {
            int(matched.group(1))
            for key in form
            if (matched := re.fullmatch(rf"opt_{part}_(\d+)_name", key)) is not None
        }
    )


def _option_rows(form: FormData, part: int) -> list[OptionRow]:
    """One part's option rows, blank ones dropped."""
    rows = []
    for index in _option_indices(form, part):
        name = _text_field(form, f"opt_{part}_{index}_name")
        if not name:
            continue
        rows.append(
            OptionRow(
                name=name,
                amount=_text_field(form, f"opt_{part}_{index}_amount"),
                unit=_text_field(form, f"opt_{part}_{index}_unit"),
                minutes=_text_field(form, f"opt_{part}_{index}_minutes"),
            )
        )
    return rows


def _is_row_change(form: FormData) -> bool:
    """Did the click come from an add/remove button rather than save?

    They are buttons in the same form, so the only thing that tells them apart is
    which button carried the name. A row change only reshapes the form: it must
    never write the file, or removing a part would save the dish without it.
    """
    return bool(
        _text_field(form, "more")
        or _text_field(form, "remove")
        or any(
            key.startswith("more_opt_") or key.startswith("rm_opt_")
            for key in form
            if isinstance(key, str)
        )
    )


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
    # '+ optie' and its per-row remove reshape one part's options; the buttons
    # carry the part index in their name, since they sit next to their own rows.
    for key in form:
        matched = re.fullmatch(r"more_opt_(\d+)", key)
        if matched and 0 <= (index := int(matched.group(1))) < len(rows):
            rows[index]["options"].append(empty_option())
    for key in form:
        matched = re.fullmatch(r"rm_opt_(\d+)_(\d+)", key)
        if matched and 0 <= (index := int(matched.group(1))) < len(rows):
            options = rows[index]["options"]
            drop = int(matched.group(2))
            if 0 <= drop < len(options):
                options.pop(drop)
    while len(rows) < MIN_PART_SLOTS:
        rows.append(empty_slot())
    return rows


def _slots_from_parts(
    parts: list[DishPart], by_slug: Mapping[str, object] | None = None
) -> list[PartSlot]:
    """A dish's parts as form rows, with one spare row to add to.

    An option shows only what this dish says about it: a name, and the numbers it
    overrides. The component's own values are not repeated here, because that is
    what an empty field means -- inherit them.
    """
    rows = []
    for part in parts:
        options: list[OptionRow] = []
        for option in part.options:
            obj = (by_slug or {}).get(option.slug)
            options.append(
                OptionRow(
                    name=getattr(obj, "name", option.slug) if obj else option.slug,
                    amount=_number(option.amount),
                    unit=option.unit or "",
                    minutes=_number(option.minutes),
                )
            )
        rows.append(PartSlot(label=part.label, options=options))
    rows.append(empty_slot())
    return rows


def _number(value: float | int | None) -> str:
    """A number as the form shows it, without a trailing '.0'."""
    if value is None:
        return ""
    return f"{value:g}"


def _parts_from_slots(
    slots: list[PartSlot], known: Mapping[str, object] | None = None
) -> tuple[list[DishPart], list[Component], str]:
    """Turn the form's rows into parts and the components they create, or explain
    what is missing.

    A part row nobody filled in is not an error: the form always shows a spare slot
    or two, and the user is not obliged to use them.

    An option names a component or recipe; the numbers next to it are what *this
    dish* uses it with. Numbers left empty are not stored, so the option inherits
    the component's own amount and minutes -- that is what lets one component be
    shared by dishes that each want a different amount. A name that is not a
    component or recipe yet is a new component, returned for the caller to write.
    """
    name_to_slug = {getattr(obj, "name", slug).lower(): slug for slug, obj in (known or {}).items()}

    parts: list[DishPart] = []
    created: dict[str, Component] = {}
    seen: dict[str, str] = {}
    for slot in slots:
        label, rows = slot["label"], slot["options"]
        if not label and not rows:
            continue
        if not label:
            return [], [], "geef elk onderdeel een naam, of typ niets bij de opties"
        if not rows:
            return [], [], f"typ minstens één optie voor '{label}'"
        options: list[DishOption] = []
        try:
            for row in rows:
                option, component = _option_from_row(row, name_to_slug)
                options.append(option)
                if component is not None and component.slug not in created:
                    created[component.slug] = component
                    # a later row naming the same new component resolves to it,
                    # instead of creating it a second time
                    name_to_slug[component.name.lower()] = component.slug
        except DishFormError as exc:
            return [], [], str(exc)
        key = slugify(label)
        if key in seen:
            return (
                [],
                [],
                f"'{label}' en '{seen[key]}' zijn hetzelfde onderdeel; geef ze andere namen",
            )
        seen[key] = label
        parts.append(DishPart.model_validate({"key": key, "label": label, "options": options}))
    if not parts:
        return [], [], "minstens één onderdeel nodig"
    return parts, list(created.values()), ""


class DishFormError(Exception):
    """A dish-form field that cannot be read; the message is shown to the user."""


def _option_from_row(
    row: OptionRow, name_to_slug: dict[str, str]
) -> tuple[DishOption, Component | None]:
    """One option row as a stored option, plus the component it creates (if any).

    The name may be typed as the component's name or its slug, either way; a slug
    that already belongs to a recipe is that recipe, not a new component, since the
    two share one namespace and a clash would make them indistinguishable.

    Empty numbers are not stored, so the option inherits the component's own
    amount and minutes. A name nothing knows yet is a new component, created with
    the numbers the user gave as its defaults.
    """
    typed = row["name"].strip()
    # by name if it is one, otherwise by slug: 'kip-doner' typed literally is that
    # recipe, not a new component that would take the same slug
    slug = name_to_slug.get(typed.lower(), slugify(typed))
    amount = _parse_number(row["amount"], f"het aantal van '{typed}'")
    minutes = _parse_number(row["minutes"], f"de minuten van '{typed}'")
    if amount is not None and amount <= 0:
        raise DishFormError(f"het aantal van '{typed}' moet groter dan 0 zijn")
    if minutes is not None and minutes < 0:
        raise DishFormError(f"de minuten van '{typed}' kunnen niet negatief zijn")
    if minutes is not None and minutes % 1:
        raise DishFormError(f"de minuten van '{typed}' moeten een heel getal zijn")
    unit = row["unit"].strip() or None
    option = DishOption.model_validate(
        {
            "slug": slug,
            "amount": amount,
            "unit": unit,
            "minutes": int(minutes) if minutes is not None else None,
        }
    )
    if slug in name_to_slug.values():
        return option, None
    return option, Component(
        slug=slug,
        name=typed,
        amount=amount or 1.0,
        unit=unit or "stuk",
        minutes=int(minutes or 0),
    )


def _parse_number(raw: str, what: str) -> float | None:
    """A typed number, or None when the field was left empty (inherit)."""
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise DishFormError(f"{what} is geen getal: '{raw}'") from exc


def _option_text(option: DishOption, name: str) -> str:
    """A dish option as the list shows it: only the numbers this dish overrides.

    A dish that agrees with its component says just the name; one that does not
    says what it does differently, e.g. 'Kipfilet (1 kg, 40 min)'.
    """
    bits: list[str] = []
    if option.amount is not None or option.unit:
        amount = "" if option.amount is None else f"{option.amount:g}"
        bits.append(" ".join(piece for piece in (amount, option.unit or "") if piece))
    if option.minutes is not None:
        bits.append(f"{option.minutes} min")
    return f"{name} ({', '.join(bits)})" if bits else name


class OptionChoice(TypedDict):
    """A component or recipe the form suggests, with the values it defaults to."""

    slug: str
    name: str
    minutes: int
    amount: str
    unit: str


def _choices(recipes: list[Recipe]) -> list[OptionChoice]:
    """The recipes a part row may name, for the datalist.

    A composed dish is mostly components ('kipfilet', 'brocolli') with a few whole
    recipes to swap in, so the two are suggested separately. A recipe brings its
    own amounts, so it has none to inherit: only its time is offered as a default.
    """
    return [
        OptionChoice(
            slug=recipe.slug,
            name=recipe.name,
            minutes=recipe.time.total_minutes,
            amount="",
            unit="",
        )
        for recipe in sorted(recipes, key=lambda r: r.name.lower())
    ]


def component_recipe(component: Component) -> Recipe:
    """A component as the engine sees it: the product, and the time to prepare it.

    `servings` is a formality -- the engine wants a number -- but the recipe does
    not scale with it: a component is one purchase, so the amount is what the
    component says whatever the household size is.
    """
    return Recipe(
        slug=component.slug,
        name=component.name,
        servings=4,
        scales_with_servings=False,
        time=TimeInfo(cook=component.minutes),
        ingredients=[
            Ingredient(product=component.name, amount=component.amount, unit=component.unit)
        ],
    )


def _component_choices(components: list[Component]) -> list[OptionChoice]:
    """The components a part row may name, with what each one is worth."""
    return sorted(
        (
            OptionChoice(
                slug=c.slug, name=c.name, minutes=c.minutes, amount=f"{c.amount:g}", unit=c.unit
            )
            for c in components
        ),
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
        (data / folder).mkdir(parents=True, exist_ok=True)
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
                result[c.slug] = component_recipe(c)
        return result

    templates.env.filters["dutch_date"] = dutch_date
    templates.env.filters["option_text"] = _option_text

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
            return render(
                request,
                "plan.html",
                no_recipes=True,
                errors=errors,
                start=date.today(),
            )
        proposal = store().load_proposal()
        if proposal is None:
            start = parse_start(None, start_weekday=store().week_start_weekday())
            dishes = dishes_by_slug()
            components = list(all_known().values())
            dinners_only = all_recipes
            plan = propose(
                dinners_only, store(), start, dishes=list(dishes.values()), components=components
            )
            plan_id = store().save_proposal(start, plan_to_rows(plan), plan_to_picks(plan, dishes))
            proposal = (plan_id, start)
        plan_id, start = proposal[0], proposal[1]
        # Re-anchor an existing proposal to the configured start weekday: shift
        # the days (and with them every swap and part pick) so the change lands
        # immediately without regenerating the plan.
        delta = (store().week_start_weekday() - start.weekday()) % 7
        if delta:
            store().shift_proposal(plan_id, delta)
            start = start + timedelta(days=delta)
        prefs = preferences_from_store(store())
        approved_days = store().plan_approvals(plan_id)
        ordered_days = store().plan_ordered(plan_id)
        evenings = plan_view(
            store(),
            plan_id,
            all_known(),
            [r for r in all_recipes],
            prefs,
            store().people(),
            image_for=image_url_for,
            dishes=list(dishes_by_slug().values()),
            approved_days=approved_days,
            ordered_days=ordered_days,
        )
        last_day = max(e["day_iso"] for e in evenings) if evenings else None
        next_start = date.fromisoformat(last_day) + timedelta(days=1) if last_day else start
        pending = sorted(approved_days - ordered_days)
        return render(
            request,
            "plan.html",
            plan_id=plan_id,
            start=start,
            next_start=next_start,
            order_open=bool(pending),
            order_from=pending[0] if pending else None,
            order_to=pending[-1] if pending else None,
            approved_any=bool(approved_days),
            evenings=evenings,
            errors=errors,
        )

    @app.post("/plan/swap")
    async def plan_swap(
        request: Request,
        day: str = Form(...),
        choice: str = Form(""),
        force: str = Form(""),
    ):
        """Swap one evening's dish; empty/'__keep__' keeps the current pick.

        Changing a day whose groceries have been confirmed ('besteld') is
        allowed, but only with the extra `force` confirmation: the day
        unlocks and the previously bought products are marked 'in huis' so
        they are not bought twice.

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
        confirmed = store().plan_ordered(plan_id)
        was_confirmed = target in confirmed
        if was_confirmed and not force:
            raise HTTPException(
                400,
                "De boodschappen voor deze dag zijn al besteld: vink de bevestiging"
                " aan om het gerecht te wisselen",
            )
        freed = {}
        if was_confirmed:
            freed = _day_ordered_products(plan_id, target)
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
        if was_confirmed:
            store().set_day_ordered(plan_id, target, False)
            for product, unit in freed:
                store().set_order_item_have(plan_id, product, unit, True)
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/swap-days")
    def plan_swap_days(day: str = Form(...), swap_to: str = Form(...)):
        """Ruil twee avonden: de gerechten ruilen van dag.

        The bought groceries travel with their recipe, so a confirmed dish
        can move to another date without losing its 'besteld' status.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        other = date.fromisoformat(swap_to)
        if target == other:
            raise HTTPException(400, "Kies een andere dag om mee te ruilen")
        try:
            store().swap_days(plan_id, target, other)
        except KeyError:
            raise HTTPException(400, "Onbekende dag") from None
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
            if chosen and chosen in part.option_slugs:
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

    def _build_and_save_order(plan_id: int, rows: list[PlanEntryRow]) -> None:
        """Build the grocery order for `rows` and store it for the plan."""
        by_slug = all_known()
        dishes = dishes_by_slug()
        entries = rows_to_entries(rows, by_slug, dishes, store().plan_picks(plan_id))
        if not any(e.kind == "cook" for e in entries):
            raise HTTPException(400, "Minstens één kookavond nodig")
        facts = store().product_facts()
        order = build_order(
            entries,
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

    def _day_ordered_products(plan_id: int, day: date) -> set[tuple[str, str]]:
        """The (product, unit) pairs the saved list has for one evening.

        Used when a confirmed dish is changed anyway: rebuilds just that
        evening so its bought products can be marked 'in huis' instead of
        being bought twice. The saved list is an aggregate over many days,
        so the day's own products are re-derived rather than guessed from
        shared product names.
        """
        row = next((r for r in store().plan_entries(plan_id) if r.day == day), None)
        if row is None or row.kind != "cook" or not row.recipe_slug:
            return set()
        by_slug = all_known()
        dishes = dishes_by_slug()
        entries = rows_to_entries([row], by_slug, dishes, store().plan_picks(plan_id))
        if not any(e.kind == "cook" for e in entries):
            return set()
        facts = store().product_facts()
        order = build_order(
            entries,
            store().staples(),
            servings=SERVINGS,
            facts=facts,
            carried=last_weeks_stock(store(), plan_id, by_slug, dishes, facts, servings=SERVINGS),
        )
        return {(item.product, item.unit) for item in order.shopping}

    @app.post("/plan/approve")
    def plan_approve(day: str = Form("")):
        """Approve one day (per-row goedkeuren), or the whole week.

        A per-row button posts a `day` and approves exactly that day;
        ordering stays separate. With no `day` (the long-standing whole-week
        flow) it approves every planned day, builds the order and redirects
        to /order.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        all_rows = store().plan_entries(plan_id)
        if not all_rows:
            raise HTTPException(400, "Voorstel is leeg")
        store().update_proposal_rows(plan_id, apply_absences(store(), all_rows))
        if day:
            target = date.fromisoformat(day)
            if target not in [r.day for r in all_rows]:
                raise HTTPException(400, "Onbekende dag")
            store().set_day_approved(plan_id, target, True)
            return RedirectResponse("/plan", status_code=303)
        for row in all_rows:
            store().set_day_approved(plan_id, row.day, True)
        _build_and_save_order(plan_id, all_rows)
        for row in all_rows:
            store().set_day_ordered(plan_id, row.day, True)
        return RedirectResponse("/order", status_code=303)

    @app.post("/plan/unapprove")
    def plan_unapprove(day: str = Form(...)):
        """Revoke the goedkeuring of one day (as long as it is not ordered)."""
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        if target in store().plan_ordered(plan_id):
            raise HTTPException(400, "Die dag is al besteld")
        store().set_day_approved(plan_id, target, False)
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/order")
    def plan_order():
        """Build the grocery list for the approved-but-not-yet-confirmed days.

        The list is only ever a preview: days become 'besteld' (locked in,
        hold for ordering) when the shopper confirms the AH links on
        /order/link, not when the list is built here.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        approved_days = store().plan_approvals(plan_id)
        pending = sorted(approved_days - store().plan_ordered(plan_id))
        if not pending:
            raise HTTPException(400, "Nog niets goedgekeurd")
        rows = [r for r in store().plan_entries(plan_id) if r.day in pending]
        _build_and_save_order(plan_id, rows)
        return RedirectResponse("/order", status_code=303)

    @app.post("/plan/advance")
    def plan_advance():
        """Plan the next week: append a fresh week after the proposal's last day.

        Unlike the old +7 shift, this re-proposes the next week, so the no-repeat
        window and preferences pick *different* dishes for it. The current week
        stays visible; the table simply gains another week below.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel om uit te breiden")
        plan_id, _, rows = proposal
        if not rows:
            raise HTTPException(400, "Voorstel is leeg")
        start = max(r.day for r in rows) + timedelta(days=1)
        all_recipes, _errors = load_all()
        dishes = dishes_by_slug()
        components = list(all_known().values())
        plan = propose(
            all_recipes,
            store(),
            start,
            dishes=list(dishes.values()),
            components=components,
        )
        store().extend_proposal(plan_id, plan_to_rows(plan), plan_to_picks(plan, dishes))
        return RedirectResponse("/plan", status_code=303)

    def _match(product_names: list[str]) -> dict[str, tuple[int, str]]:
        if search_fn is not None:
            return match_products(product_names, search_fn, mappings())
        with AHClient() as client:
            return match_products(product_names, client.search, mappings())

    # --- order ---------------------------------------------------------------

    def _product_sources(plan_id: int) -> dict[str, list[dict]]:
        """Which recipes/dishes need each product, for display on the order page.

        Keyed by lowercase product name, exactly as `aggregate_ingredients`
        keys them. A composed dish part attributes to the dish (the combination
        is what the household cooks), not to the component that happens to
        carry the ingredient. Each reference is {name, href} so the order line
        can link straight to the recipe or dish to fix.
        """
        by_slug = all_known()
        dishes = dishes_by_slug()
        entries = rows_to_entries(
            store().plan_entries(plan_id),
            by_slug,
            dishes,
            store().plan_picks(plan_id),
        )
        sources: dict[str, dict[str, str]] = {}
        for entry in entries:
            if entry.kind != "cook":
                continue
            if entry.dish_slug and entry.dish_slug in dishes:
                display, href = dishes[entry.dish_slug].name, f"/dishes/{entry.dish_slug}"
            elif entry.recipe is not None:
                display, href = entry.recipe.name, f"/recipes/{entry.recipe.slug}/edit"
            else:
                continue
            for recipe in entry.dishes:
                for ingredient in recipe.ingredients:
                    name, _note = order_product(ingredient.product)
                    per_product = sources.setdefault(name.lower(), {})
                    per_product.setdefault(display, href)
        return {
            product: [{"name": n, "href": h} for n, h in sorted(names.items())]
            for product, names in sources.items()
        }

    @app.get("/order", response_class=HTMLResponse)
    def order_page(request: Request):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        items = store().order_items(plan_id)
        sources = _product_sources(plan_id)
        # thumbnail of the matched AH article, when one is known
        for item in items:
            mapping = mappings().get(item["product"])
            item["image"] = mapping.ah_image_url if mapping else None
            item["recipes"] = sources.get(item["product"].lower(), [])
        return render(
            request,
            "order.html",
            items=items,
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

    @app.post("/order/confirm")
    def order_confirm():
        """Confirm the AH order went through: lock the days on the list.

        Marks every approved-but-not-yet-confirmed day as 'besteld' (its
        recipe can then only be moved to another date, or changed with an
        extra confirmation). Rebuilds the list first so it always covers the
        days being locked, and so 'in huis' ticks survive.
        """
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        approved_days = store().plan_approvals(plan_id)
        pending = sorted(approved_days - store().plan_ordered(plan_id))
        if not pending:
            raise HTTPException(400, "Niets om te bevestigen")
        rows = [r for r in store().plan_entries(plan_id) if r.day in pending]
        _build_and_save_order(plan_id, rows)
        for day in pending:
            store().set_day_ordered(plan_id, day, True)
        return RedirectResponse("/plan", status_code=303)

    @app.get("/order/match", response_class=HTMLResponse)
    def order_match_page(request: Request):
        """Pin order items to AH article numbers, with live suggestions."""
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        items = store().order_items(plan_id)
        facts = store().product_facts()
        sources = _product_sources(plan_id)
        rows = []
        for item in items:
            item["recipes"] = sources.get(item["product"].lower(), [])
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
        # Unmatched first: that is the work to do. Matched rows become reference.
        unmatched = [row for row in rows if not row["webshop_id"]]
        matched_rows = [row for row in rows if row["webshop_id"]]
        return render(
            request,
            "order_match.html",
            unmatched=unmatched,
            matched=matched_rows,
            fmt=format_amount,
        )

    def _search(query: str, limit: int = 5) -> list:
        """AH product search, using the injected stub or the real client."""
        if search_fn is not None:
            return search_fn(query)[:limit]
        with AHClient() as client:
            return client.search(query, limit=limit)

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

    @app.post("/order/match/backfill-images")
    def order_match_backfill_images():
        """Fetch images for mappings that lack one (older auto-matches).

        For every mapping without an image, the product is searched once;
        the candidate with the same webshop id donates its image URL. Misses
        are left alone — a wrong image is worse than no image.
        """
        mapping_store = mappings()
        targets = [m for m in mapping_store.all() if not m.ah_image_url]
        updated = 0
        if targets:
            by_id = {}
            try:
                if search_fn is not None:
                    for m in targets:
                        by_id.update({c.webshop_id: c for c in search_fn(m.product)})
                else:
                    with AHClient() as client:
                        for m in targets:
                            by_id.update({c.webshop_id: c for c in client.search(m.product)})
            except Exception:
                by_id = {}
            for m in targets:
                hit = by_id.get(m.ah_webshop_id)
                image = getattr(hit, "image_url", None) if hit else None
                if not image:
                    continue
                mapping_store.put(
                    AHMapping(
                        product=m.product,
                        ah_webshop_id=m.ah_webshop_id,
                        ah_title=m.ah_title,
                        score=m.score,
                        confirmed=m.confirmed,
                        ah_image_url=image,
                        ah_unit_size=m.ah_unit_size,
                    )
                )
                updated += 1
        return RedirectResponse(f"/order/match?opgehaald={updated}", status_code=303)

    def _order_links(plan_id: int) -> list[str]:
        """The AH deep-links for the saved list, one per chunk of 25 items."""
        pairs = [
            (int(i["webshop_id"]), 1)
            for i in store().order_items(plan_id)
            if i["webshop_id"] and not i["have"]
        ]
        return [build_add_multiple_url(chunk) for chunk in chunk_items(pairs)]

    @app.get("/order/link", response_class=HTMLResponse)
    def order_link(request: Request):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        links = _order_links(plan_id)
        qrs = [generate_qr_svg(link) for link in links]
        pending = sorted(store().plan_approvals(plan_id) - store().plan_ordered(plan_id))
        return render(
            request,
            "link.html",
            links=links,
            qrs=qrs,
            pending=pending,
            link_confirmed=store().plan_links_confirmed(plan_id),
        )

    @app.post("/order/link/confirm")
    def order_link_confirm(seq: int = Form(...)):
        """Record that the order behind one AH link was placed.

        Each link gets its own confirmation checkbox. The approved-but-not-
        yet-confirmed days lock only once *every* current link is confirmed,
        so placing the order in batches never locks anything early. Like the
        old /order/confirm, the list is rebuilt at that point so it covers
        exactly the days being locked, with 'in huis' ticks preserved.
        """
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        links = _order_links(plan_id)
        if not links or not 0 <= seq < len(links):
            raise HTTPException(400, "Onbekende lijst")
        store().set_link_confirmed(plan_id, seq)
        confirmed = set(store().plan_links_confirmed(plan_id))
        if confirmed >= set(range(len(links))):
            approved_days = store().plan_approvals(plan_id)
            pending = sorted(approved_days - store().plan_ordered(plan_id))
            if pending:
                rows = [r for r in store().plan_entries(plan_id) if r.day in pending]
                _build_and_save_order(plan_id, rows)
                for day in pending:
                    store().set_day_ordered(plan_id, day, True)
                store().clear_links_confirmed(plan_id)
        return RedirectResponse("/order/link", status_code=303)

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
        # What each known component says about itself, as the form's placeholders:
        # an empty field inherits these, so they are shown rather than filled in.
        defaults: dict[str, OptionChoice] = {}
        for c in all_components:
            defaults[c.name] = OptionChoice(
                slug=c.slug,
                name=c.name,
                minutes=c.minutes,
                amount=f"{c.amount:g}",
                unit=c.unit,
            )
        for r in all_recipes:
            defaults.setdefault(
                r.name,
                OptionChoice(
                    slug=r.slug, name=r.name, minutes=r.time.total_minutes, amount="", unit=""
                ),
            )
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
            "defaults": defaults,
            "blank": empty_option(),
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

        parts, created, problem = _parts_from_slots(slots, all_known())
        if problem:
            return again(problem)
        target = slug or slugify(name)
        if not name.strip():
            return again("geef het gerecht een naam")
        if dish_file(dishes_dir(), target).exists() and slug != target:
            return again(f"'{target}' bestaat al")
        # A dish shares its slug space with recipes and components -- including the
        # components this very form creates -- so a name clash would make them
        # indistinguishable.
        taken = set(recipes_by_slug()) | set(components_by_slug()) | {c.slug for c in created}
        if target in taken:
            return again(f"'{target}' is al een recept of onderdeel; kies een andere naam")
        for component in created:
            # Only now, with the dish itself known to be valid, so a rejected form
            # does not leave new components behind that nothing refers to.
            save_component(data / "components" / f"{component.slug}.yaml", component)
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
                _slots_from_parts(dish.parts, all_known()),
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
        missing = sorted(
            {slug for part in parts for slug in part.option_slugs if slug not in known}
        )
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

    def _stage_web_image(url: str) -> str | None:
        """Download an image from a URL to the OCR staging dir; returns uid."""
        fetched = _fetch_image(url)
        if fetched is None:
            return None
        content, ext = fetched
        uid = uuid4().hex
        staging = ocr_staging()
        (staging / f"{uid}{ext}").write_bytes(content)
        return uid

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
                else:
                    raw = text
        except Exception as exc:
            return render(request, "import.html", error=f"Importeren mislukt: {exc}")
        stage_uid = ""
        staged_url = ""
        if not used_photo and url.strip():
            # URL import: use the structured result directly, no flatten/reparse
            if getattr(parsed, "image_url", None):
                try:
                    stage_uid = _stage_web_image(getattr(parsed, "image_url", ""))
                    if stage_uid:
                        staged_url = ocr_staged_url(stage_uid)
                except Exception:
                    pass
        elif not used_photo:
            # text import: flatten then reparse
            parsed = parse_recipe_text(raw)
        ctx = _editor_ctx_from_parse(parsed, url_value=url.strip())
        ctx["staged_photo"] = staged_url
        ctx["stage_uid"] = stage_uid
        return render(
            request,
            "recipe_edit.html",
            **ctx,
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

    def image_url_for(slug: str) -> str | None:
        """URL of the image of a recipe or component, whichever dir holds it.

        Recipes, components and dishes share one slug space, so looking in both
        image dirs is enough to find the file and to name the route that serves
        it. A dish has no image of its own: its parts do.
        """
        image = image_path(slug) or component_image_path(slug)
        if image is None:
            return None
        folder = "recipes" if image.parent == data / "recipes" / "img" else "components"
        return f"/{folder}/img/{image.name}"

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

    @app.post("/recipes/{slug}/delete")
    def recipe_delete(slug: str):
        path = data / "recipes" / f"{slug}.yaml"
        if not path.is_file():
            raise HTTPException(404, "Onbekend recept")
        path.unlink()
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

    def component_image_path(slug: str) -> Path | None:
        """The one image file of a component, if it has one.

        A component is a single product, so unlike a recipe it keeps one photo,
        not a gallery: <slug>.<ext> under the components dir.
        """
        directory = data / "components" / "img"
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob(f"{slug}.*")):
            if candidate.suffix.lower() in ALLOWED_IMAGE_EXTS:
                return candidate
        return None

    def _store_component_image(slug: str, content: bytes, ext: str) -> None:
        """Write a component's photo, replacing whatever it had before."""
        directory = data / "components" / "img"
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob(f"{slug}.*"):
            stale.unlink(missing_ok=True)
        (directory / f"{slug}{ext}").write_bytes(content)

    @app.get("/components/img/{filename}")
    def component_image(filename: str):
        slug = Path(filename).stem
        if not filename.startswith(f"{slug}.") or _image_ext(filename) == "":
            raise HTTPException(404, "Geen afbeelding")
        path = data / "components" / "img" / filename
        if not path.is_file():
            raise HTTPException(404, "Geen afbeelding")
        return FileResponse(path)

    def _component_edit_ctx(
        *,
        title: str,
        action: str,
        component: Component | None = None,
        error: str = "",
        candidates: list[dict] | None = None,
        note: str = "",
    ):
        image = component_image_path(component.slug) if component else None
        return {
            "title": title,
            "action": action,
            "name": component.name if component else "",
            "amount": component.amount if component else 1.0,
            "unit": component.unit if component else "stuk",
            "minutes": component.minutes if component else 0,
            "original_slug": component.slug if component else "",
            "error": error,
            "image": image.name if image else "",
            "candidates": candidates or [],
            "note": note,
        }

    def _ah_candidates(component: Component) -> list[dict]:
        """AH products that could be this component, best match first.

        The image is only a suggestion: AH matches loosely, so the user picks.
        """
        return [
            {
                "webshop_id": hit.webshop_id,
                "title": hit.title,
                "image": getattr(hit, "image_url", None),
            }
            for hit in _search(component.name, AH_CANDIDATES)
        ]

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
        candidates: list[dict] = []
        note = ""
        if request.query_params.get("fout"):
            note = "De afbeelding kon niet worden opgehaald."
        elif request.query_params.get("ah"):
            try:
                candidates = _ah_candidates(c)
                if not candidates:
                    note = "AH.nl vond niets voor deze naam."
            except Exception as exc:
                note = f"AH onbereikbaar ({exc})"
        return render(
            request,
            "component_edit.html",
            **_component_edit_ctx(
                title=f"Wijzig {c.name}",
                action="/components/save",
                component=c,
                candidates=candidates,
                note=note,
            ),
        )

    @app.post("/components/save")
    def save_component_route(
        name: str = Form(...),
        amount: float = Form(1.0),
        unit: str = Form("stuk"),
        minutes: int = Form(0),
        original_slug: str = Form(""),
        photo: Annotated[UploadFile | None, File()] = None,
    ):
        slug = original_slug if original_slug else slugify(name)
        c = Component(
            slug=slug, name=name.strip(), amount=amount, unit=unit.strip(), minutes=minutes
        )
        save_component(data / "components" / f"{slug}.yaml", c)
        ext = _image_ext(photo.filename) if photo is not None else ""
        if photo is not None and ext:
            _store_component_image(slug, photo.file.read(), ext)
        return RedirectResponse("/components", status_code=303)

    @app.post("/components/img/pick")
    def component_image_pick(slug: str = Form(...), webshop_id: int = Form(...)):
        """Store the photo of one AH candidate as the component's own.

        The form carries the webshop id, not the image URL: the search is redone
        so the server never fetches a URL a browser made up.
        """
        component = components_by_slug().get(slug)
        if component is None:
            raise HTTPException(404, "Onbekend onderdeel")
        back = f"/components/{slug}/edit"
        try:
            hits = _search(component.name, AH_CANDIDATES)
        except Exception:
            hits = []
        hit = next((h for h in hits if h.webshop_id == webshop_id), None)
        fetched = _fetch_image(hit.image_url) if hit and hit.image_url else None
        if fetched is None:
            return RedirectResponse(f"{back}?fout=1", status_code=303)
        _store_component_image(slug, *fetched)
        return RedirectResponse(back, status_code=303)

    @app.post("/components/img/delete")
    def component_image_delete(slug: str = Form(...)):
        component = components_by_slug().get(slug)
        if component is None:
            raise HTTPException(404, "Onbekend onderdeel")
        image = component_image_path(slug)
        if image is not None:
            image.unlink()
        return RedirectResponse(f"/components/{slug}/edit", status_code=303)

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
            WEEKDAYS=WEEKDAYS,
            week_start=store().week_start_weekday(),
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
        try:
            week_start = int(str(form.get("week_start", "")))
            if week_start not in range(7):
                raise ValueError
        except ValueError:
            week_start = 0
        store().set_week_start_weekday(week_start)
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
