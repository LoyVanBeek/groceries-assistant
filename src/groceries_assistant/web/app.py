"""FastAPI web app: plan -> review -> AH deep-link, recipe ingestion, settings."""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from groceries_assistant.ah.client import AHClient
from groceries_assistant.ah.deeplink import build_add_multiple_url, chunk_items, generate_qr_svg
from groceries_assistant.ah.matching import AHMappingStore
from groceries_assistant.models import Recipe, slugify
from groceries_assistant.recipes.extract import extract_from_images
from groceries_assistant.recipes.loader import (
    RecipeError,
    load_recipe,
    load_recipes_with_errors,
    save_recipe,
)
from groceries_assistant.recipes.textparser import (
    LIST_MARKER,
    parse_ingredient_line,
    parse_recipe_text,
)
from groceries_assistant.recipes.urlimport import extract_from_url
from groceries_assistant.services.planner_service import (
    apply_absences,
    build_order,
    evenings_for,
    match_products,
    parse_start,
    plan_to_rows,
    preferences_from_store,
    propose,
    rows_to_entries,
)
from groceries_assistant.store.db import PlanEntryRow, Store
from groceries_assistant.web.views import WEEKDAYS, format_amount, plan_view

SERVINGS = 4
LEFTOVERS_CHOICE = "__leftovers__"
COOK_CHOICE = "__cook__"
KEEP_CHOICE = ""


def create_app(
    data_dir: str | os.PathLike[str],
    search_fn=None,
    tesseract_binary: str = "tesseract",
) -> FastAPI:
    """search_fn: injectable AH search(product) -> candidates; None = real client."""
    data = Path(data_dir)
    (data / "recipes").mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Groceries Assistant")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    holder: list[Store] = []

    def store() -> Store:
        if not holder:
            holder.append(Store(data / "state.db"))
        return holder[0]

    def mappings() -> AHMappingStore:
        return AHMappingStore(data / "ah_mappings.yaml")

    def load_all() -> tuple[list[Recipe], list[str]]:
        return load_recipes_with_errors(data / "recipes")

    def recipes_by_slug() -> dict[str, Recipe]:
        all_recipes, _errors = load_all()
        return {r.slug: r for r in all_recipes}

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
        target = to if to in ("light", "dark") else (
            "light" if request.cookies.get("theme") == "dark" else "dark"
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
            plan = propose(all_recipes, store(), start)
            plan_id = store().save_proposal(start, plan_to_rows(plan))
            proposal = (plan_id, start)
        plan_id, start = proposal[0], proposal[1]
        prefs = preferences_from_store(store())
        evenings = plan_view(store(), plan_id, all_recipes, prefs, store().people())
        return render(
            request, "plan.html", plan_id=plan_id, start=start.isoformat(),
            evenings=evenings, errors=errors,
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
            else:
                new_rows.append(PlanEntryRow(day=target, kind="cook", recipe_slug=choice, people=4))
        store().update_proposal_rows(plan_id, new_rows)
        return RedirectResponse("/plan", status_code=303)

    def _best_cook_row(rows: list[PlanEntryRow], target: date) -> PlanEntryRow:
        """Convert a leftovers evening into a cook evening with the best fit."""
        from groceries_assistant.services.planning import alternatives

        evening = evenings_for(store(), target, days=1)[0]
        prefs = preferences_from_store(store())
        all_recipes, _ = load_all()
        source = _previous_cook_slug(rows, target)
        scored = alternatives(
            all_recipes, prefs, evening, exclude_slug=source
        )
        if scored:
            recipe = scored[0][1]
        elif all_recipes:
            recipe = min(all_recipes, key=lambda r: (r.time.same_day_minutes, r.slug))
        else:
            raise HTTPException(400, "Geen recepten om te koken")
        return PlanEntryRow(day=target, kind="cook", recipe_slug=recipe.slug, people=4)

    @app.post("/plan/evening")
    async def plan_evening(request: Request, day: str = Form(...)):
        """Per-evening adjustments: who cooks, who joins.

        The cook must be an adult who joins that evening; violations reject
        the whole save so no partial state (override/absences) is written.
        """
        form = await request.form()
        cook = str(form.get("cook", "")).strip()
        joined = {str(v) for v in form.getlist("joined")}
        target = date.fromisoformat(day)
        if cook:
            people_by_id = {p["id"]: p for p in store().people()}
            person = people_by_id.get(cook)
            if person is None or person["role"] != "adult" or cook not in joined:
                return RedirectResponse("/plan?error=kok", status_code=303)
        store().set_week_override(
            target,
            cook=cook,
            free_minutes=evening_minutes(store(), target),
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
        by_slug = recipes_by_slug()
        entries = rows_to_entries(rows, by_slug)
        if not any(e.kind == "cook" for e in entries):
            raise HTTPException(400, "Minstens één kookavond nodig")
        order = build_order(entries, by_slug, store().staples(), servings=SERVINGS)
        matched = _match([i.product for i in order.shopping])
        store().save_order_items(
            plan_id,
            [
                (item.product, item.unit, item.amount, matched.get(item.product, (None, ""))[0])
                for item in order.shopping
            ],
        )
        store().approve_proposal(plan_id)
        return RedirectResponse("/order", status_code=303)

    def _match(product_names: list[str]) -> dict[str, tuple[int, str]]:
        if search_fn is None:
            return {}
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
            fmt=format_amount,
        )

    @app.post("/order/have")
    def order_have(product: str = Form(...), unit: str = Form(...), have: str = Form("")):
        latest = store().latest_plan()
        if latest is None:
            raise HTTPException(404, "Geen actieve week")
        store().set_order_item_have(latest[0], product, unit, bool(have))
        return RedirectResponse("/order", status_code=303)

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

    # --- recipes ---------------------------------------------------------------

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
                "verdicts": {p["id"]: verdicts.get(r.slug, {}).get(p["id"], "") for p in people},
            }
            for r in sorted(all_recipes, key=lambda r: r.name.lower())
        ]
        return render(request, "recipes.html", recipes=rows, people=people, errors=errors)

    @app.get("/recipes/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html")

    @app.post("/recipes/import")
    async def do_import(
        request: Request,
        photo: Annotated[UploadFile | None, File()] = None,
        text: str = Form(""),
        url: str = Form(""),
    ):
        try:
            if photo is not None and photo.filename:
                with tempfile.NamedTemporaryFile(
                    suffix=Path(photo.filename).suffix or ".jpg", delete=False
                ) as tmp:
                    tmp.write(await photo.read())
                    tmp_path = Path(tmp.name)
                try:
                    parsed, _raw = extract_from_images(
                        [tmp_path], binary=tesseract_binary, lang="nld"
                    )
                    used_photo = True
                finally:
                    tmp_path.unlink(missing_ok=True)
            else:
                used_photo = False
            if not used_photo:
                if url.strip():
                    _recipe, parsed = extract_from_url(url.strip())
                    raw = "\n".join([parsed.name or "", *parsed.ingredients, *parsed.steps])
                else:
                    raw = text
        except Exception as exc:
            return render(request, "import.html", error=f"Importeren mislukt: {exc}")
        if not used_photo:
            parsed = parse_recipe_text(raw)
        return render(
            request,
            "recipe_edit.html",
            **editor_ctx(
                parsed=parsed,
                name_value=parsed.name or "",
                servings_value=parsed.servings or 4,
                prep_value=parsed.prep_minutes or 0,
                cook_value=parsed.cook_minutes or 0,
                ingredient_lines="\n".join(
                    f"{i.amount:g} {i.unit} {i.product}" for i in parsed.ingredients
                ),
                steps_text="\n".join(number_steps(parsed.steps)),
                protein_value="",
            ),
            original_slug=None,
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
            "ingredient_lines": "",
            "steps_text": "",
            "parsed": None,
        }
        return {**defaults, **fields}

    @app.get("/recipes/{slug}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, slug: str):
        try:
            recipe = load_recipe(data / "recipes" / f"{slug}.yaml")
        except RecipeError as exc:
            raise HTTPException(404, str(exc)) from exc
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
                ingredient_lines="\n".join(
                    f"{i.amount:g} {i.unit} {i.product}" for i in recipe.ingredients
                ),
                steps_text="\n".join(number_steps(recipe.steps)),
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
        return render(
            request,
            "recipe_view.html",
            recipe=recipe,
            ingredients=[
                format_amount(i.amount, i.unit) + " " + i.product for i in recipe.ingredients
            ],
        )

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
        ingredients: str = Form(""),
        steps: str = Form(""),
    ):
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
                    ingredient_lines=ingredients,
                    steps_text=steps,
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
            steps=number_steps(steps.splitlines()),
            ingredients=parsed_ingredients,
        )
        old_path = data / "recipes" / f"{original_slug}.yaml" if original_slug else None
        new_path = data / "recipes" / f"{recipe.slug}.yaml"
        if old_path != new_path and new_path.exists():
            raise HTTPException(400, f"Recept '{recipe.slug}' bestaat al")
        save_recipe(new_path, recipe)
        if old_path is not None and old_path != new_path:
            old_path.unlink(missing_ok=True)
            store().rename_recipe(original_slug, recipe.slug)
        return RedirectResponse("/recipes", status_code=303)

    def parse_ingredient_lines(lines: list[str]) -> list[dict]:
        out: list[dict] = []
        for line in lines:
            parsed = parse_ingredient_line(line.strip())
            if parsed is not None:
                out.append(
                    {"product": parsed.product, "amount": parsed.amount, "unit": parsed.unit}
                )
        return out

    @app.post("/recipes/verdict")
    def set_verdict_route(slug: str = Form(...), person: str = Form(...), verdict: str = Form("")):
        if verdict in ("love", "ok", "dislike", "never"):
            store().set_verdict(slug, person, verdict)
        else:
            store().clear_verdict(slug, person)
        return RedirectResponse("/recipes", status_code=303)

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

    return app
