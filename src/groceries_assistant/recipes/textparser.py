"""Rule-based parsing of Dutch recipe text into recipe fields.

Best effort by design: the web UI always lets the user correct the result
before saving. Handles decimal commas, unicode fractions, word numbers,
Dutch kitchen units (el/tl/blikje/snufje/...), common section headers,
servings and time lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from groceries_assistant.models import Ingredient

# --- amount parsing ----------------------------------------------------------

FRACTIONS = {
    "½": "0.5",
    "¼": "0.25",
    "¾": "0.75",
    "⅓": "0.33",
    "⅔": "0.67",
    "⅛": "0.125",
    "⅜": "0.375",
}

WORD_NUMBERS = {
    "een": 1,
    "één": 1,
    "twee": 2,
    "drie": 3,
    "vier": 4,
    "vijf": 5,
    "zes": 6,
    "zeven": 7,
    "acht": 8,
    "negen": 9,
    "tien": 10,
    "halve": 0.5,
    "half": 0.5,
    "paar": 3,
}

# singular -> canonical unit
UNIT_MAP = {
    "g": "g",
    "gram": "g",
    "gr": "g",
    "kg": "kg",
    "kilo": "kg",
    "kilogram": "kg",
    "mg": "mg",
    "ml": "ml",
    "milliliter": "ml",
    "cl": "cl",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "el": "el",
    "eetlepel": "el",
    "eetlepels": "el",
    "tl": "tl",
    "theelepel": "tl",
    "theelepels": "tl",
    "stuk": "stuk",
    "stuks": "stuk",
    "stukken": "stuk",
    "blik": "blik",
    "blikken": "blik",
    "blikje": "blikje",
    "blikjes": "blikje",
    "pak": "pak",
    "pakken": "pak",
    "bos": "bos",
    "bossen": "bos",
    "bosje": "bosje",
    "bosjes": "bosje",
    "teen": "teen",
    "tenen": "teen",
    "takje": "takje",
    "takjes": "takje",
    "snufje": "snufje",
    "snufjes": "snufje",
    "scheutje": "scheutje",
    "scheutjes": "scheutje",
    "handje": "handje",
    "handjes": "handje",
    "handvol": "handvol",
    "plak": "plak",
    "plakken": "plak",
    "plakje": "plakje",
    "plakjes": "plakje",
    "bakje": "bakje",
    "bakjes": "bakje",
    "pot": "pot",
    "potten": "pot",
    "fles": "fles",
    "flessen": "fles",
    "zak": "zak",
    "zakken": "zak",
    "rol": "rol",
    "rollen": "rol",
    "kopje": "kopje",
    "kopjes": "kopje",
    "kop": "kop",
    "borden": "bord",
    "bord": "bord",
    "borrelglas": "borrelglas",
    "blaadje": "blaadje",
    "blaadjes": "blaadje",
    "glass": "glas",
    "glas": "glas",
    "flesje": "flesje",
    "bak": "bak",
    "strook": "strook",
    "blokje": "blokje",
    "blokjes": "blokje",
    "sneetje": "sneetje",
    "sneetjes": "sneetje",
    "clementine": "stuk",
}

UNIT_WORDS = frozenset(UNIT_MAP)

ARTICLES = ("de ", "het ", "een ", "verse ", "vers ", "verse,", "fijne ")

INGREDIENT_HEADER = re.compile(
    r"^\s*(ingred\w*nten|ingredienten|je hebt nodig|je nodig hebt|boodschappenlijst|"
    r"voor de |voor het |dat heb je nodig|benodigdheden)\b",
    re.IGNORECASE,
)
STEP_HEADER = re.compile(
    r"^\s*(bereiding|bereidingswijze|instructies|aanbereiden|werkwijze|aan de slag|"
    r"instructie|bereiden|stappen)\b",
    re.IGNORECASE,
)
SERVINGS_PATTERNS = [
    re.compile(
        r"(?:voor\s+)?(\d{1,3})\s*(?:tot|of|/-)?\s*(?:\d{1,3})?"
        r"\s*(?:grote\s+|kleine\s+)?(?:personen|persones|porties|pax)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:aantal\s+)?(?:personen|porties|persones)\s*[:=]?\s*(\d{1,3})", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*(?:persoonsmaaltijd|portie)\b", re.IGNORECASE),
]
TIME_KEYWORD_COOK = (
    "bereidingstijd",
    "kooktijd",
    "oventijd",
    "totale tijd",
    "totale bereidingstijd",
)
TIME_KEYWORD_PREP = ("voorbereidingstijd", "voorbereiding", "voorbereiden")
TIME_SPAN = re.compile(r"(\d+)\s*(minuten|min|mins|m|uur|u|uren|uren|h)\b", re.IGNORECASE)
AMOUNT_TOKEN = re.compile(
    r"^(?:\d{1,4}(?:[.,]\d{1,3})?|\d+/\d+|\d{1,4}(?:[.,]\d{1,3})?\s*[-–]\s*\d{1,4}(?:[.,]\d{1,3})?)$"
)
LIST_MARKER = re.compile(r"^\s*(?:[-–*•·]|\d{1,2}[.)::])\s+")
LEADING_ARTICLE = re.compile(r"^(?:de|het|een|n?\d+\s?)\s+", re.IGNORECASE)


@dataclass
class ParseResult:
    name: str | None = None
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    ingredients: list[Ingredient] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_fraction_unicode(text: str) -> str:
    for char, digits in FRACTIONS.items():
        text = text.replace(char, f" {digits} ")
    return text


def parse_amount(token: str) -> float | None:
    """Parse '400', '1,5', '1 1/2', '2-3', '½' into a float."""
    token = token.strip().lower().replace(",", ".")
    for char, digits in FRACTIONS.items():
        token = token.replace(char, f" {digits} ")
    token = token.strip()

    # mixed number like "1 1/2"
    mixed = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", token)
    if mixed:
        return int(mixed.group(1)) + int(mixed.group(2)) / int(mixed.group(3))

    simple = re.fullmatch(r"(\d+)/(\d+)", token)
    if simple:
        return int(simple.group(1)) / int(simple.group(2))

    range_match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*\d+(?:\.\d+)?)?", token)
    if range_match:
        return float(range_match.group(1))

    try:
        return float(token)
    except ValueError:
        return None


def parse_ingredient_line(line: str) -> Ingredient | None:
    """Parse one ingredient line; None when the line is not an ingredient."""
    cleaned = LIST_MARKER.sub("", line.strip())
    if not cleaned:
        return None

    words = parse_fraction_unicode(cleaned).split()
    if not words:
        return None

    amount: float | None = None
    unit = "stuk"
    product_start = 0

    # word-number start ("een handvol pijnboompitten", "twee uien")
    first = words[0].lower().strip(",")
    if first in WORD_NUMBERS:
        amount = float(WORD_NUMBERS[first])
        product_start = 1
    elif first in UNIT_WORDS:
        # "snufje peper", "handvol amandelen": unit without explicit amount
        amount = 1.0
        unit = UNIT_MAP[first]
        product_start = 1
    elif AMOUNT_TOKEN.match(first):
        amount = parse_amount(first)
        product_start = 1
    else:
        return None  # no leading amount -> not an ingredient line

    # optional second numeric token ("1 1/2", "1 ½")
    if product_start < len(words):
        second = words[product_start].lower().strip(",")
        if AMOUNT_TOKEN.match(second) or second in FRACTIONS:
            extra = parse_amount(second)
            if extra is not None:
                amount = (amount or 0) + extra
                product_start += 1

    # optional unit word
    if product_start < len(words):
        candidate = words[product_start].lower().strip(".,")
        if candidate in UNIT_WORDS:
            unit = UNIT_MAP[candidate]
            product_start += 1

    product = " ".join(words[product_start:]).strip().strip(",;")
    if not product:
        return None
    product = _clean_product(product)
    if not product:
        return None

    return Ingredient(product=product, amount=amount if amount else 1.0, unit=unit)


def _clean_product(product: str) -> str:
    lowered = product.lower()
    for article in ("fijngesneden ", "fijngehakt ", "in stukjes ", "in plakjes "):
        if lowered.startswith(article):
            product = product[len(article) :]
            lowered = product.lower()
    product = re.sub(r"\s{2,}", " ", product).strip(" ,;")
    return product


def parse_servings(text: str) -> int | None:
    for line in text.splitlines():
        for pattern in SERVINGS_PATTERNS:
            match = pattern.search(line)
            if match:
                return int(match.group(1))
    return None


def parse_times(text: str) -> tuple[int | None, int | None]:
    """Return (prep_minutes, cook_minutes)."""
    prep: int | None = None
    cook: int | None = None
    for line in text.splitlines():
        lowered = line.lower()
        spans = _line_minutes(lowered)
        if not spans:
            continue
        if any(k in lowered for k in TIME_KEYWORD_PREP) and prep is None:
            prep = spans
        elif (any(k in lowered for k in TIME_KEYWORD_COOK) and cook is None) or cook is None:
            cook = spans
    return prep, cook


def _line_minutes(line: str) -> int | None:
    total = 0
    found = False
    for number, unit in TIME_SPAN.findall(line):
        number = int(number)
        unit = unit.lower()
        if unit in ("uur", "u", "uren", "h"):
            total += number * 60
        else:
            total += number
        found = True
    return total if found else None


def _looks_like_ingredient(line: str) -> bool:
    first = parse_fraction_unicode(line.strip()).split()
    if not first:
        return False
    token = first[0].lower().strip(",")
    return bool(AMOUNT_TOKEN.match(token) or token in WORD_NUMBERS)


def _strip_step_marker(line: str) -> str:
    return LIST_MARKER.sub("", line.strip()).strip()


def parse_recipe_text(text: str) -> ParseResult:
    """Parse free Dutch recipe text into structured fields."""
    result = ParseResult()
    lines = [line.rstrip() for line in text.splitlines()]

    mode = "seek"  # seek | ingredients | steps
    non_empty = [(i, line) for i, line in enumerate(lines) if line.strip()]
    if not non_empty:
        result.warnings.append("Geen tekst om te parsen")
        return result

    result.servings = parse_servings(text)
    result.prep_minutes, result.cook_minutes = parse_times(text)

    first_done = False
    for _, line in non_empty:
        stripped = line.strip()
        lowered = stripped.lower()

        if INGREDIENT_HEADER.match(stripped):
            mode = "ingredients"
            # "Ingrediënten (voor 4 personen)"
            if result.servings is None:
                result.servings = parse_servings(stripped)
            continue
        if STEP_HEADER.match(stripped):
            mode = "steps"
            continue
        # "Voor de saus:" style sub-headers inside steps -> back to ingredients
        if mode == "steps" and re.match(r"^\s*voor (de|het)\b", lowered) and stripped.endswith(":"):
            mode = "ingredients"
            continue
        if (
            re.match(r"^\s*(bereidingstijd|voorbereidingstijd|wachtijd|oventijd)\s*[:=]?", lowered)
            and _line_minutes(lowered) is not None
        ):
            continue  # already handled by parse_times
        if _is_servings_line(stripped):
            continue  # already handled by parse_servings

        # candidate title: short, no amount, not a step sentence
        if (
            not first_done
            and mode == "seek"
            and len(stripped) <= 60
            and not _looks_like_ingredient(stripped)
            and not stripped.endswith((".", "!", "?"))
        ):
            result.name = stripped.strip(":").strip()
            first_done = True
            continue

        if mode == "ingredients":
            ingredient = parse_ingredient_line(stripped)
            if ingredient is not None:
                result.ingredients.append(ingredient)
            elif _looks_like_step(stripped):
                mode = "steps"
                result.steps.append(_strip_step_marker(stripped))
            else:
                # quantity-less ingredient ("zout en peper", "olijfolie om te bakken")
                cleaned = _clean_product(LIST_MARKER.sub("", stripped))
                if cleaned:
                    result.ingredients.append(Ingredient(product=cleaned, amount=1, unit="stuk"))
                    result.warnings.append(f"Geen hoeveelheid herkend: '{cleaned}'")
            continue

        if mode == "steps":
            result.steps.append(_strip_step_marker(stripped))
            continue

        # seek mode without headers
        if _looks_like_ingredient(stripped):
            ingredient = parse_ingredient_line(stripped)
            if ingredient is not None:
                result.ingredients.append(ingredient)
                continue
        result.steps.append(_strip_step_marker(stripped))

    if not result.ingredients:
        result.warnings.append("Geen ingrediënten herkend")
    if not result.steps:
        result.warnings.append("Geen bereidingsstappen herkend")
    if result.servings is None:
        result.warnings.append("Geen aantal personen gevonden (standaard 4)")
    return result


def _is_servings_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in SERVINGS_PATTERNS)


def _looks_like_step(line: str) -> bool:
    """Heuristic: imperative Dutch sentences are steps, not ingredients."""
    lowered = line.lower()
    if _looks_like_ingredient(line):
        return False
    step_verbs = (
        "snijd",
        "snipper",
        "bak",
        "kook",
        "verwarm",
        "meng",
        "roer",
        "voeg",
        "giet",
        "verdeel",
        "bestrooi",
        "blaas",
        "laat",
        "verhit",
        "fruit",
        "blancheer",
        "maak",
        "beleg",
        "klop",
        " Puree",
        "pureer",
        "garneer",
        "verdeel",
        "serveer",
        "smelt",
        "doe ",
        "neem ",
        "haal ",
        "schep ",
        "kruid ",
        "braad",
        "grill",
        "warm ",
    )
    return any(lowered.startswith(verb) for verb in step_verbs)
