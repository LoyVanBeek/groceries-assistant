"""Import recipes from recipe websites via recipe-scrapers.

We fetch the HTML ourselves -- httpx with HTTP/2 and browser headers for most
sites, curl_cffi with a browser TLS fingerprint for ah.nl (via its loosely
protected ah.be twin), and a real headed browser as the last resort -- and
hand it to recipe-scrapers' offline parser, then run every ingredient string
through the Dutch line parser to split amount/unit/product.
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

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
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.5",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ah.nl recipe pages sit behind Akamai Bot Manager: TLS fingerprinting plus a
# JavaScript challenge, so no header or HTTP trick passes (only a real browser
# does; see fetch_html_browser). The Belgian twin ah.be serves the same
# Allerhande recipes (identical /allerhande/recept/<id>/<slug> paths) behind a
# looser Akamai config: a curl_cffi request with a browser TLS fingerprint
# gets in where httpx is refused. Not every fingerprint passes, and some are
# served a small HTTP-200 challenge page -- so a response only counts when the
# recipe markup is really in it.
AH_HOSTS = ("ah.nl", "www.ah.nl", "ah.be", "www.ah.be")
AH_RECIPE_PREFIX = "/allerhande/recept/"
# safari first: the one fingerprint that passes on ah.be today; the others are
# there in case AH's config shifts to a different browser profile
IMPERSONATE_TARGETS = ("safari184", "chrome150", "firefox155")
PASTE_DEFAULT_URL = "https://www.ah.nl/"
# neutral org_url for the pure-schema recipe-scrapers run: matches no dedicated
# scraper class, so a stale site scraper can't shadow working JSON-LD
SCHEMA_FALLBACK_URL = "https://schema-fallback.invalid/"


def _is_ah_host(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in AH_HOSTS
    except ValueError:
        return False


def ah_be_url(url: str) -> str | None:
    """The ah.be twin of an ah.nl Allerhande recipe URL, else None."""
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in ("ah.nl", "www.ah.nl"):
        return None
    if not parts.path.startswith(AH_RECIPE_PREFIX):
        return None
    return urlunparse(parts._replace(scheme="https", netloc="ah.be"))


def looks_like_recipe_html(html: str) -> bool:
    """Heuristic: does this HTML actually contain recipe markup?

    Akamai serves some fingerprints a small HTTP-200 challenge page, so the
    status code alone proves nothing.
    """
    return "application/ld+json" in html or "recipeIngredient" in html


def fetch_html_impersonated(
    url: str, timeout: float = 20.0, require_recipe_markup: bool = True
) -> str:
    """Fetch with a browser TLS fingerprint (curl_cffi).

    Sites that only run the TLS check refuse httpx's Python fingerprint but
    let real-browser ones through. Tries several fingerprints; returns "" when
    none of them works. With ``require_recipe_markup`` a response only counts
    when recipe markup is really in it -- Akamai serves some fingerprints a
    small HTTP-200 challenge page, so the status code alone proves nothing.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return ""
    for target in IMPERSONATE_TARGETS:
        try:
            response = curl_requests.get(url, impersonate=target, timeout=timeout)
        except Exception:
            continue
        if response.status_code == 200 and (
            looks_like_recipe_html(response.text) or not require_recipe_markup
        ):
            return response.text
    return ""


def normalize_pasted_page(text: str) -> str:
    """Accept a bare JSON-LD paste (no tags) by wrapping it in a script element."""
    stripped = text.strip()
    if stripped.startswith(("{", "[")) and "application/ld+json" not in stripped:
        return f'<script type="application/ld+json">{stripped}</script>'
    return text


@dataclass
class UrlParseResult(ParseResult):
    source_url: str = ""
    image_url: str | None = None


def fetch_html(url: str, timeout: float = 20.0) -> str:
    """Fetch a recipe page.

    AH recipe URLs go to the ah.be twin first (see fetch_html_impersonated);
    when that fails we raise so the caller falls back to a real browser --
    plain httpx on ah.nl is refused no matter what. Everything else keeps the
    HTTP/2 + browser-headers fetch that works for most recipe sites.
    """
    mirror = ah_be_url(url)
    if mirror is not None:
        html = fetch_html_impersonated(mirror, timeout)
        if html:
            return html
        raise ExtractionError(
            "ah.nl blokkeert automatische imports (bot-bescherming) en de "
            "ah.be-spiegel deed het ook niet."
        )
    if _is_ah_host(url):  # ah.be URL pasted directly
        html = fetch_html_impersonated(url, timeout)
        if html:
            return html
        raise ExtractionError(
            "ah.be liet deze import niet door; probeer de ah.nl-URL of plak "
            "de paginabron in de import."
        )
    try:
        with httpx.Client(http2=True, timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=BROWSER_HEADERS)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (403, 406, 429, 503):
            # TLS-gated (Cloudflare & co refuse the Python fingerprint with
            # 403): a browser TLS fingerprint may still get in
            html = fetch_html_impersonated(url, timeout, require_recipe_markup=False)
            if html:
                return html
        raise ExtractionError(f"Kon de pagina niet laden: {exc}") from exc
    return response.text


def fetch_html_browser(url: str, timeout_s: float = 60.0) -> str:
    """Fetch a recipe page in a real (headed) browser under a virtual display.

    Some sites (ah.nl) sit behind Akamai's JavaScript challenge: no header or
    TLS trick passes it — only a real browser does. Chromium runs headed under
    Xvfb (headless Chromium is detected and refused). Slow (~15 s) and only
    used when the plain fetch failed or bot-blocked.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExtractionError(
            "Deze site blokkeert automatische imports (bot-bescherming) en "
            "playwright is niet geinstalleerd."
        ) from exc

    launch_kwargs: dict = {
        "headless": False,  # headless is detected by Akamai
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if executable:
        launch_kwargs["executable_path"] = executable

    def _grab() -> str:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(locale="nl-NL", viewport={"width": 1280, "height": 800})
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_s * 1000)
            except Exception as exc:
                raise ExtractionError(f"Kon de pagina niet laden (browser): {exc}") from exc
            # the challenge/hydration needs a few seconds; wait until the
            # recipe markup shows up or give up after a few tries
            html = page.content()
            for _ in range(4):
                if "application/ld+json" in html or "recipeIngredient" in html:
                    break
                page.wait_for_timeout(4000)
                try:
                    page.reload(timeout=timeout_s * 1000)
                    html = page.content()
                except Exception:
                    break
            browser.close()
        return html

    # The web app runs on an asyncio loop; playwright's Sync API refuses to run
    # inside one. Run _grab on a worker thread with its own loop.
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            html: str = pool.submit(_grab).result(timeout=timeout_s + 120)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Kon de pagina niet laden (browser): {exc}") from exc

    if not looks_like_recipe_html(html):
        raise ExtractionError(
            "De pagina liet zich niet uitlezen (bot-bescherming bleef in de weg); "
            "plak de recepttekst, of de paginabron/JSON-LD, handmatig in de import."
        )
    return html


def extract_from_url(url: str, html: str | None = None) -> tuple[Recipe, UrlParseResult]:
    """Scrape a recipe URL into a Recipe plus the parse details.

    Tries, in order:
    1. recipe-scrapers: the site's dedicated scraper when it exists, then the
       schema.org extraction (some dedicated scrapers are stale and miss
       elements the site still serves in its JSON-LD).
    2. A dedicated site parser of our own (sites.py).
    3. A Dutch label scan ('Ingrediënten:' ... 'Bereiding:' free text).
    4. A generic text-heuristic fallback (bullet ingredients + numbered steps).

    An empty step-1 result (schema present, ingredients unreadable) falls
    through to the later steps; the Recipe model demands at least one
    ingredient, so a hollow parse is never returned.

    With ``html`` given (pasted page source or JSON-LD) nothing is fetched.
    """
    url = url.strip()
    if "://" not in url:  # tolerate 'www.ah.nl/...' pastes
        url = f"https://{url}"
    from groceries_assistant.recipes.sites import (
        RECIPE_SITES,
        dutch_label_fallback,
        generic_text_fallback,
    )

    if html is None:
        try:
            html = fetch_html(url)
        except ExtractionError:
            # blocked (Akamai etc.): try a real browser before giving up
            html = fetch_html_browser(url)

    # --- step 1: recipe-scrapers, dedicated site class then pure schema -----
    for org_url in (url, SCHEMA_FALLBACK_URL):
        try:
            scraper = scrape_html(html, org_url=org_url, wild_mode=True)
        except Exception:
            continue
        try:
            recipe, result = _build_from_scraper(scraper, url, html)
        except Exception:
            continue
        if result.ingredients:
            return recipe, result

    # --- step 2: site-specific parser ---------------------------------------
    host = urlparse(url).hostname or ""
    site_cls = RECIPE_SITES.get(host.lower())
    if site_cls is not None:
        try:
            result = site_cls.parse(html, url)
            if result is not None and result.ingredients:
                recipe = _build_result_to_recipe(result)
                return recipe, result
        except Exception:
            pass

    # --- step 3: Dutch label scan -------------------------------------------
    try:
        result = dutch_label_fallback(html, url)
        if result is not None and result.ingredients:
            recipe = _build_result_to_recipe(result)
            return recipe, result
    except Exception:
        pass

    # --- step 4: generic fallback -------------------------------------------
    try:
        result = generic_text_fallback(html, url)
        if result is not None and result.ingredients:
            recipe = _build_result_to_recipe(result)
            return recipe, result
    except Exception:
        pass

    raise ExtractionError(
        f"Geen recept gevonden op {url}. "
        "Plak de recepttekst, of de paginabron/JSON-LD, handmatig in de import."
    )


def _build_from_scraper(scraper, url: str, html: str) -> tuple[Recipe, UrlParseResult]:
    """Turn a recipe-scrapers result into a Recipe + ParseResult."""
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

    recipe = _build_result_to_recipe(result)
    return recipe, result


def _build_result_to_recipe(result: UrlParseResult) -> Recipe:
    """Build a Recipe from a parse result."""
    return Recipe.model_validate(
        {
            "name": result.name or "Naamloos recept",
            "slug": result.name or "naamloos-recept",
            "servings": result.servings or 4,
            "time": {"prep": result.prep_minutes or 0, "cook": result.cook_minutes or 0},
            "steps": result.steps,
            "url": result.source_url or None,
            "ingredients": [
                {"product": i.product, "amount": i.amount, "unit": i.unit}
                for i in result.ingredients
            ],
        }
    )


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
