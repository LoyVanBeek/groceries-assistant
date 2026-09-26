"""Import recipes from recipe websites via recipe-scrapers.

We fetch the HTML ourselves (httpx, browser UA, timeout control) and hand it
to recipe-scrapers' offline parser, then run every ingredient string through
the Dutch line parser to split amount/unit/product.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from recipe_scrapers import scrape_html

from groceries_assistant.models import Ingredient, Recipe
from groceries_assistant.recipes.extract import ExtractionError
from groceries_assistant.recipes.textparser import (
    UNIT_MAP,
    UNIT_WORDS,
    ParseResult,
    parse_ingredient_line,
)

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


@dataclass
class UrlParseResult(ParseResult):
    source_url: str = ""
    image_url: str | None = None


def fetch_html(url: str, timeout: float = 20.0) -> str:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExtractionError(f"Kon de pagina niet laden: {exc}") from exc
    return response.text


def extract_from_url(url: str, html: str | None = None) -> tuple[Recipe, UrlParseResult]:
    """Scrape a recipe URL into a Recipe plus the parse details."""
    if html is None:
        html = fetch_html(url)
    try:
        # wild_mode: fall back to generic schema.org parsing for domains that
        # have no dedicated scraper
        scraper = scrape_html(html, org_url=url, wild_mode=True)
    except Exception as exc:
        raise ExtractionError(
            f"Geen recept gevonden op {url}. Kopieer de tekst handmatig en gebruik de tekst-import."
        ) from exc

    result = UrlParseResult(source_url=url)

    try:
        result.name = str(scraper.title() or "").strip() or None
    except Exception:
        result.name = None

    try:
        result.ingredients = _parse_ingredient_strings(scraper.ingredients())
    except Exception:
        result.ingredients = []
        result.warnings.append("Geen ingrediënten gevonden op de pagina")

    try:
        instructions = scraper.instructions()
    except Exception:
        instructions = ""
    if instructions:
        result.steps = [
            line.strip() for line in instructions.replace("\r", "").split("\n") if line.strip()
        ]

    try:
        yields = scraper.yields()
    except Exception:
        yields = ""
    result.servings = _parse_yields(yields) or parse_servings_fallback(html)

    try:
        total = scraper.total_time()
    except Exception:
        total = None
    try:
        prep = scraper.prep_time()
    except Exception:
        prep = None
    try:
        cook = scraper.cook_time()
    except Exception:
        cook = None
    result.prep_minutes = _minutes(prep)
    result.cook_minutes = _minutes(cook) or _minutes(total)

    try:
        result.image_url = scraper.image()
    except Exception:
        result.image_url = None

    if not result.servings:
        result.servings = 4
        result.warnings.append("Geen aantal personen gevonden (standaard 4)")

    recipe = Recipe.model_validate(
        {
            "name": result.name or "Naamloos recept",
            "slug": result.name or "naamloos-recept",
            "servings": result.servings,
            "time": {"prep": result.prep_minutes or 0, "cook": result.cook_minutes or 0},
            "steps": result.steps,
            "ingredients": [
                {"product": i.product, "amount": i.amount, "unit": i.unit}
                for i in result.ingredients
            ],
        }
    )
    return recipe, result


def _parse_ingredient_strings(strings: list[str]):
    ingredients = []
    for raw in strings:
        line = str(raw).strip()
        if not line:
            continue
        ingredient = parse_ingredient_line(line)
        if ingredient is not None:
            ingredients.append(ingredient)
        else:
            # structured sites often use "product (amount unit)" or bare names
            lowered = line.lower()
            matched = next(
                (
                    token
                    for token in lowered.replace("(", " ").replace(")", " ").split()
                    if token in UNIT_WORDS
                ),
                None,
            )
            if matched:
                ingredients.append(Ingredient(product=line, amount=1, unit=UNIT_MAP[matched]))
            else:
                ingredients.append(Ingredient(product=line, amount=1, unit="stuk"))
    return ingredients


def _parse_yields(yields: str) -> int | None:
    import re

    if not yields:
        return None
    match = re.search(r"(\d{1,3})", str(yields))
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 < value <= 24 else None


def parse_servings_fallback(html: str) -> int | None:
    import re

    from groceries_assistant.recipes.textparser import parse_servings

    text = re.sub(r"<[^>]+>", " ", html)
    return parse_servings(text)


def _minutes(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
