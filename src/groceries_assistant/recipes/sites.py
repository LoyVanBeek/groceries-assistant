"""Site-specific recipe parsers and a generic text-fallback heuristic.

Each parser takes (html, url) -> UrlParseResult | None.
``None`` means "not my content, try the next parser".

Registered in ``RECIPE_SITES`` keyed by the lowercased hostname.
"""

from __future__ import annotations

import re
from typing import Protocol

from groceries_assistant.models import Ingredient
from groceries_assistant.recipes.textparser import parse_ingredient_line
from groceries_assistant.recipes.urlimport import UrlParseResult


class SiteParser(Protocol):
    """A site parser with parse(html, url) -> UrlParseResult | None."""

    @classmethod
    def parse(cls, html: str, url: str) -> UrlParseResult | None: ...


RECIPE_SITES: dict[str, type[SiteParser]] = {}


def register(host: str):
    """Decorator: register a parser class for *host*."""

    def wrapper(cls: type[SiteParser]) -> type[SiteParser]:
        RECIPE_SITES[host] = cls
        return cls

    return wrapper


# ---------------------------------------------------------------------------
# Helpers shared by site parsers and the generic fallback
# ---------------------------------------------------------------------------


def _plain_text(html: str) -> str:
    """Strip script/style content, HTML tags and collapse whitespace.

    No <head> stripping: a stray or unclosed tag would make the non-greedy
    match swallow real content.
    """
    from html import unescape

    text = re.sub(r"<(script|style)\b.*?</\1>", "\n", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    text = re.sub(r"\s{2,}", " ", text)
    return unescape(text).strip()


def _title(html: str) -> str | None:
    """Extract <title> or <h1> text.

    Site suffixes are split off on |, en dash or em dash only -- a plain
    hyphen stays, so 'Zuid-Afrikaanse Bobotie | Knorr' keeps its name.
    """
    from html import unescape

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        t = re.sub(r"\s+", " ", unescape(m.group(1))).strip()
        parts = [p for p in re.split(r"\s*[–—|]\s*", t) if p.strip()]
        return parts[0].strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", unescape(m.group(1))).strip()
    return None


def _cook_minutes(text: str) -> int | None:
    """Extract time from e.g. '[40 min]', '25 min', '1 uur 15 min'."""
    m = re.search(r"\[(\d+)\s*min\]", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*min", text)
    if m:
        val = int(m.group(1))
        return val if 0 < val < 600 else None
    return None


def _og_image(html: str) -> str | None:
    """Extract og:image from HTML meta tags."""
    m = re.search(
        r'<meta[^>]+property\s*=\s*["\']og:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*property\s*=\s*["\']og:image["\']',
        html,
        re.I,
    )
    return m.group(1) if m else None


def _parse_bullet_list(lines: list[str]) -> list[Ingredient]:
    """Try to parse each line as an ingredient; unparseable ones become 1 stuk."""
    ingredients: list[Ingredient] = []
    for line in lines:
        stripped = line.strip().strip("• ").strip()
        if not stripped:
            continue
        # Skip HTML tags and label keywords
        if "<" in stripped or ">" in stripped:
            continue
        if stripped.lower().startswith(("boodschappenlijst", "ingrediënt", "ingrediënt")):
            continue
        ingredient = parse_ingredient_line(stripped)
        if ingredient is not None:
            ingredients.append(ingredient)
        else:
            ingredients.append(Ingredient(product=stripped, amount=1, unit="stuk"))
    return ingredients


def _numbered_steps(lines: list[str]) -> list[str]:
    """Extract lines that start with a digit-dot pair, stripping the number.

    Also splits inline on step markers like ``1. A … 2. B … 3. C …``
    found on a single line (Knorr product pages do this).
    """
    steps: list[str] = []
    for line in lines:
        stripped = line.strip()
        parts = re.split(r"\s+(?=\d+\s*[.:)])", stripped)
        if len(parts) > 1:
            for part in parts:
                m = re.match(r"^\s*(\d+)\s*[.:)]\s*(.*)", part.strip())
                if m and m.group(2).strip():
                    steps.append(m.group(2).strip())
        else:
            m = re.match(r"^\s*(\d+)\s*[.:)]\s*(.*)", stripped)
            if m:
                steps.append(m.group(2).strip())
    return steps


# ---------------------------------------------------------------------------
# Generic text fallback (last resort)
# ---------------------------------------------------------------------------


def generic_text_fallback(html: str, url: str) -> UrlParseResult | None:
    """Heuristic: find a bullet-list of ingredients + numbered steps."""
    text = _plain_text(html)

    # collect all bullet lines
    bullet_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("•") or stripped.startswith("-"):
            bullet_lines.append(stripped)

    # if there are fewer than 3 bullet lines, look for <li> texts
    if len(bullet_lines) < 3:
        items = re.findall(r"<li[^>]*>(.*?)</li>", html, re.S | re.I)
        bullet_lines = [re.sub(r"<[^>]+>", "", it).strip() for it in items if it.strip()]
        bullet_lines = [ln for ln in bullet_lines if not re.match(r"^\d", ln)]

    if len(bullet_lines) < 3:
        return None

    ingredients = _parse_bullet_list(bullet_lines)
    if len(ingredients) < 2:
        return None

    # numbered steps from the full text
    step_lines = [ln for ln in text.splitlines() if ln.strip()]
    steps = _numbered_steps(step_lines)
    if len(steps) < 2:
        return None

    name = _title(html)
    cook = _cook_minutes(text)

    return UrlParseResult(
        source_url=url,
        name=name,
        ingredients=ingredients,
        steps=steps,
        cook_minutes=cook,
        servings=4,
        image_url=_og_image(html),
    )


# ---------------------------------------------------------------------------
# Dutch label scan (no schema, free text between labels)
# ---------------------------------------------------------------------------

_START_LABEL = re.compile(r"(?i)ingredi[ëe]nt(en)?\s*:?\s*\n")
_STOP_INGREDIENT = re.compile(
    r"(?im)^\s*(bereiding|bereidingswijze|zo maak|instructies|aan de slag|aan het werk)\b[^\n]*$"
)
_STOP_STEPS = re.compile(
    r"(?im)^\s*(extra tips|tips|bewaar|print|vorig|volgend|waardeer|deel|bron|eet smakelijk)\b"
)
_STAP_MARKER = re.compile(r"(?i)^stap\s+\d+\s*[.:]?$")


def dutch_label_fallback(html: str, url: str) -> UrlParseResult | None:
    """Parse free-text recipes: 'Ingrediënten:' ... 'Bereiding:' blocks.

    Blogs without schema or clean lists (uitdekeukenvanfatima.nl,
    dedagelijksekost.nl, ...) hold the recipe as plain text between labels;
    the labels sit at a line start but not always on a line of their own.
    Lines without an amount or unit are skipped (group headers like 'Kip
    vulling:', prep notes like 'fijngesneden'); 'Stap N.' markers merge into
    the step after them.
    """
    text = _plain_text(html)
    start = _START_LABEL.search(text)
    if start is None:
        return None
    rest = text[start.end() :]
    stop = _STOP_INGREDIENT.search(rest)
    ingredient_block, steps_block = (
        (rest[: stop.start()], rest[stop.end() :]) if stop else (rest, "")
    )

    ingredients: list[Ingredient] = []
    for ln in ingredient_block.splitlines()[:40]:
        ln = ln.strip()
        if not ln or ln.endswith(":") or len(ln) > 60:
            continue  # group header or paragraph text
        # '300 gr. kipfilet, in kleine stukjes': the prep note after the comma
        # pollutes the product name; the amount+product stays behind
        cleaned = ln.split(",")[0].replace("etl.", "el").rstrip(",;. ")
        ingredient = parse_ingredient_line(cleaned)
        if ingredient is not None:
            ingredients.append(ingredient)
    if len(ingredients) < 2:
        return None

    steps: list[str] = []
    buf = ""
    for ln in steps_block.splitlines()[:60]:
        ln = ln.strip()
        if not ln:
            continue
        if _STOP_STEPS.match(ln):
            break
        if _STAP_MARKER.match(ln):
            if buf:
                steps.append(buf)
                buf = ""
            continue
        m = re.match(r"^(\d+)\s*[.:)]\s*(.*)$", ln)
        if m:
            if buf:
                steps.append(buf)
                buf = ""
            buf = m.group(2).strip()
        elif buf:
            buf = f"{buf} {ln}"
        else:
            buf = ln
        if buf and buf[-1] in ".!?":
            steps.append(buf)
            buf = ""
    if buf:
        steps.append(buf)
    steps = [re.sub(r"\s+", " ", s).strip(" .") for s in steps if len(s.strip()) > 10]

    name = _title(html)
    if not name and not steps:
        return None

    return UrlParseResult(
        source_url=url,
        name=name,
        ingredients=ingredients,
        steps=steps,
        image_url=_og_image(html),
    )


# ---------------------------------------------------------------------------
# Knorr product-page parser
# ---------------------------------------------------------------------------


@register("knorr.com")
@register("www.knorr.com")
class KnorrParser:
    """Extract recipes from Knorr product pages that lack schema markup.

    Knorr pages have a free-text block like:

        Boodschappenlijstje: • 225 g kipfilet(blokjes) • 100 g zeevruchten …
        Zo maak je 't [40 min] 1. Snipper de ui … 2. Verhit 1 el olie …
    """

    @classmethod
    def parse(cls, html: str, url: str) -> UrlParseResult | None:
        # --- ingredient block: "Boodschappenlijstje:" -----------------------
        # Exact match to avoid matching FAQ content that mentions 'Ingrediënten'
        idx = html.find("Boodschappenlijstje")
        if idx < 0:
            idx = html.find("Boodschappenlijst")
        if idx < 0:
            return None
        # Find the next instruction block boundary
        end_idx = html.find("Zo maak", idx)
        if end_idx < 0:
            end_idx = html.find("Bereidingswijze", idx)
        if end_idx < 0:
            end_idx = len(html)
        raw = html[idx:end_idx]

        # split on bullets, <br>, or newlines
        bullets = re.split(r"[•\n]", raw)
        ingredients = _parse_bullet_list(bullets)
        if len(ingredients) < 2:
            return None

        # --- instruction block: "Zo maak je 't [40 min]" -------------------
        m2 = re.search(
            r"Zo\s+maak\s+je\s+'t\s*(?:\[(\d+)\s*min\])?(.*?)(?:Nog\s+steeds\s+trek|$)",
            html,
            re.S | re.I,
        )
        steps: list[str] = []
        cook_minutes: int | None = None
        if m2:
            if m2.group(1):
                cook_minutes = int(m2.group(1))
            raw_steps = m2.group(2)
            step_lines = re.split(r"\n", raw_steps)
            steps = _numbered_steps(step_lines)

        name = _title(html)
        if not name:
            return None

        # servings: breadcrumb often says "2-3 Persoons" or "2 Personnen"
        servings = 4
        m = re.search(r"(\d)\s*[-–]\s*(\d)\s*(persoons|personen|porties)", html, re.I)
        if m:
            servings = max(int(m.group(1)), int(m.group(2)))
        else:
            m = re.search(r"(\d+)\s*(persoons|personen|porties)", html, re.I)
            if m:
                servings = int(m.group(1))

        return UrlParseResult(
            source_url=url,
            name=name,
            ingredients=ingredients,
            steps=steps,
            cook_minutes=cook_minutes,
            servings=servings,
            image_url=_og_image(html),
        )
