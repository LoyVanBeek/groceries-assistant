"""FastAPI web app: plan -> review -> AH deep-link, recipe ingestion, settings."""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import tempfile
import threading
import zipfile
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData

from groceries_assistant.ah.bonus import BonusStore, fetch_due, random_bonus_slot
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
    COURSES,
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
from groceries_assistant.planner.engine import PlanEntry, WeeklyPlan
from groceries_assistant.planner.pantry import StockEntry
from groceries_assistant.planner.shopping import order_product
from groceries_assistant.recipes.extract import extract_from_images, unique_recipe_path
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
from groceries_assistant.recipes.urlimport import (
    PASTE_DEFAULT_URL,
    extract_from_url,
    normalize_pasted_page,
)
from groceries_assistant.services.planner_service import (
    ah_pack_qty,
    apply_absences,
    bonus_counts,
    build_order,
    evenings_for,
    fill_remaining,
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
from groceries_assistant.store.barcodes import BarcodeStore, valid_ean
from groceries_assistant.store.db import PlanEntryRow, Store
from groceries_assistant.store.vriezer import VriezerStore
from groceries_assistant.web.helptext import help_for
from groceries_assistant.web.views import (
    SORT_DEFAULT_DIR,
    WEEKDAYS,
    Popularity,
    RecencyBadge,
    dutch_date,
    format_amount,
    plan_view,
    popularity,
    recency,
    restjes_surplus,
    select_options,
)

SERVINGS = 4
LEFTOVERS_CHOICE = "__leftovers__"
COOK_CHOICE = "__cook__"
NO_COOK_CHOICE = "__no_cook__"

logger = logging.getLogger(__name__)

# The daily bonus fetch runs in a background thread: it wakes this often and
# fires once per day at a random slot in the 04:00-09:00 window (see
# ah.bonus.random_bonus_slot).
BONUS_CHECK_INTERVAL = 300  # seconds between staleness checks
VRIEZER_CHOICE = "__vriezer__:"
WEG_CHOICE = "__weg__"

# Bookmarklet for ah.nl recipe pages: copies 'URL\nJSON-LD' to the clipboard.
# ah.nl's bot protection only lets a real browser through, so the JSON is
# grabbed on the page itself; the bulk importer accepts the URL + JSON pair.
RECIPE_BOOKMARKLET = (
    "(function(){var c=document.querySelector('link[rel=\"canonical\"]');"
    "var u=((c&&c.href)||location.href).split('#')[0];"
    "var e=document.querySelectorAll('script[type=\"application/ld+json\"]'),h='';"
    "for(var i=0;i<e.length;i++){"
    "if(e[i].textContent.indexOf('recipeIngredient')>=0){h=e[i].outerHTML;break}}"
    "if(h){navigator.clipboard.writeText(u+'\\n'+h).then("
    "function(){alert('URL + recept-JSON gekopieerd: plak ze in de bulkimport.')})}"
    "else{alert('Geen recept-JSON gevonden op deze pagina.')}})()"
)
# Bookmarklet that posts the recipe straight into the review editor: it builds
# a hidden form on the recipe page and submits it as a navigation to the
# server. A form navigation needs no CORS (unlike fetch) and works from an
# https page to the plain-http home server. @BASE@/@TOKEN@ are filled in per
# request when the import page renders, so a saved bookmarklet keeps working.
RECIPE_POST_BOOKMARKLET = (
    "(function(){var c=document.querySelector('link[rel=\"canonical\"]');"
    "var u=((c&&c.href)||location.href).split('#')[0];"
    "var e=document.querySelectorAll('script[type=\"application/ld+json\"]'),h='';"
    "for(var i=0;i<e.length;i++){"
    "if(e[i].textContent.indexOf('recipeIngredient')>=0){h=e[i].outerHTML;break}}"
    "if(!h){alert('Geen recept-JSON gevonden op deze pagina.');return}"
    "var f=document.createElement('form');f.method='POST';"
    "f.action='@BASE@/recipes/import-paste';f.style.display='none';"
    "var a=function(k,v){var i=document.createElement('input');"
    "i.type='hidden';i.name=k;i.value=v;f.appendChild(i)};"
    "a('token','@TOKEN@');a('url',u);a('paste',h);"
    "document.body.appendChild(f);f.submit()})()"
)
KEEP_CHOICE = ""

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AH_CANDIDATES = 8
"""AH products offered as a component's photo: enough to recognise the right one."""
# staged uploads get a random id as name, so they can never collide
STAGED_UID_RE = re.compile(r"[0-9a-f]{32}\Z")
PHOTO_STAGING = "photo-staging"
OCR_STAGING = "ocr-staging"


def git_hash() -> str:
    """The version indicator's hash: GIT_HASH wins (baked into the image at
    build time), then the checkout's git, then 'dev'. The container has no
    .git, so a docker build passes the hash via the GIT_HASH build-arg."""
    baked = os.environ.get("GIT_HASH")
    if baked:
        return baked
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path(__file__).parent,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "dev"


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


class RecipeRow(TypedDict):
    """One row of the /recipes table, indicators included."""

    slug: str
    name: str
    minutes: int
    protein: str | None
    tags: list[str]
    image: Path | None
    verdicts: dict[str, str]
    badge: Popularity
    favourite: bool
    recency: RecencyBadge


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
    schedule_bonus: bool = False,
) -> FastAPI:
    """search_fn: injectable AH search(product) -> candidates; None = real client.

    schedule_bonus: start the background thread that fetches the AH bonus list
    once per day at a random time (only the real server passes True; tests
    keep it off so they never touch the network).
    """
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
    templates.env.globals["GIT_HASH"] = git_hash()
    templates.env.globals["help_for"] = help_for

    holder: list[Store] = []

    def store() -> Store:
        if not holder:
            holder.append(Store(data / "state.db"))
        return holder[0]

    _mappings_cache: list[AHMappingStore] = []
    _bonus_cache: list[BonusStore] = []
    _barcode_cache: list[BarcodeStore] = []

    def _reset_data_caches() -> None:
        """Sluit de plandatabase en leeg de singletons zodat na een import de
        teruggezette bestanden opnieuw worden ingelezen."""
        if holder:
            holder[0].conn.close()
            holder.clear()
        _mappings_cache.clear()
        _bonus_cache.clear()
        _barcode_cache.clear()

    def mappings() -> AHMappingStore:
        if not _mappings_cache:
            _mappings_cache.append(AHMappingStore(store()))
        return _mappings_cache[0]

    def bonus_store() -> BonusStore:
        """The bonus list singleton: load once, refreshed by the button."""
        if not _bonus_cache:
            _bonus_cache.append(BonusStore(store()))
        return _bonus_cache[0]

    def vriezer() -> VriezerStore:
        """De vriezer: een dunne leeslaag over de vriezer-tabel (live)."""
        return VriezerStore(store())

    def barcodes() -> BarcodeStore:
        """The barcode registry singleton: learns from every scan."""
        if not _barcode_cache:
            _barcode_cache.append(BarcodeStore(store()))
        return _barcode_cache[0]

    # --- daily bonus fetch (button + background scheduler share this) --------

    _bonus_lock = threading.Lock()

    def _ingredient_names() -> list[str]:
        all_recipes, _errors = load_all()
        return sorted({i.product for r in all_recipes for i in r.ingredients})

    def _refresh_bonus_now() -> None:
        """Fetch the current AH bonus list into the cache (manual button and
        the daily scheduler both go through here)."""
        names = _ingredient_names()
        products: list[tuple[int, str]] = []
        if names:
            with AHClient() as client:
                products = [
                    (p.webshop_id, p.title) for p in client.bonus_products_for_queries(names)
                ]
        bonus_store().save(products, date.today())

    def _auto_refresh_bonus(slot: datetime) -> None:
        """One daily fetch from the scheduler thread: it uses its own DB
        connection (WAL allows several), then reloads the app singleton."""
        own_store = Store(data / "state.db")
        try:
            bonus = BonusStore(own_store)
            if not fetch_due(bonus.fetched_at, bonus.last_attempt, now=datetime.now(), slot=slot):
                return
            with _bonus_lock:
                bonus.load()  # the manual button may have refreshed just now
                if not fetch_due(
                    bonus.fetched_at, bonus.last_attempt, now=datetime.now(), slot=slot
                ):
                    return
                bonus.note_attempt()
                names = _ingredient_names()
                products: list[tuple[int, str]] = []
                if names:
                    with AHClient() as client:
                        products = [
                            (p.webshop_id, p.title)
                            for p in client.bonus_products_for_queries(names)
                        ]
                bonus.save(products, datetime.now().date())
            bonus_store().load()  # pages read the singleton: pick up the new list
            logger.info("bonuslijst automatisch ververst (%d producten)", len(products))
        finally:
            own_store.close()

    def _bonus_scheduler_loop(stop: threading.Event) -> None:
        slot_day: date | None = None
        slot: datetime | None = None
        while not stop.wait(BONUS_CHECK_INTERVAL):
            try:
                today = date.today()
                if slot_day != today or slot is None:
                    slot_day = today
                    slot = random_bonus_slot(today)  # re-rolled every day
                if slot is not None and datetime.now() >= slot:
                    _auto_refresh_bonus(slot)
            except Exception:
                logger.exception("bonus automatisch verversen mislukt; oude lijst blijft")

    @app.on_event("startup")
    def _start_bonus_scheduler() -> None:
        if schedule_bonus:
            stop = threading.Event()
            app.state.bonus_stop = stop
            threading.Thread(
                target=_bonus_scheduler_loop,
                args=(stop,),
                name="bonus-daily",
                daemon=True,
            ).start()

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

    def _previous_cook_row(rows: list[PlanEntryRow], day: date) -> PlanEntryRow | None:
        earlier = [r for r in rows if r.day < day and r.kind == "cook" and r.recipe_slug]
        return earlier[-1] if earlier else None

    def _repoint_leftovers(plan_id: int, rows: list[PlanEntryRow]) -> list[PlanEntryRow]:
        """Houd elke restjes-avond aan zijn gerecht gekoppeld.

        Een restjes-avond eet het gerecht waar zijn `source_slug` naar wijst,
        waar die avond in de week ook staat: zolang het gerecht nog ergens
        vóór de restjesdag gekookt wordt, blijft de bron staan en volgen de
        dag-picks de avond waar het gerecht nu staat. Pas als het gerecht
        nergens meer vóór de restjesdag kookt, valt de avond terug op de
        dichtstbijzijnde vorige kookavond (het oude 'avond ervoor' gedrag).
        Restjes uit de vriezer (frozen) staan los van de weekopbouw: hun bron
        is leidend en wordt nooit verplaatst.
        """
        new_rows: list[PlanEntryRow] = []
        for row in rows:
            if row.kind != "leftovers" or row.frozen:
                new_rows.append(row)
                continue
            own = None
            for r in reversed(rows):
                if r.day < row.day and r.kind == "cook" and r.recipe_slug == row.source_slug:
                    own = r
                    break
            source = own if own is not None else _previous_cook_row(rows, row.day)
            if source is None:
                new_rows.append(row)
                continue
            if source.recipe_slug != row.source_slug:
                new_rows.append(
                    PlanEntryRow(
                        day=row.day,
                        kind="leftovers",
                        source_slug=source.recipe_slug,
                        people=row.people,
                        frozen=row.frozen,
                    )
                )
            else:
                new_rows.append(row)
            # de restjes-dag kookt de onderdelen van de bron-dag mee
            fresh = store().plan_picks(plan_id).get(source.day, {})
            if fresh != store().plan_picks(plan_id).get(row.day, {}):
                store().set_picks(plan_id, row.day, fresh)
        return new_rows

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
        today = date.today()
        # Een voorstel dat (vanwege per-dag goedkeuren) 'proposed' bleef staan
        # terwijl de week al voorbij is, zou de pagina voor altijd op die oude
        # week laten staan. Finaliseer zulke voorstellen als historie.
        store().archive_past_proposals(today)
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
            # an empty week: seven unselected evenings to fill by hand per day
            # (or with the fill button); nothing is planned implicitly
            start = parse_start(None, start_weekday=store().week_start_weekday())
            skeleton = [PlanEntryRow(day=start + timedelta(days=i)) for i in range(7)]
            plan_id = store().save_proposal(start, skeleton)
            proposal = (plan_id, start)
        plan_id, start = proposal[0], proposal[1]
        # Re-anchor an existing proposal to the configured start weekday: shift
        # the days (and with them every swap and part pick) so the change lands
        # immediately without regenerating the plan.
        delta = (store().week_start_weekday() - start.weekday()) % 7
        if delta:
            store().shift_proposal(plan_id, delta)
            start = start + timedelta(days=delta)
        # Oudere voorstellen kunnen dagen missen: de planner liet avonden weg
        # waar niets op paste (te weinig tijd, geen restjes meer). Met de
        # verwachte reeks (start..start+6) aanvullen als lege kookavonden,
        # zodat de week weer de volledige reeks telt.
        expected = {start + timedelta(days=i) for i in range(7)}
        rows = store().plan_entries(plan_id)
        if missing := sorted(expected - {r.day for r in rows}):
            store().update_proposal_rows(
                plan_id,
                sorted(
                    [*rows, *(PlanEntryRow(day=day, kind="cook") for day in missing)],
                    key=lambda r: r.day,
                ),
            )
        approved_days = store().plan_approvals(plan_id)
        ordered_days = store().plan_ordered(plan_id)
        all_evenings = plan_view(
            store(),
            plan_id,
            all_known(),
            store().people(),
            image_for=image_url_for,
            dishes=list(dishes_by_slug().values()),
            approved_days=approved_days,
            ordered_days=ordered_days,
            vriezer=vriezer(),
        )
        last_day = max(e["day_iso"] for e in all_evenings) if all_evenings else None
        next_start = date.fromisoformat(last_day) + timedelta(days=1) if last_day else start
        # Toon standaard alleen deze week en later. Een doorlopend voorstel kan
        # weken meegroeien; de afgelopen weken zijn verleden en blijven via
        # ?past=1 (de 'toon eerdere dagen'-knop) bekijkbaar.
        show_past = request.query_params.get("past") == "1"
        current_week = today - timedelta(days=(today.weekday() - store().week_start_weekday()) % 7)
        past_count = sum(1 for e in all_evenings if date.fromisoformat(e["day_iso"]) < current_week)
        hidden_past = 0 if show_past else past_count
        evenings = all_evenings
        if not show_past:
            evenings = [e for e in all_evenings if date.fromisoformat(e["day_iso"]) >= current_week]
        display_start = start if show_past else max(start, current_week)
        pending = sorted(approved_days - ordered_days)
        rows_now = store().plan_entries(plan_id)
        week_empty = all(r.kind == "cook" and r.recipe_slug is None for r in rows_now)
        week_partly = not week_empty and any(
            r.kind == "cook" and r.recipe_slug is None for r in rows_now
        )
        return render(
            request,
            "plan.html",
            plan_id=plan_id,
            start=display_start,
            next_start=next_start,
            week_empty=week_empty,
            week_partly=week_partly,
            vriezer_total=sum(vriezer().all().values()),
            order_open=bool(pending),
            order_from=pending[0] if pending else None,
            order_to=pending[-1] if pending else None,
            approved_any=bool(approved_days),
            evenings=evenings,
            hidden_past=hidden_past,
            past_count=past_count,
            show_past=show_past,
            errors=errors,
        )

    @app.post("/plan/fill")
    def plan_fill():
        """Fill the week with a planner proposal: from nothing, or by
        replacing an untouched week. Selections are never overwritten."""
        all_recipes, _errors = load_all()
        existing = store().load_proposal()
        entries = store().plan_entries(existing[0]) if existing else []
        untouched = not entries or all(r.kind == "cook" and r.recipe_slug is None for r in entries)
        if all_recipes and untouched:
            start = parse_start(None, start_weekday=store().week_start_weekday())
            dishes = dishes_by_slug()
            plan = propose(
                all_recipes,
                store(),
                start,
                dishes=list(dishes.values()),
                components=list(all_known().values()),
                bonus=bonus_counts(all_recipes, bonus_store(), mappings()),
                vriezer=vriezer().available_all(),
            )
            store().save_proposal(
                start,
                plan_to_rows(plan, days=[start + timedelta(days=i) for i in range(7)]),
                plan_to_picks(plan, dishes),
            )
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/fill-remaining")
    def plan_fill_remaining():
        """Fill only the evenings without a dish; manual picks stay."""
        all_recipes, _errors = load_all()
        proposal = store().load_proposal()
        if proposal is None or not all_recipes:
            return RedirectResponse("/plan", status_code=303)
        plan_id = proposal[0]
        rows = store().plan_entries(plan_id)
        if all(r.kind == "cook" and r.recipe_slug is None for r in rows):
            return RedirectResponse("/plan", status_code=303)  # use "Vul de week in"
        new_rows, new_picks = fill_remaining(
            rows,
            store(),
            store().plan_picks(plan_id),
            all_recipes,
            dishes=list(dishes_by_slug().values()),
            components=list(all_known().values()),
            bonus=bonus_counts(all_recipes, bonus_store(), mappings()),
            vriezer=vriezer().available_all(),
        )
        store().update_proposal_rows(plan_id, new_rows)
        for day, day_picks in new_picks.items():
            store().set_picks(plan_id, day, day_picks)
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/clear")
    def plan_clear():
        """Wipe this week and every planned-ahead week; past weeks stay."""
        today = date.today()
        cleared = store().clear_future_plans(today)
        # Een resterend voorstel dat volledig in het verleden ligt is geen
        # actieve week meer; finaliseer het zodat de pagina een nieuwe week opent.
        store().archive_past_proposals(today)
        return RedirectResponse(f"/plan?cleared={cleared}", status_code=303)

    @app.post("/plan/bonus/refresh")
    def bonus_refresh(back: str = Form("/plan")):
        """Fetch the current AH bonus list now (same fetch the daily scheduler
        runs). Catches everything: a failing fetch is a notice on the page you
        came from, never a crash."""
        if not back.startswith("/plan"):
            back = "/plan"  # no open redirects
        try:
            with _bonus_lock:
                _refresh_bonus_now()
        except Exception as exc:  # the button must never 500
            separator = "&" if "?" in back else "?"
            return RedirectResponse(
                f"{back}{separator}error=bonus:{quote(str(exc))}", status_code=303
            )
        return RedirectResponse(back, status_code=303)

    @app.get("/plan/select", response_class=HTMLResponse)
    def plan_select(
        request: Request,
        day: str = "",
        q: str = "",
        sort: str = "score",
        dir: str = "",
        tag: str = "",
        course: str = "hoofdgerecht",
    ):
        """Pick one evening's dish on its own page.

        Every option is the same /plan/swap form the old dropdown posted; the
        page only adds the indicators (how well liked, how recently planned,
        favourite) and, for a 'besteld' day, the same 'force' gate. The day's
        current dish is excluded so the list starts where the old picker did.
        """
        proposal = store().load_proposal()
        if proposal is None or not day:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        try:
            target = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "Ongeldige datum") from None
        rows = store().plan_entries(plan_id)
        # de gang is tegelijk filter en slot: 'hoofdgerecht'/''
        # = de hoofdrij van de dag, 'soep' e.d. = dat gangen-slot
        slot_course = "" if course in ("", "hoofdgerecht") else course
        current = next(
            (r for r in rows if r.day == target and (r.course or "") == slot_course), None
        )
        if current is None and not any(r.day == target for r in rows):
            raise HTTPException(404, "Geen avond die dag")
        all_recipes, errors = load_all()
        prefs = preferences_from_store(store())
        people = store().people()
        evening = evenings_for(store(), target, days=1)[0]
        known = all_known()
        # slug -> tags, with composed dishes inheriting the union of the tags
        # of the recipes/components that may fill their parts
        tags_by_slug: dict[str, list[str]] = {slug: r.tags for slug, r in known.items()}
        for dish in dishes_by_slug().values():
            dish_tags: set[str] = set()
            for part in dish.parts:
                for opt in part.options:
                    dish_tags.update(tags_by_slug.get(opt.slug, ()))
            tags_by_slug[dish.slug] = sorted(dish_tags)
        course_by_slug = {slug: obj.course for slug, obj in known.items()}
        course_by_slug.update({slug: d.course for slug, d in dishes_by_slug().items()})
        options = select_options(
            all_recipes,
            prefs,
            evening,
            image_url_for,
            # een gangen-slot vult geen dag-gerecht (zijn parts-keuze hoort bij
            # het hoofdslot); het slots' huidige gerecht valt weg
            list(dishes_by_slug().values()) if not slot_course else [],
            (current.recipe_slug or current.source_slug or "") if current else "",
            by_slug=known,
            history=store().history(),
            verdicts_by_slug=store().verdicts(),
            people=people,
            today=date.today(),
            q=q,
            sort=sort,
            tags_by_slug=tags_by_slug,
            tag=tag,
            course=course,
            course_by_slug=course_by_slug,
            bonus=bonus_counts(all_recipes, bonus_store(), mappings()),
            bonus_store=bonus_store(),
            mappings=mappings(),
            direction=dir if dir in ("asc", "desc") else "",
        )
        # restjes-optie: het gerecht van de avond ervoor, als er porties
        # over zijn (servings minus de eters minus alle geplande restjes)
        prev_slug = _previous_cook_slug(rows, target)
        restjes_option = None
        if prev_slug:
            prev_recipe = known.get(prev_slug)
            prev_row = next(
                (r for r in rows if r.day < target and r.recipe_slug == prev_slug), None
            )
            prev_people = prev_row.people if prev_row else 4
            restjes_gepland = sum(
                (r.people or 0)
                for r in rows
                if r.kind == "leftovers" and r.source_slug == prev_slug and r.day != target
            )
            if prev_recipe is not None and prev_recipe.servings > prev_people + restjes_gepland:
                restjes_option = {
                    "day_iso": target.isoformat(),
                    "name": prev_recipe.name,
                    "portions": prev_recipe.servings - prev_people - restjes_gepland,
                }
        # bevroren restjes: het langst ingevroren eerst, met een waarschuwing
        # als de BESCHIKBARE porties (voorraad minus al gepland eruit) niet
        # genoeg zijn voor de eters van deze avond
        dishes = dishes_by_slug()
        joiners_count = (
            len(evening.joiners) if evening.joiners is not None else 4
        ) + evening.guests
        frozen_options = []
        for e in vriezer().entries():
            slug = e["slug"]
            name = (
                known[slug].name
                if slug in known
                else (dishes[slug].name if slug in dishes else slug)
            )
            age = (date.today() - date.fromisoformat(e["since"])).days if e["since"] else None
            available = vriezer().available(slug)
            frozen_options.append(
                {
                    "slug": slug,
                    "name": name,
                    "portions": available,
                    "age": age,
                    "fits": available >= joiners_count,
                }
            )
        # de ingrediëntenregels linken naar hun AH-koppeling; 'terug' van de
        # koppelpagina komt terug naar exact deze zoekopdracht
        select_back = "/plan/select?" + "&".join(
            f"{name}={quote(value)}"
            for name, value in (
                ("day", day),
                ("q", q),
                ("sort", sort),
                ("tag", tag),
                ("course", course),
            )
            if value
        )
        ingredient_links = {
            m.product.lower(): {
                "url": product_url(m.ah_webshop_id),
                "title": m.ah_title,
            }
            for m in mappings().all()
        }

        def _sort_href(col: str) -> str:
            # clicking the active column toggles asc/desc (the default view's
            # column counts as its default direction); a new column gets its
            # default direction; the current filters ride along
            current_dir = dir if dir in ("asc", "desc") else SORT_DEFAULT_DIR.get(sort, "desc")
            if sort == col:
                next_dir = "asc" if current_dir == "desc" else "desc"
            else:
                next_dir = SORT_DEFAULT_DIR[col]
            params = {
                "day": day,
                "sort": col,
                "dir": next_dir,
                "q": q,
                "tag": tag,
                "course": course,
            }
            return "/plan/select?" + urlencode({k: v for k, v in params.items() if v})

        sort_links = {col: _sort_href(col) for col in SORT_DEFAULT_DIR}
        return render(
            request,
            "select.html",
            day=day,
            q=q,
            sort=sort,
            dir=dir if dir in ("asc", "desc") else "",
            sort_links=sort_links,
            tag=tag,
            restjes_option=restjes_option,
            frozen_options=frozen_options,
            frozen_count=len(frozen_options),
            frozen_total=sum(f["portions"] for f in frozen_options),
            all_courses=list(COURSES),
            all_tags=sorted({t for tags in tags_by_slug.values() for t in tags}),
            label=f"{WEEKDAYS[target.weekday()].capitalize()} {target.strftime('%d-%m')}",
            options=options,
            select_back=select_back,
            ingredient_links=ingredient_links,
            confirmed=target in store().plan_ordered(plan_id),
            course=course,
            slot_course=slot_course,
            joiners_count=joiners_count,
            is_no_cook=(current.kind == "no_cook") if current else False,
            bonus_count=len(bonus_store().products),
            bonus_fetched=bonus_store().fetched_at,
            can_leftovers=(
                (current is None or current.kind in ("cook", "no_cook"))
                and not slot_course
                and _previous_cook_slug(rows, target) is not None
            ),
            errors=errors,
        )

    @app.post("/plan/swap")
    async def plan_swap(
        request: Request,
        day: str = Form(...),
        choice: str = Form(""),
        course: str = Form(""),
        force: str = Form(""),
    ):
        """Swap one evening's dish; empty/'__keep__' keeps the current pick.

        Changing a day whose groceries have been confirmed ('besteld') is
        allowed, but only with the extra `force` confirmation: the day
        unlocks and the previously bought products are marked 'in huis' so
        they are not bought twice.

        With `course` set, only that course slot of the day changes (a soep-,
        nagerecht-... row, created on the fly): the day's main row and its
        other courses stay. '__weg__' removes the course slot again.

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
        if choice in (KEEP_CHOICE, "__keep__") and not course:
            return RedirectResponse("/plan", status_code=303)
        confirmed = store().plan_ordered(plan_id)
        was_confirmed = target in confirmed
        if was_confirmed and not force:
            raise HTTPException(
                400,
                "De boodschappen voor deze dag zijn al besteld: vink de bevestiging"
                " aan om het gerecht te wisselen",
            )
        if not was_confirmed and target in store().plan_approvals(plan_id):
            # een goedgekeurde avond staat vast tot de goedkeuring wordt
            # ingetrokken; pas daarna kan het gerecht gewisseld worden
            return RedirectResponse("/plan?error=goedkeuring", status_code=303)
        freed = {}
        if was_confirmed:
            freed = _day_ordered_products(plan_id, target)
        new_rows: list[PlanEntryRow] = []
        if course:
            # een gangen-slot: alleen die rij verandert; de hoofdrij en de
            # andere gangen van de dag blijven staan. '__weg__' haalt de gang
            # weg; de speciale hoofdslot-keuzes doen hier niets (een
            # receptslug begint nooit met '__').
            new_rows.extend(r for r in rows if r.day != target or r.course != course)
            if choice not in (KEEP_CHOICE, "__keep__", WEG_CHOICE) and not choice.startswith("__"):
                new_rows.append(
                    PlanEntryRow(
                        day=target, kind="cook", recipe_slug=choice, people=4, course=course
                    )
                )
        else:
            new_rows.extend(r for r in rows if r.day != target or r.course)
            for row in rows:
                if row.day != target or row.course:
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
                elif choice.startswith(VRIEZER_CHOICE):
                    # restjes uit de vriezer eten: alleen wat de eters nodig
                    # hebben gaat van de voorraad af, de rest blijft liggen
                    slug = choice[len(VRIEZER_CHOICE) :]
                    portions = vriezer().available(slug)
                    if portions <= 0:
                        raise HTTPException(400, "Geen restjes van dit gerecht in de vriezer")
                    evening = evenings_for(store(), target, days=1)[0]
                    eat = min(
                        portions,
                        (len(evening.joiners) if evening.joiners is not None else 4)
                        + evening.guests,
                    )
                    new_rows.append(
                        PlanEntryRow(
                            day=target,
                            kind="leftovers",
                            source_slug=slug,
                            people=eat,
                            frozen=True,
                        )
                    )
                    # de voorraad verandert pas als de maaltijd bevestigd is
                    source = WEEKDAYS[target.weekday()][:2].lower() + target.strftime(" %d-%m")
                    vriezer().plan_take(slug, eat, source)
                elif choice == NO_COOK_CHOICE:
                    # eating elsewhere: no dish, no groceries, no cook needed
                    new_rows.append(PlanEntryRow(day=target, kind="no_cook", people=0))
                else:
                    new_rows.append(
                        PlanEntryRow(day=target, kind="cook", recipe_slug=choice, people=4)
                    )
        new_rows.sort(key=lambda r: (r.day, r.course))
        new_rows = _repoint_leftovers(plan_id, new_rows)
        store().update_proposal_rows(plan_id, new_rows)
        if not course:
            # picks horen bij het hoofdslot; een gangen-slot heeft geen parts
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
        store().update_proposal_rows(
            plan_id, _repoint_leftovers(plan_id, store().plan_entries(plan_id))
        )
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/restjes")
    def plan_restjes(source_day: str = Form(...), day: str = Form(...)):
        """Plan the restjes of `source_day`'s dish on a later, empty day."""
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        source = date.fromisoformat(source_day)
        target = date.fromisoformat(day)
        if target <= source:
            return RedirectResponse("/plan?error=restjes-vroeg", status_code=303)
        try:
            store().add_restjes(plan_id, source, target)
        except ValueError:
            return RedirectResponse("/plan?error=restjes-bezet", status_code=303)
        store().update_proposal_rows(
            plan_id, _repoint_leftovers(plan_id, store().plan_entries(plan_id))
        )
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/restjes-vriezer")
    def plan_restjes_vriezer(day: str = Form(...)):
        """Bewaar de overgebleven porties van deze dag in de vriezer."""
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        rows = store().plan_entries(plan_id)
        row = next((r for r in rows if r.day == target), None)
        if row is None or row.kind != "cook" or not row.recipe_slug:
            return RedirectResponse("/plan?error=restjes-geen", status_code=303)
        gepland: dict[str, int] = {}
        for r in rows:
            if r.kind == "leftovers" and r.source_slug:
                gepland[r.source_slug] = gepland.get(r.source_slug, 0) + (r.people or 0)
        portions, slug = restjes_surplus(
            row,
            store().plan_picks(plan_id).get(target, {}),
            all_known(),
            dishes_by_slug(),
            gepland,
            vriezer(),
        )
        if portions <= 0 or slug is None:
            return RedirectResponse("/plan?error=restjes-geen", status_code=303)
        source = WEEKDAYS[target.weekday()][:2].lower() + target.strftime(" %d-%m")
        vriezer().plan_add(slug, portions, source)
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/vriezer-eten")
    def plan_vriezer_eten(day: str = Form(...), slug: str = Form(...)):
        """Eet de bevroren restjes van `slug` op `day`.

        De sleep van de ❄-chip posts hier. Alleen een lege kookavond mag de
        porties opnemen (bezette dagen worden geweigerd, net als bij de
        restjes-chip); de avond wordt een vriezer-restjes rij (frozen: de
        bron schuift nooit mee) en de porties gaan van de voorraad af.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        rows = store().plan_entries(plan_id)
        row = next((r for r in rows if r.day == target), None)
        if row is None or row.kind != "cook" or row.recipe_slug is not None:
            return RedirectResponse("/plan?error=vriezer-bezet", status_code=303)
        if target in store().plan_approvals(plan_id):
            return RedirectResponse("/plan?error=goedkeuring", status_code=303)
        portions = vriezer().available(slug)
        if portions <= 0:
            return RedirectResponse("/plan?error=restjes-geen", status_code=303)
        evening = evenings_for(store(), target, days=1)[0]
        eat = min(
            portions,
            (len(evening.joiners) if evening.joiners is not None else 4) + evening.guests,
        )
        new_rows = [
            (
                PlanEntryRow(
                    day=r.day,
                    kind="leftovers",
                    source_slug=slug,
                    people=eat,
                    frozen=True,
                )
                if r.day == target
                else r
            )
            for r in rows
        ]
        # de voorraad verandert pas als de maaltijd bevestigd is
        source = WEEKDAYS[target.weekday()][:2].lower() + target.strftime(" %d-%m")
        vriezer().plan_take(slug, eat, source)
        new_rows = _repoint_leftovers(plan_id, new_rows)
        store().update_proposal_rows(plan_id, new_rows)
        return RedirectResponse("/plan", status_code=303)

    @app.post("/plan/recipe-order")
    def plan_recipe_order(
        day: str = Form(...),
        recipe_slug: str = Form(...),
        people: int = Form(4),
        course: str = Form(""),
    ):
        """Generate an AH shopping list + QR code for a single recipe.

        Used to order ingredients for one specific recipe on a specific day
        with a custom number of people. The day/course identifies the slot,
        but the list is built only for this one recipe.
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)

        # Build a mini plan with just this one recipe
        row = next(
            (
                r
                for r in store().plan_entries(plan_id)
                if r.day == target and (r.course or "") == course
            ),
            None,
        )
        if row is None:
            # No existing slot: create a temporary entry for this recipe
            PlanEntryRow(
                day=target,
                kind="cook",
                recipe_slug=recipe_slug,
                people=people,
                course=course,
            )
        else:
            # Replace the existing row's recipe/people for this order
            PlanEntryRow(
                day=target,
                kind=row.kind,
                recipe_slug=recipe_slug,
                source_slug=row.source_slug,
                people=people,
                frozen=row.frozen,
                course=row.course,
            )

        all_recipes, _errors = load_all()
        known = {r.slug: r for r in all_recipes}
        dishes = dishes_by_slug()
        all_known_dict = {**known, **{d.slug: d for d in dishes.values()}}
        by_slug = all_known_dict
        evening = evenings_for(store(), target, days=1)[0]
        facts = store().product_facts()
        carried = last_weeks_stock(
            store(), plan_id, by_slug, list(dishes.values()), facts, SERVINGS
        )
        staples = store().staples()

        # Create a temporary plan with just this entry
        temp_plan = WeeklyPlan(
            entries=[
                PlanEntry(
                    date=target,
                    recipe=by_slug.get(recipe_slug),
                    dish_slug=recipe_slug if recipe_slug in dishes_by_slug() else None,
                    availability=evening,
                    people=people or SERVINGS,
                    kind="cook",
                    source_slug=recipe_slug if recipe_slug not in by_slug else None,
                    score=0.0,
                    reason="Handmatige bestelling voor recept",
                )
            ]
        )
        order = build_order(
            [temp_plan.entries[0]],
            staples,
            servings=people or SERVINGS,
            facts=facts,
            carried=carried,
        )
        matched = _match([i.product for i in order.shopping])
        pairs = [
            (int(matched.get(item.product, (None, ""))[0]), max(int(item.amount), 1))
            for item in order.shopping
            if matched.get(item.product)
        ]
        link = build_add_multiple_url(pairs[:25]) if pairs else ""
        qr = generate_qr_svg(link) if link else ""

        return {
            "link": link,
            "qr": qr,
            "items": [
                {
                    "product": item.product,
                    "unit": item.unit,
                    "amount": item.amount,
                    "webshop_id": matched.get(item.product, (None, ""))[0],
                    "pack_count": item.pack_count,
                    "pack_unit": item.pack_unit,
                    "note": item.note,
                }
                for item in order.shopping
            ],
            "warnings": order.warnings,
        }

    @app.post("/plan/restjes-vriezen")
    def plan_restjes_vriezen(day: str = Form(...)):
        """De geplande restjes van deze avond gaan naar de vriezer.

        De avond (het hoofdslot) wordt leeg en zijn porties gaan de voorraad
        in, gedateerd vandaag; goedkeuring vervalt mee (de dag verandert).
        """
        proposal = store().load_proposal()
        if proposal is None:
            raise HTTPException(404, "Geen voorstel")
        plan_id = proposal[0]
        target = date.fromisoformat(day)
        rows = store().plan_entries(plan_id)
        row = next((r for r in rows if r.day == target and not r.course), None)
        if row is None or row.kind != "leftovers" or not row.source_slug:
            return RedirectResponse("/plan?error=restjes-geen", status_code=303)
        if target in store().plan_approvals(plan_id):
            return RedirectResponse("/plan?error=goedkeuring", status_code=303)
        source = WEEKDAYS[target.weekday()][:2].lower() + target.strftime(" %d-%m")
        vriezer().plan_add(row.source_slug, row.people or 4, source)
        new_rows = [
            (
                PlanEntryRow(
                    day=r.day,
                    kind="cook",
                    recipe_slug=None,
                    source_slug=None,
                    people=4,
                    course=r.course,
                )
                if (r.day == target and not r.course)
                else r
            )
            for r in rows
        ]
        new_rows = _repoint_leftovers(plan_id, new_rows)
        store().update_proposal_rows(plan_id, new_rows)
        store().set_day_approved(plan_id, target, False)
        return RedirectResponse("/plan", status_code=303)

    def _row_dish_slug(rows: list[PlanEntryRow], day: date) -> str:
        """The composed dish (if any) an evening's main slot had before the swap."""
        row = next((r for r in rows if r.day == day and not r.course), None)
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
        if target in store().plan_approvals(plan_id):
            return RedirectResponse("/plan?error=goedkeuring", status_code=303)
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
        store().update_proposal_rows(
            plan_id, _repoint_leftovers(plan_id, store().plan_entries(plan_id))
        )
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
        minutes_raw = str(form.get("minutes", "")).strip()
        target = date.fromisoformat(day)

        # Handle attendance toggle for a specific person
        person_id = str(form.get("person_id", "")).strip()
        if person_id:
            joined = form.get("joined") == "1"
            store().set_absence(target, person_id, not joined)
            return RedirectResponse("/plan", status_code=303)

        # Full attendance update (when cook is changed or explicitly submitted)
        joined = {str(v) for v in form.getlist("joined")}
        if cook:
            people_by_id = {p["id"]: p for p in store().people()}
            person = people_by_id.get(cook)
            if person is None or person["role"] != "adult" or cook not in joined:
                return RedirectResponse("/plan?error=kok", status_code=303)

        minutes_raw = str(form.get("minutes", "")).strip()
        target = date.fromisoformat(day)
        minutes = int(minutes_raw) if minutes_raw.isdigit() else evening_minutes(store(), target)
        # guests vary per day: the +/- buttons submit guests_delta with the form
        guests_delta = str(form.get("guests_delta", "")).strip()
        guests = evening_guests(store(), target)
        if guests_delta in ("-1", "1"):
            guests = max(0, min(10, guests + int(guests_delta)))
        store().set_week_override(
            target,
            cook=cook,
            free_minutes=max(minutes, 0),
            allow_leftovers=evening_allows_leftovers(store(), target),
            guests=guests,
        )
        # Update attendance for all people when cook is provided (even if empty string)
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

    def evening_guests(s: Store, day: date) -> int:
        """Guests eating this specific day (per-day override, default 0)."""
        override = s.week_override(day)
        return override[3] if override else 0

    def _build_and_save_order(plan_id: int, rows: list[PlanEntryRow]) -> None:
        """Build the grocery order for `rows` and store it for the plan."""
        by_slug = all_known()
        dishes = dishes_by_slug()
        entries = rows_to_entries(rows, by_slug, dishes, store().plan_picks(plan_id))
        if not any(e.kind == "cook" for e in entries):
            raise HTTPException(400, "Minstens één kookavond nodig")
        facts = store().product_facts()
        # staples ticked on /order are ordered like anything else this week
        wanted = store().plan_staples(plan_id)
        staples = [s for s in store().staples() if s.lower() not in wanted]
        order = build_order(
            entries,
            staples,
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
        # A ticked staple the covered days' cooking does not use is a restock:
        # it still belongs on the list, as one pack of whatever AH sells.
        on_list = {item.product.lower() for item in order.shopping}
        for product in sorted(wanted - on_list):
            store().add_order_item(
                plan_id, product, webshop_id=_match([product]).get(product, (None, ""))[0]
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
        # staples ticked on /order are ordered like anything else this week
        wanted = store().plan_staples(plan_id)
        staples = [s for s in store().staples() if s.lower() not in wanted]
        order = build_order(
            entries,
            staples,
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
            bonus=bonus_counts(all_recipes, bonus_store(), mappings()),
            vriezer=vriezer().available_all(),
        )
        store().extend_proposal(
            plan_id,
            plan_to_rows(plan, days=[start + timedelta(days=i) for i in range(7)]),
            plan_to_picks(plan, dishes),
        )
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
        facts = store().product_facts()
        unit_sizes = _unit_sizes()
        # the staples checklist: everything you usually have at home, off by
        # default; ticking one puts it on the list like any product
        wanted_staples = store().plan_staples(plan_id)
        staples = [
            {"product": s, "on_list": s.lower() in wanted_staples} for s in store().staples()
        ]
        # thumbnail of the matched AH article, when one is known
        for item in items:
            mapping = mappings().get(item["product"])
            item["image"] = mapping.ah_image_url if mapping else None
            item["recipes"] = sources.get(item["product"].lower(), [])
            item["unknown_pack"] = (
                ah_pack_qty(
                    item["product"],
                    item["amount"],
                    item["unit"],
                    item["pack_count"],
                    facts,
                    unit_sizes,
                ).source
                == "unknown"
            )
        return render(
            request,
            "order.html",
            items=items,
            staples=staples,
            start=dutch_date(latest[1]),
            warnings=_plan_warnings(plan_id),
            leftovers=_plan_leftovers(plan_id),
            fmt=format_amount,
        )

    @app.post("/order/staples")
    async def order_staples(request: Request):
        """Tick staples onto the week's list, or off it again.

        Checked staples join the list like any product -- matched, pack-aware,
        riding the AH links -- and survive every rebuild via plan_staples. The
        list is rebuilt for the days it covers; when there is nothing left to
        rebuild from (everything already ordered), the rows are maintained in
        place instead.
        """
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        plan_id = latest[0]
        form = await request.form()
        wanted = {str(p).strip().lower() for p in form.getlist("product") if str(p).strip()}
        current = store().plan_staples(plan_id)
        for product in sorted(wanted - current):
            store().add_plan_staple(plan_id, product)
        for product in sorted(current - wanted):
            store().remove_plan_staple(plan_id, product)

        def _match_one(product: str) -> int | None:
            return _match([product]).get(product, (None, ""))[0]

        approved = store().plan_approvals(plan_id)
        pending = sorted(approved - store().plan_ordered(plan_id))
        rows = [r for r in store().plan_entries(plan_id) if r.day in pending]
        if rows and any(r.kind == "cook" for r in rows):
            # includes restock rows for ticked staples without demand
            _build_and_save_order(plan_id, rows)
        else:
            for product in sorted(wanted - current):
                store().add_order_item(plan_id, product, webshop_id=_match_one(product))
            for product in sorted(current - wanted):
                store().remove_order_item(plan_id, product)
        return RedirectResponse("/order", status_code=303)

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

    @app.post("/order/scan-add")
    def order_scan_add(ean: str = Form(...), back: str = Form("/plan")):
        """The scan button: a scanned package joins this week's shopping list.

        The barcode must be known — scanning an unknown package teaches it the
        product name first (the widget asks once). The item lands on the
        current plan's list as one piece, and rides the AH link like any
        other item.
        """
        if not back.startswith(("/plan", "/order")):
            back = "/plan"  # no open redirects
        separator = "&" if "?" in back else "?"

        def _scan_notice(message: str) -> RedirectResponse:
            return RedirectResponse(
                f"{back}{separator}error=scan:{quote(message)}", status_code=303
            )

        ean = ean.strip()
        if not valid_ean(ean):
            return _scan_notice("ongeldige EAN")
        proposal = store().load_proposal()
        if proposal is None:
            return _scan_notice("geen actieve week")
        title = barcodes().get(ean)
        if title is None:
            return _scan_notice(f"onbekende barcode {ean}")
        if not store().add_order_item(proposal[0], title):
            return _scan_notice(f"'{title}' staat al op de lijst")
        return RedirectResponse(f"{back}{separator}scanadded={quote(title)}", status_code=303)

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

    def _kitchen_product_sources() -> dict[str, list[dict]]:
        """Which recipes and dishes need each product, across the whole library.

        Keyed by the orderable product name (lowercase), like the week list's
        `_product_sources`. A recipe attributes its ingredients to itself; a
        component attributes to the dishes that name it as a part option.
        """
        sources: dict[str, dict[str, str]] = {}
        for recipe in load_all()[0]:
            for ingredient in recipe.ingredients:
                name, _note = order_product(ingredient.product)
                per = sources.setdefault(name.lower(), {})
                per.setdefault(recipe.name, f"/recipes/{recipe.slug}")
        components = components_by_slug()
        for dish in dishes_by_slug().values():
            for part in dish.parts:
                for option in part.options:
                    component = components.get(option.slug)
                    if component is not None:
                        per = sources.setdefault(component.name.lower(), {})
                        per.setdefault(dish.name, f"/dishes/{dish.slug}")
        return {
            product: [{"name": n, "href": h} for n, h in sorted(names.items())]
            for product, names in sources.items()
        }

    @app.get("/matches", response_class=HTMLResponse)
    def matches_page(request: Request):
        """All AH couplings, with the kitchen's uncoupled products on top.

        The queue at the top is every ingredient product the recipes and
        components use that has no mapping yet; each row opens the one-product
        coupling page. Below, every existing coupling with its article, AH's
        own pack size and the pack-fact state.
        """
        mapping_store = mappings()
        by_lower = {m.product.lower(): m for m in mapping_store.all()}
        facts = store().product_facts()
        used: set[str] = set()
        for recipe in load_all()[0]:
            for ingredient in recipe.ingredients:
                name, _note = order_product(ingredient.product)
                used.add(name.lower())
        for component in load_components_all()[0]:
            used.add(component.name.lower())
        unmatched = sorted(used - set(by_lower))
        sources = _kitchen_product_sources()
        matched_rows = []
        for m in sorted(mapping_store.all(), key=lambda m: m.product.lower()):
            suggestion = fact_from_unit_size(m.product, m.ah_unit_size) if m.ah_unit_size else None
            matched_rows.append(
                {
                    "product": m.product,
                    "title": m.ah_title,
                    "url": product_url(m.ah_webshop_id),
                    "image": m.ah_image_url,
                    "unit_size": m.ah_unit_size or "",
                    "confirmed": m.confirmed,
                    "has_fact": m.product.lower() in facts,
                    "pack_label": (
                        f"{suggestion.pack_qty:g} {suggestion.pack_unit}" if suggestion else ""
                    ),
                }
            )
        return render(
            request,
            "matches.html",
            unmatched=unmatched,
            matched=matched_rows,
            sources=sources,
        )

    @app.get("/order/match/one", response_class=HTMLResponse)
    def order_match_one(request: Request, product: str, unit: str = "", back: str = "/order"):
        """Couple one product to an AH article, from wherever it came.

        The week match page only lists the plan's saved items; this shows the
        same suggestion and manual-pin forms for a single product (a recipe
        order row), then returns to `back`.
        """
        if not back.startswith("/") or back.startswith("//"):
            back = "/order"  # no open redirects
        mapping = mappings().get(product)
        fact = store().product_facts().get(product.lower())
        try:
            suggestions = [
                {
                    "webshop_id": s.webshop_id,
                    "title": s.title,
                    "unit_size": s.unit_size,
                    "url": product_url(s.webshop_id),
                    "image": getattr(s, "image_url", None),
                }
                for s in _search(product)[:5]
            ]
            search_error = ""
        except Exception as exc:
            suggestions = []
            search_error = f"AH onbereikbaar ({exc})"
        if fact is not None and fact.unit_measure:
            # a pack of pieces with a known piece weight: editing it here
            # would drop the weight, so that happens on the settings page
            pack_label = (
                f"{fact.pack_qty:g} {fact.pack_unit}"
                f" ({fact.unit_qty:g} {fact.unit_measure} per stuk)"
            )
            pack_editable = False
            pack_prefill = ""
        elif fact is not None:
            pack_label = f"{fact.pack_qty:g} {fact.pack_unit}"
            pack_editable = True
            pack_prefill = pack_label
        else:
            pack_label = ""
            pack_editable = True
            pack_prefill = mapping.ah_unit_size if mapping and mapping.ah_unit_size else ""
        return render(
            request,
            "match_one.html",
            product=product,
            unit=unit,
            back=back,
            suggestions=suggestions,
            search_error=search_error,
            matched_url=product_url(mapping.ah_webshop_id) if mapping else None,
            matched_title=mapping.ah_title if mapping else "",
            matched_image=mapping.ah_image_url if mapping else None,
            pack_label=pack_label,
            pack_editable=pack_editable,
            pack_prefill=pack_prefill,
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
        unit: str = Form(""),
        webshop_id: str = Form(...),
        title: str = Form(""),
        image: str = Form(""),
        unit_size: str = Form(""),
        back: str = Form(""),
    ):
        """Pin an order item to an AH product.

        `webshop_id` accepts a bare id or a pasted ah.nl product URL; the id is
        taken from the URL and its slug fills in the title when none is given.
        AH's own pack size is kept alongside, to offer as a suggestion later.
        `back` returns to the page that asked (a single-product coupling from
        a recipe order row) instead of the match page.

        `unit` is the saved list line the coupling belongs to. It may be empty:
        the mapping is keyed by product alone, and a coupling made from a page
        that only knows the product (the /matches queue, a recipe order row)
        fixes every unit-line of that product on the list.
        """
        if not back.startswith("/") or back.startswith("//"):
            back = ""  # no open redirects
        ref = parse_product_ref(webshop_id)
        if ref is None:
            separator = "&" if "?" in back else "?"
            return RedirectResponse(f"{back or '/order/match'}{separator}error=id", status_code=303)
        article_id, slug_title = ref
        label = title.strip() or slug_title or f"webshop-id {article_id}"
        image_url = image.strip() or None
        unit_size_value = unit_size.strip() or None
        if image_url is None or unit_size_value is None or not title.strip():
            # A manual pin carries no article data: look the id up in AH's
            # search so the pin still gets its thumbnail, AH's own pack size
            # and the real title. Nothing is guessed — a miss keeps the pin
            # exactly as entered.
            try:
                candidates = _search(product, limit=10)
                if slug_title and not any(c.webshop_id == article_id for c in candidates):
                    candidates = _search(slug_title, limit=10)
            except Exception:
                candidates = []
            hit = next((c for c in candidates if c.webshop_id == article_id), None)
            if hit is not None:
                image_url = image_url or getattr(hit, "image_url", None)
                unit_size_value = unit_size_value or (hit.unit_size or None)
                if not title.strip() and hit.title:
                    label = hit.title
        mappings().put(
            AHMapping(
                product=product,
                ah_webshop_id=article_id,
                ah_title=label,
                score=1.0,
                confirmed=True,
                ah_image_url=image_url,
                ah_unit_size=unit_size_value,
            )
        )
        mappings().unreject(product)
        # AH's own pack size comes with the suggestion; adopt it as the
        # product's fact when there is none yet, so quantities are pack-aware
        # right away. An existing fact is never overwritten from here.
        if unit_size_value and product.lower() not in store().product_facts():
            fact = fact_from_unit_size(product, unit_size_value)
            if fact is not None:
                store().set_product_fact(fact)
        latest = store().latest_plan()
        if latest is not None:
            if unit.strip():
                store().set_order_item_webshop_id(latest[0], product, unit, article_id)
            else:
                store().set_order_item_webshop_id_all_units(latest[0], product, article_id)
        if back:
            return RedirectResponse(back, status_code=303)
        return RedirectResponse("/order/match", status_code=303)

    @app.post("/order/match/unpin")
    def order_match_unpin(product: str = Form(...), unit: str = Form(""), back: str = Form("")):
        if not back.startswith("/") or back.startswith("//"):
            back = ""  # no open redirects
        mappings().remove(product)
        # the human uncoupled this on purpose: never auto-match it again
        # until a new pin replaces the marker
        mappings().reject(product)
        latest = store().latest_plan()
        if latest is not None:
            if unit.strip():
                store().set_order_item_webshop_id(latest[0], product, unit, None)
            else:
                store().set_order_item_webshop_id_all_units(latest[0], product, None)
        if back:
            return RedirectResponse(back, status_code=303)
        return RedirectResponse("/order/match", status_code=303)

    @app.post("/order/match/pack")
    def order_match_pack(product: str = Form(...), unit_size: str = Form(""), back: str = Form("")):
        """Store a pack size for a matched product, read from AH's own size.

        Only the plain shapes are read; anything else is refused rather than
        guessed, and the user types it on the settings page instead. The shelf
        life is left at its default: a pack size is not a claim about freshness.
        `back` returns to the page that asked (an inline 'pakgrootte?' form on
        the recipe page or the order list) instead of the match page.
        """
        if not back.startswith("/") or back.startswith("//"):
            back = "/order/match"  # no open redirects
        mapping = mappings().get(product)
        text = unit_size.strip() or (mapping.ah_unit_size if mapping else "") or ""
        fact = fact_from_unit_size(product, text)
        if fact is None:
            separator = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{separator}error=pack", status_code=303)
        store().set_product_fact(fact)
        return RedirectResponse(back, status_code=303)

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
        """The AH deep-links for the saved list, one per chunk of 25 items.

        The link quantity counts AH packs, not recipe units, so it follows the
        same pack arithmetic as the list: `ah_pack_qty` adds one pack when
        nothing is known, more only when the demand really exceeds one pack.
        """
        facts = store().product_facts()
        unit_sizes = _unit_sizes()
        pairs = []
        for i in store().order_items(plan_id):
            if not i["webshop_id"] or i["have"]:
                continue
            pack = ah_pack_qty(
                i["product"], i["amount"], i["unit"], i["pack_count"], facts, unit_sizes
            )
            pairs.append((int(i["webshop_id"]), pack.qty))
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
        qr_slug: str | None = None,
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
            "qr_slug": qr_slug,
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
    def dish_edit_page(request: Request, slug: str, people: int | None = None):
        try:
            dish = load_dish(dish_file(dishes_dir(), slug))
        except DishError as exc:
            raise HTTPException(404, str(exc)) from exc
        ctx: dict = {}
        if people is not None:
            parts = _dish_order_parts(dish)
            if not parts:
                raise HTTPException(404, "Geen onderdeel van dit gerecht is bekend")
            with_products = {
                w.strip().lower() for w in request.query_params.getlist("with") if w.strip()
            }
            back = f"/dishes/{slug}?people={people or SERVINGS}"
            entry = PlanEntry(
                date=date.today(),
                recipe=None,
                availability=evenings_for(store(), date.today(), days=1)[0],
                score=0.0,
                reason="Handmatige bestelling voor gerecht",
                dish_slug=slug,
                parts=parts,
                people=people or SERVINGS,
            )
            ctx = _order_ctx(entry, people, back, with_products)
        return render(
            request,
            "dishes.html",
            **_dish_form_ctx(
                dish.name,
                _slots_from_parts(dish.parts, all_known()),
                form_title=f"{dish.name} wijzigen",
                form_action=f"/dishes/{slug}",
                qr_slug=slug,
            ),
            **ctx,
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

    def _unit_sizes() -> dict[str, str]:
        """AH's own reported pack size per product, for the pack fallback."""
        return {m.product.lower(): m.ah_unit_size for m in mappings().all() if m.ah_unit_size}

    def _order_ctx(
        entry: PlanEntry,
        people: int,
        back: str,
        wanted_staples: set[str] | None = None,
        have_products: set[str] | None = None,
        ingredient_rows: dict[str, str] | None = None,
    ) -> dict:
        """The inline 'bestellen' block: AH link + QR + items with pack counts.

        The link quantity counts AH packs, not recipe units: `ah_pack_qty`
        converts, and every line it could not convert is flagged so a human
        can supply the pack size right there.

        `wanted_staples` names staples that ARE ordered this time (the `with`
        query parameter): they flow through as normal items, marked
        '(voorraad)', and the staples this entry uses but that are not wanted
        get a '+' link to pull them in.

        `have_products` names items ticked 'in huis': they are dropped from
        the AH link and QR and shown as already owned. `ingredient_rows` maps
        the orderable product (lowercase) to the scaled recipe quantity, so
        the recipe page can show recipe and order side by side.
        """
        wanted_staples = wanted_staples or set()
        have_products = have_products or set()

        def _state_url(staples: set[str]) -> str:
            """The page URL with this exact with=/have= state, people kept."""
            return (
                back
                + "".join(f"&with={quote(p)}" for p in sorted(staples))
                + "".join(f"&have={quote(p)}" for p in sorted(have_products))
            )

        facts = store().product_facts()
        staples = [s for s in store().staples() if s.lower() not in wanted_staples]
        order = build_order(
            [entry],
            staples,
            servings=people or SERVINGS,
            facts=facts,
            carried=last_weeks_stock(
                store(),
                0,
                recipes_by_slug(),
                list(dishes_by_slug().values()),
                facts,
                SERVINGS,
            ),
        )
        matched = _match([i.product for i in order.shopping])
        items = []
        for item in order.shopping:
            key = item.product.lower()
            pack = ah_pack_qty(
                item.product, item.amount, item.unit, item.pack_count, facts, _unit_sizes()
            )
            is_staple = key in wanted_staples
            mapping = mappings().get(item.product)
            items.append(
                {
                    "product": item.product,
                    "product_lower": key,
                    "unit": item.unit,
                    "qty": pack.qty,
                    "source": pack.source,
                    "detail": pack.detail,
                    "webshop_id": matched.get(item.product, (None, ""))[0],
                    "match_url": product_url(mapping.ah_webshop_id) if mapping else None,
                    "match_title": mapping.ah_title if mapping else "",
                    "match_image": mapping.ah_image_url if mapping else None,
                    "need": format_amount(item.amount, item.unit),
                    "note": item.note,
                    "staple": is_staple,
                    "have": key in have_products,
                    "recipe_qty": (
                        (ingredient_rows or {}).get(f"{key}|{item.unit}")
                        or (ingredient_rows or {}).get(key)
                        or format_amount(item.amount, item.unit)
                    ),
                    "remove_url": _state_url(wanted_staples - {key}) if is_staple else None,
                }
            )
        # in-house items are owned, so they are not bought: no pair, no QR cell
        pairs = [(i["webshop_id"], i["qty"]) for i in items if i["webshop_id"] and not i["have"]]
        link = build_add_multiple_url(pairs[:25]) if pairs else ""
        ctx = {
            "order_link": link,
            "order_qr": generate_qr_svg(link) if link else "",
            "order_people": people or SERVINGS,
            # the full current state (people + with + have), so a form that
            # leaves the page comes back to exactly this view
            "order_back": _state_url(wanted_staples),
            "order_with": sorted(wanted_staples),
            "order_have": sorted(have_products),
            "order_items": items,
            # the staples this entry uses but that are not being ordered
            "order_staples": [
                {
                    "product": s.product,
                    "need": format_amount(s.amount, s.unit),
                    "add_url": _state_url(wanted_staples | {s.product.lower()}),
                }
                for s in order.staples
            ],
        }
        if ingredient_rows is not None:
            # the recipe page shows recipe quantity and order quantity side by
            # side, with the in-house checkbox between them
            ctx["order_rows"] = items
        return ctx

    def _recipe_order_entry(recipe: Recipe, people: int) -> PlanEntry:
        """One cook entry for this recipe alone, for the order block."""
        by_slug = all_known()
        dishes_map = dishes_by_slug()
        evening = evenings_for(store(), date.today(), days=1)[0]
        slug = recipe.slug
        return PlanEntry(
            date=date.today(),
            recipe=by_slug.get(slug),
            dish_slug=slug if slug in dishes_map else None,
            availability=evening,
            people=people or SERVINGS,
            kind="cook",
            source_slug=slug if slug not in by_slug else None,
            score=0.0,
            reason="Handmatige bestelling voor recept",
        )

    def _dish_order_parts(dish: ComposedDish) -> list[Recipe]:
        """The recipes filling a dish for a standalone order.

        There is no per-part pick here, so each part takes its first option
        that resolves to a recipe or component.
        """
        known = all_known()
        parts = []
        for part in dish.parts:
            for option in part.options:
                if option.slug in known:
                    parts.append(known[option.slug])
                    break
        return parts

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

    @app.get("/barcode/{ean}")
    def barcode_lookup(ean: str):
        """The product name a barcode is remembered for (the scanner widget)."""
        if not valid_ean(ean):
            raise HTTPException(400, "Ongeldige EAN")
        title = barcodes().get(ean)
        if title is None:
            raise HTTPException(404, "Onbekende barcode")
        return {"ean": ean, "title": title}

    @app.post("/barcode/{ean}")
    def barcode_remember(ean: str, title: str = Form(...)):
        """Remember a scanned barcode's product name (the scanner asks once)."""
        if not valid_ean(ean):
            raise HTTPException(400, "Ongeldige EAN")
        if not title.strip():
            raise HTTPException(400, "Geen productnaam")
        barcodes().put(ean, title)
        return {"ean": ean, "title": title.strip()}

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request, q: str = "", tag: str = ""):
        all_recipes, errors = load_all()
        people = store().people()
        verdicts = store().verdicts()
        history = store().history()
        today = date.today()
        rows: list[RecipeRow] = []
        for r in sorted(all_recipes, key=lambda r: r.name.lower()):
            pop = popularity(verdicts.get(r.slug, {}))
            per_person = {p["id"]: verdicts.get(r.slug, {}).get(p["id"], "") for p in people}
            rows.append(
                {
                    "slug": r.slug,
                    "name": r.name,
                    "minutes": r.time.total_minutes,
                    "protein": r.protein,
                    "tags": r.tags,
                    "image": image_path(r.slug),
                    "verdicts": per_person,
                    "badge": pop,
                    "favourite": pop["favourite"],
                    "recency": recency(history.get(r.slug), today),
                }
            )
        query = q.strip().lower()
        if query:
            rows = [row for row in rows if query in row["name"].lower()]
        wanted = tag.strip().lower()
        if wanted:
            rows = [row for row in rows if wanted in row["tags"]]
        all_tags = sorted({t for r in all_recipes for t in r.tags})
        return render(
            request,
            "recipes.html",
            recipes=rows,
            people=people,
            errors=errors,
            q=q,
            tag=wanted,
            all_tags=all_tags,
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

    def import_token() -> str:
        """Stable secret baked into the served bookmarklet: the POST endpoint
        has no auth, so a token keeps random sites from creating recipes."""
        path = data / "import_token"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        token = uuid4().hex
        path.write_text(token, encoding="utf-8")
        return token

    @app.get("/recipes/import", response_class=HTMLResponse)
    def import_page(request: Request):
        base = str(request.base_url).rstrip("/")
        post_bookmarklet = RECIPE_POST_BOOKMARKLET.replace("@BASE@", base).replace(
            "@TOKEN@", import_token()
        )
        return render(
            request,
            "import.html",
            bookmarklet=RECIPE_BOOKMARKLET,
            post_bookmarklet=post_bookmarklet,
        )

    @app.post("/recipes/import")
    async def do_import(
        request: Request,
        photos: Annotated[list[UploadFile] | None, File()] = None,
        text: str = Form(""),
        url: str = Form(""),
        page_html: str = Form(""),
    ):
        """Read one recipe from photos (all pages of it), pasted text, page source or a URL."""
        uploads = [p for p in (photos or []) if p.filename]
        source_url = ""
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
                source_url = url.strip()
                if page_html.strip():
                    # pasted page source / JSON-LD: parse offline, nothing is
                    # fetched; a leading URL line (bookmarklet output) is the source
                    pasted = page_html.strip()
                    if not source_url and "\n" in pasted:
                        first_line, _, rest = pasted.partition("\n")
                        if re.match(r"^(?:https?://|www\.)\S+$", first_line.strip()):
                            source_url, pasted = first_line.strip(), rest.strip()
                    _recipe, parsed = extract_from_url(
                        source_url or PASTE_DEFAULT_URL,
                        html=normalize_pasted_page(pasted),
                    )
                elif source_url:
                    _recipe, parsed = extract_from_url(source_url)
                else:
                    raw = text
        except Exception as exc:
            return render(request, "import.html", error=f"Importeren mislukt: {exc}")
        stage_uid = ""
        staged_url = ""
        if not used_photo and (url.strip() or page_html.strip()):
            # URL/paste import: use the structured result directly, no flatten/reparse
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
        ctx = _editor_ctx_from_parse(parsed, url_value=url.strip() or source_url)
        ctx["staged_photo"] = staged_url
        ctx["stage_uid"] = stage_uid
        return render(
            request,
            "recipe_edit.html",
            **ctx,
            original_slug=None,
        )

    @app.post("/recipes/import-paste")
    def import_paste(
        request: Request,
        token: str = Form(""),
        url: str = Form(""),
        paste: str = Form(""),
    ):
        """The bookmarklet's target: recipe JSON-LD or page source from an
        ah.nl tab, posted straight into the review editor.

        Reached by a form navigation, so no CORS is involved; the token keeps
        random sites from creating recipes. Same offline parse as the
        pasted-page path of /recipes/import.
        """
        if not token or token != import_token():
            raise HTTPException(403, "Ongeldige token")
        source_url = url.strip()
        try:
            _recipe, parsed = extract_from_url(
                source_url or PASTE_DEFAULT_URL,
                html=normalize_pasted_page(paste),
            )
        except Exception as exc:
            return HTMLResponse(
                "<html><body><h1>Importeren mislukt</h1>"
                f"<p>{exc}</p>"
                "<p>Gebruik in plaats daarvan het kopieer-bookmarkletje en plak"
                ' de JSON in <a href="/recipes/import-urls">de bulkimport</a>'
                ' of op <a href="/recipes/import">de importpagina</a>.</p>'
                "</body></html>",
                status_code=400,
            )
        ctx = _editor_ctx_from_parse(parsed, url_value=source_url)
        stage_uid = ""
        if getattr(parsed, "image_url", None):
            try:
                stage_uid = _stage_web_image(getattr(parsed, "image_url", ""))
            except Exception:
                stage_uid = ""
        ctx["staged_photo"] = ocr_staged_url(stage_uid) if stage_uid else ""
        ctx["stage_uid"] = stage_uid
        return render(request, "recipe_edit.html", **ctx, original_slug=None)

    @app.get("/recipes/import-urls", response_class=HTMLResponse)
    def import_urls_page(request: Request):
        return render(request, "import_urls.html")

    @app.post("/recipes/import-urls")
    async def import_urls(request: Request, urls: str = Form("")):
        """Bulk import: save every URL on its own line directly as a recipe.

        Pastes of page source or JSON-LD also work -- for ah.nl, whose bot
        protection only lets a real browser through: the bookmarklet copies
        the recipe JSON, one paste per recipe, optionally preceded by the
        recipe's URL. A blank line or the next URL ends a paste. Runs
        sequentially (gentle on the sites, and the browser fallback is not
        parallel-safe); fetched AH recipes may take ~20 s each.
        """

        def is_url(line: str) -> bool:
            return bool(re.match(r"^(?:https?://|www\.)\S+$", line))

        lines: list[str] = []
        for raw in urls.splitlines():
            ln = raw.strip()
            if not ln:
                lines.append("")  # blank line: paste separator
                continue
            if ln.startswith("#"):
                continue  # headers/comments in a pasted list
            ln = ln.lstrip("-* \t").strip()  # markdown list markers
            if ln:
                lines.append(ln)

        items: list[tuple[str, str | None]] = []  # (url, pasted html or None)
        i = 0
        while i < len(lines):
            ln = lines[i]
            if not ln:
                i += 1
                continue
            if is_url(ln):
                if i + 1 < len(lines) and lines[i + 1] and not is_url(lines[i + 1]):
                    # the URL heads its own pasted page source/JSON-LD
                    block = []
                    j = i + 1
                    while j < len(lines) and lines[j] and not is_url(lines[j]):
                        block.append(lines[j])
                        j += 1
                    items.append((ln, normalize_pasted_page("\n".join(block))))
                    i = j
                else:
                    items.append((ln, None))
                    i += 1
            else:  # a pasted block: until the next blank line or URL
                block = []
                while i < len(lines) and lines[i] and not is_url(lines[i]):
                    block.append(lines[i])
                    i += 1
                items.append(("", normalize_pasted_page("\n".join(block))))

        results: list[dict] = []
        for source, html_block in items[:50]:  # hard cap: one request must stay finite
            label = source or "(geplakte bron)"
            if "://" not in source and source:
                source = f"https://{source}"
            try:
                if html_block:
                    _recipe, parsed = extract_from_url(source or PASTE_DEFAULT_URL, html=html_block)
                else:
                    _recipe, parsed = extract_from_url(source)
                # extract seeds slug with the raw name (the editor re-slugs on
                # save); bulk saves directly, so slug properly here
                recipe = _recipe.model_copy(update={"slug": slugify(_recipe.name)})
                path = data / "recipes" / f"{recipe.slug}.yaml"
                outcome = "opgeslagen"
                if path.exists():
                    existing = load_recipe(path)
                    if existing.url == recipe.url:
                        results.append(
                            {"url": label, "ok": True, "skipped": True, "name": existing.name}
                        )
                        continue
                    # different recipe with the same name: keep both
                    path = unique_recipe_path(recipe, data / "recipes")
                    outcome = "opgeslagen (andere naam: dubbel)"
                save_recipe(path, recipe)
                stage_uid = ""
                if getattr(parsed, "image_url", None):
                    try:
                        stage_uid = _stage_web_image(getattr(parsed, "image_url", "")) or ""
                    except Exception:
                        stage_uid = ""
                if stage_uid:
                    _retire_ocr_card(stage_uid, recipe.slug, keep=True)
                results.append(
                    {
                        "url": label,
                        "ok": True,
                        "name": recipe.name,
                        "slug": recipe.slug,
                        "outcome": outcome,
                        "image": bool(stage_uid),
                    }
                )
            except Exception as exc:
                results.append({"url": label, "ok": False, "error": str(exc)[:200]})
        ok = sum(1 for r in results if r["ok"] and not r.get("skipped"))
        skipped = sum(1 for r in results if r.get("skipped"))
        return render(
            request,
            "import_urls.html",
            results=results,
            ok=ok,
            skipped=skipped,
            failed=len(results) - ok - skipped,
            urls_value=urls,
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
            "tags_value": "",
            "course_value": "hoofdgerecht",
            "courses": list(COURSES),
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
                tags_value=", ".join(recipe.tags),
                course_value=recipe.course,
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
    def recipe_view_page(request: Request, slug: str, people: int | None = None):
        try:
            recipe = load_recipe(data / "recipes" / f"{slug}.yaml")
        except RecipeError as exc:
            raise HTTPException(404, str(exc)) from exc
        image = image_path(slug)
        # the combo is always on: recipe and shopping list side by side,
        # scaled to the shown people count (the recipe's own servings until
        # the visitor changes it)
        shown = people or recipe.servings
        factor = (shown / recipe.servings) if recipe.servings else 1.0
        ingredients = [
            format_amount(i.amount * factor, i.unit) + " " + i.product for i in recipe.ingredients
        ]
        # orderable product + unit (lowercase) -> scaled quantity, for the
        # side-by-side table: duplicate ingredient lines of one product join
        # into one row per unit
        ingredient_rows: dict[str, str] = {}
        for i in recipe.ingredients:
            key = f"{order_product(i.product)[0].lower()}|{i.unit}"
            qty = format_amount(i.amount * factor, i.unit)
            ingredient_rows[key] = (
                f"{ingredient_rows[key]} + {qty}" if key in ingredient_rows else qty
            )
        with_products = {
            w.strip().lower() for w in request.query_params.getlist("with") if w.strip()
        }
        have_products = {
            w.strip().lower() for w in request.query_params.getlist("have") if w.strip()
        }
        ctx = _order_ctx(
            _recipe_order_entry(recipe, shown),
            shown,
            f"/recipes/{slug}?people={shown}",
            wanted_staples=with_products,
            have_products=have_products,
            ingredient_rows=ingredient_rows,
        )
        return render(
            request,
            "recipe_view.html",
            recipe=recipe,
            image_url=f"/recipes/img/{image.name}" if image else None,
            ingredients=ingredients,
            people_shown=shown,
            **ctx,
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
        tags: str = Form(""),
        course: str = Form("hoofdgerecht"),
        ingredients: str = Form(""),
        steps: str = Form(""),
        image: Annotated[list[UploadFile] | None, File()] = None,
        ocr_stage: str = Form(""),
        card_photo: str = Form(""),
    ):
        """Save the editor form; `ocr_stage` retires the reviewed card photo."""
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
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
                    tags_value=tags,
                    course_value=course,
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
            tags=tags_list,
            course=course,
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
    def set_verdict_route(
        slug: str = Form(...),
        person: str = Form(...),
        verdict: str = Form(""),
        back: str = Form(""),
    ):
        _set_verdict(slug, person, verdict)
        if not back.startswith("/") or back.startswith("//"):
            back = ""  # no open redirects
        return RedirectResponse(back or "/recipes", status_code=303)

    @app.get("/recipes/{slug}/export")
    def recipe_export(slug: str):
        """Download the recipe as a yaml file (1-op-1 de bron op schijf)."""
        path = data / "recipes" / f"{slug}.yaml"
        if not path.is_file():
            raise HTTPException(404, "Onbekend recept")
        return FileResponse(path, media_type="application/x-yaml", filename=f"{slug}.yaml")

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
        slug: str = Form(...),
        person: str = Form(...),
        verdict: str = Form(""),
        back: str = Form(""),
    ):
        _set_verdict(slug, person, verdict)
        if not back.startswith("/") or back.startswith("//"):
            back = ""  # no open redirects
        return RedirectResponse(back or "/components", status_code=303)

    # --- vriezer ----------------------------------------------------------------

    @app.get("/vriezer", response_class=HTMLResponse)
    def vriezer_page(request: Request):
        """Wat er in de vriezer ligt: het langst ingevroren eerst, met de
        leeftijd erbij; +/− en weggooien voor handmatig beheer. Daarboven de
        geplande bewegingen die nog op bevestiging wachten."""
        known = all_known()
        dishes = dishes_by_slug()
        items = []
        for e in vriezer().entries():
            slug = e["slug"]
            name = (
                known[slug].name
                if slug in known
                else (dishes[slug].name if slug in dishes else slug)
            )
            since = date.fromisoformat(e["since"]) if e["since"] else None
            age = (date.today() - since).days if since else None
            items.append({**e, "name": name, "age": age, "image": image_url_for(slug)})

        def name_of(slug: str) -> str:
            return (
                known[slug].name
                if slug in known
                else (dishes[slug].name if slug in dishes else slug)
            )

        pending_in = [
            {**p, "name": name_of(p["slug"])} for p in vriezer().pending() if p["portions"] > 0
        ]
        pending_out = [
            {**p, "name": name_of(p["slug"])} for p in vriezer().pending() if p["portions"] < 0
        ]
        choices = sorted({**known, **dishes}.values(), key=lambda r: r.name.lower())
        return render(
            request,
            "vriezer.html",
            items=items,
            pending_in=pending_in,
            pending_out=pending_out,
            choices=choices,
            total=sum(i["portions"] for i in items),
        )

    @app.post("/vriezer/confirm")
    def vriezer_confirm(pending_id: int = Form(...)):
        """De mens bevestigt: de geplande beweging wijzigt de echte voorraad."""
        vriezer().confirm(pending_id)
        return RedirectResponse("/vriezer", status_code=303)

    @app.post("/vriezer/discard")
    def vriezer_discard(pending_id: int = Form(...)):
        """De geplande beweging is niet gebeurd: alleen de planning vervalt."""
        vriezer().discard(pending_id)
        return RedirectResponse("/vriezer", status_code=303)

    @app.post("/vriezer/add")
    def vriezer_add(slug: str = Form(...), portions: int = Form(1)):
        if portions <= 0:
            raise HTTPException(400, "Porties moet minstens 1 zijn")
        vriezer().add(slug.strip(), portions, since=date.today())
        return RedirectResponse("/vriezer", status_code=303)

    @app.post("/vriezer/take")
    def vriezer_take(slug: str = Form(...), portions: int = Form(1)):
        vriezer().take(slug.strip(), max(portions, 0))
        return RedirectResponse("/vriezer", status_code=303)

    @app.post("/vriezer/remove")
    def vriezer_remove(slug: str = Form(...)):
        vriezer().remove(slug.strip())
        return RedirectResponse("/vriezer", status_code=303)

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

    @app.get("/settings/export")
    def settings_export():
        """Download alle data als zip-backup: de receptbibliotheek (yaml +
        afbeeldingen), de plandatabase (mensen, oordelen, instellingen,
        weken, bestellingen) en de losse winkels (vriezer, bonus,
        koppelingen, barcodes)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sub in ("recipes", "components", "dishes"):
                d = data / sub
                if d.is_dir():
                    for f in sorted(d.rglob("*")):
                        if f.is_file():
                            zf.write(f, arcname=f"{sub}/{f.relative_to(d)}")
            store().conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # vriezer/bonus/koppelingen/barcodes zitten in state.db: de zip
            # hoeft alleen de bibliotheek (yaml + afbeeldingen) erbij
            db_path = data / "state.db"
            if db_path.is_file():
                zf.write(db_path, arcname="state.db")
        stamp = date.today().isoformat()
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="assistent-backup-{stamp}.zip"'},
        )

    @app.post("/settings/import")
    async def settings_import(backup: Annotated[UploadFile, File()]):
        """Zet een zip-backup terug: yaml-bestanden + afbeeldingen worden
        weggeschreven, de plandatabase wordt vervangen en alle caches worden
        opnieuw geopend. Een restorerende import vervangt alles wat er nu is."""
        payload = await backup.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile:
            raise HTTPException(400, "Geen geldige zip") from None
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if "state.db" not in names:
            raise HTTPException(400, "De backup mist state.db")
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                raise HTTPException(400, f"Onveilig pad in backup: {n}")
        # de bibliotheken eerst leegmaken: de backup is leidend, losse
        # recepten die er niet in zitten verdwijnen mee
        for sub in ("recipes", "components", "dishes"):
            d = data / sub
            if d.is_dir():
                for f in sorted(d.rglob("*")):
                    if f.is_file() and f"{sub}/{f.relative_to(d)}" not in names:
                        f.unlink()
        for n in names:
            if n == "state.db":
                continue
            target = data / n
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(n))
        store().conn.close()
        db_path = data / "state.db"
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        db_path.write_bytes(zf.read("state.db"))
        _reset_data_caches()
        # een OUDE backup bevat nog yaml-winkels (vriezer/bonus/...): die
        # worden vervangend in hun tabellen geïmporteerd; nieuwe backups
        # dragen die data al in state.db en worden met rust gelaten
        if any(
            n in names for n in ("vriezer.yaml", "bonus.yaml", "ah_mappings.yaml", "barcodes.yaml")
        ):
            store().import_yaml_side_stores(replace=True)
        return RedirectResponse("/settings?imported=1", status_code=303)

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

    @app.post("/settings/people/move")
    def move_person(person_id: str = Form(...), direction: str = Form("up")):
        """Move a person one spot up/down in the column order used by the
        week view and the recipe list."""
        store().move_person(person_id.strip(), -1 if direction == "up" else 1)
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
