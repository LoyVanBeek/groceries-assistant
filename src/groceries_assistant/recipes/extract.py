"""Recipe extraction pipeline: OCR / pasted text -> structured recipe YAML."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from groceries_assistant.models import Recipe
from groceries_assistant.recipes.textparser import (
    ParseResult,
    parse_ingredient_line,
    parse_recipe_text,
)


class ExtractionError(Exception):
    """OCR or extraction failed."""


def exif_fixed_image(path: Path, tmp_dir: Path | None = None) -> Path:
    """Return a temp file with EXIF orientation applied (phone photos)."""
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError:
        return path
    try:
        with Image.open(path) as image:
            fixed = ImageOps.exif_transpose(image)
            if fixed is image:
                return path
            out = Path(tempfile.mkstemp(suffix=".png", dir=tmp_dir)[1])
            fixed.convert("RGB").save(out)
            return out
    except OSError:
        return path


def normalize_ocr_text(text: str) -> str:
    """Clean common OCR artifacts from printed/hand-annotated pages.

    Checkbox bullets and page furniture often land as symbol runs at the
    start of an ingredient line ('© 300 g pasta'); strip symbol runs at
    line start when an amount follows, so the amount parser sees them.
    """
    return re.sub(r"(?m)^[\W_]{1,3}(?=\s*\d)", "", text)


def ocr_image(path: Path, binary: str = "tesseract", lang: str = "nld") -> str:
    """Run Tesseract OCR on an image; returns plain text."""
    path = Path(path)
    if not path.is_file():
        raise ExtractionError(f"Afbeelding niet gevonden: {path}")
    fixed = exif_fixed_image(path)
    try:
        result = subprocess.run(
            [binary, str(fixed), "stdout", "-l", lang, "--psm", "4"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(
            f"'{binary}' niet gevonden. Installeer Tesseract OCR "
            "(in Docker zit die in de image; lokaal: apt install tesseract-ocr "
            "tesseract-ocr-nld)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"OCR timed out voor {path}") from exc
    finally:
        if fixed != path:
            fixed.unlink(missing_ok=True)
    if result.returncode != 0:
        raise ExtractionError(f"OCR mislukt voor {path}: {result.stderr.strip()[:200]}")
    return normalize_ocr_text(result.stdout)


def ocr_images(paths: list[Path], binary: str = "tesseract", lang: str = "nld") -> str:
    """OCR multiple pages, concatenated with page markers."""
    parts: list[str] = []
    for index, path in enumerate(paths, start=1):
        text = ocr_image(path, binary=binary, lang=lang)
        if len(paths) > 1:
            parts.append(f"--- pagina {index} ---\n{text}")
        else:
            parts.append(text)
    return "\n".join(parts).strip()


# --- layout-aware extraction (columns) ---------------------------------------


@dataclass
class OcrLine:
    text: str
    x0: int
    x1: int
    y: int


GUTTER_FRACTION = 0.12
FULL_WIDTH_FRACTION = 0.8


def tesseract_tsv_lines(path: Path, binary: str, lang: str) -> list[OcrLine]:
    """Run tesseract with TSV output and group words into lines."""
    result = subprocess.run(
        [str(binary), str(path), "stdout", "-l", lang, "--psm", "3", "tsv"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise ExtractionError(f"OCR mislukt voor {path}: {result.stderr.strip()[:200]}")
    lines: dict[tuple[int, int, int], list[tuple[int, int, int, str]]] = {}
    header_seen = False
    for raw in result.stdout.splitlines():
        cols = raw.split("\t")
        if len(cols) < 12:
            continue
        if not header_seen:
            header_seen = True  # first row is the TSV header
            continue
        level, _page, block, par, line, left, top, width, _height, _conf, text = (
            cols[0],
            cols[1],
            cols[2],
            cols[3],
            cols[4],
            cols[6],
            cols[7],
            cols[8],
            cols[9],
            cols[10],
            cols[11],
        )
        if level != "5" or not text.strip():
            continue
        key = (int(block), int(par), int(line))
        lines.setdefault(key, []).append(
            (int(left), int(top), int(width), text.strip())
        )
    out: list[OcrLine] = []
    for words in lines.values():
        words.sort(key=lambda w: w[0])
        x0 = min(w[0] for w in words)
        y = min(w[1] for w in words)
        x1 = max(w[0] + w[2] for w in words)
        out.append(OcrLine(text=" ".join(w[3] for w in words), x0=x0, x1=x1, y=y))
    out.sort(key=lambda line: line.y)
    return out


def split_columns(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Split lines into columns; full-width lines come first as their own group.

    A new column starts when a line begins well to the right of everything
    seen so far (gutter >= GUTTER_FRACTION of the page width). Simple greedy
    scan that handles the common 'ingredients left, steps right' layout.
    """
    if not lines:
        return []
    page_width = max(line.x1 for line in lines)
    threshold = GUTTER_FRACTION * page_width
    full_width = [ln for ln in lines if (ln.x1 - ln.x0) >= FULL_WIDTH_FRACTION * page_width]
    rest = [ln for ln in lines if (ln.x1 - ln.x0) < FULL_WIDTH_FRACTION * page_width]

    columns: list[list[OcrLine]] = []
    if full_width:
        columns.append(sorted(full_width, key=lambda ln: ln.y))
    current: list[OcrLine] = []
    max_x1 = 0
    for line in rest:
        if current and line.x0 > max_x1 + threshold:
            columns.append(sorted(current, key=lambda ln: ln.y))
            current = []
        current.append(line)
        max_x1 = max(max_x1, line.x1)
    if current:
        columns.append(sorted(current, key=lambda ln: ln.y))
    return columns


def ocr_page_columns(path: Path, binary: str = "tesseract", lang: str = "nld") -> list[str]:
    """OCR one page into per-column texts (layout-aware)."""
    fixed = exif_fixed_image(path)
    try:
        lines = tesseract_tsv_lines(fixed, binary, lang)
    except ExtractionError:
        raise
    finally:
        if fixed != path:
            fixed.unlink(missing_ok=True)
    if not lines:
        return []
    return ["\n".join(line.text for line in column) for column in split_columns(lines)]


def merge_parse_results(parts: list[ParseResult]) -> ParseResult:
    """Merge per-column parse results into one.

    Ingredients and steps concatenate (column order); the first non-empty
    name/servings/times win. Duplicated ingredient lines are removed.
    """
    merged = ParseResult()
    seen_ingredients: set[tuple[str, float, str]] = set()
    seen_steps: set[str] = set()
    for part in parts:
        if merged.name is None and part.name:
            merged.name = part.name
        if merged.servings is None and part.servings is not None:
            merged.servings = part.servings
        if merged.prep_minutes is None and part.prep_minutes is not None:
            merged.prep_minutes = part.prep_minutes
        if merged.cook_minutes is None and part.cook_minutes is not None:
            merged.cook_minutes = part.cook_minutes
        for ingredient in part.ingredients:
            key = (ingredient.product, ingredient.amount, ingredient.unit)
            if key not in seen_ingredients:
                seen_ingredients.add(key)
                merged.ingredients.append(ingredient)
        for step in part.steps:
            if step not in seen_steps:
                seen_steps.add(step)
                merged.steps.append(step)
        merged.warnings.extend(part.warnings)
    return merged


def reclassify_result(parsed: ParseResult) -> ParseResult:
    """Content-based safety net: ingredient-looking lines out of `steps`.

    Column detection can misassign lines (titles span both columns; hand-
    annotated prints have no clean gutter). Any step line that starts with
    an amount is an ingredient, regardless of where OCR put it. A short,
    amount-less first step is promoted to the recipe name when missing.
    """
    keep_steps: list[str] = []
    for step in parsed.steps:
        ingredient = parse_ingredient_line(step)
        if ingredient is not None and not _looks_like_step(step):
            if not any(
                i.product == ingredient.product and i.amount == ingredient.amount
                for i in parsed.ingredients
            ):
                parsed.ingredients.append(ingredient)
        else:
            keep_steps.append(step)
    parsed.steps = keep_steps

    if parsed.name is None:
        for index, step in enumerate(keep_steps):
            stripped = step.strip().strip(".").strip()
            if 0 < len(stripped) <= 60 and not _looks_like_step(stripped):
                parsed.name = stripped
                parsed.steps = keep_steps[:index] + keep_steps[index + 1 :]
                break
    return parsed


def _looks_like_step(line: str) -> bool:
    from groceries_assistant.recipes.textparser import _looks_like_step as parser_step

    return parser_step(line)


def extract_from_images(
    paths: list[Path], binary: str = "tesseract", lang: str = "nld"
) -> tuple[ParseResult, str]:
    """OCR photos (layout-aware, columns kept apart) and parse each column.

    Returns (merged parse result, raw text for reference).
    """
    parts: list[ParseResult] = []
    raw_blocks: list[str] = []
    for index, path in enumerate(paths, start=1):
        columns = ocr_page_columns(path, binary=binary, lang=lang)
        if not columns:  # TSV fallback: whole-page text as one column
            columns = [ocr_image(path, binary=binary, lang=lang)]
        raw_blocks.append(f"--- pagina {index} ---\n" + "\n\n".join(columns))
        for column in columns:
            parts.append(parse_recipe_text(column))
    return reclassify_result(merge_parse_results(parts)), "\n".join(raw_blocks).strip()


def build_recipe_from_parse(text: str, parsed: ParseResult) -> Recipe:
    """Turn a parse result into a (possibly imperfect) Recipe model."""
    name = parsed.name or "Naamloos recept"
    ingredients = [
        {"product": i.product, "amount": i.amount, "unit": i.unit} for i in parsed.ingredients
    ]
    if not ingredients:
        ingredients = [{"product": "onbekend", "amount": 1, "unit": "stuk"}]
    return Recipe.model_validate(
        {
            "slug": _placeholder_slug(name),
            "name": name,
            "servings": parsed.servings or 4,
            "time": {
                "prep": parsed.prep_minutes or 0,
                "cook": parsed.cook_minutes or 0,
            },
            "steps": parsed.steps,
            "ingredients": ingredients,
            "notes": _source_note(text),
        }
    )


def _placeholder_slug(name: str) -> str:
    from groceries_assistant.models import slugify

    return slugify(name)


def _source_note(text: str) -> str | None:
    return None


def recipe_to_yaml(recipe: Recipe) -> str:
    """Render a recipe as user-editable YAML (no grocy refs)."""
    data = recipe.model_dump(mode="json", exclude_none=True)
    data.pop("grocy", None)
    data.pop("slug", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip() + "\n"


def unique_recipe_path(recipe: Recipe, directory: Path) -> Path:
    """Free path in the recipes dir; colliding slugs get a numeric suffix."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = recipe.slug or "recept"
    candidate = directory / f"{base}.yaml"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}-{counter}.yaml"
        counter += 1
    return candidate


def extract_from_text(text: str) -> tuple[Recipe, ParseResult]:
    parsed = parse_recipe_text(text)
    return build_recipe_from_parse(text, parsed), parsed
