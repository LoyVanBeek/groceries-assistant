import io
import os
import textwrap
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from groceries_assistant.models import Recipe
from groceries_assistant.recipes.extract import (
    ExtractionError,
    build_recipe_from_parse,
    extract_from_text,
    ocr_images,
    recipe_to_yaml,
    unique_recipe_path,
)
from groceries_assistant.recipes.textparser import parse_recipe_text
from groceries_assistant.recipes.urlimport import extract_from_url

# --- OCR ----------------------------------------------------------------------


@pytest.fixture
def fake_tesseract(tmp_path, monkeypatch):
    """A fake tesseract binary that prints canned OCR text per page."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "tesseract"
    script.write_text(
        "#!/bin/sh\n"
        'echo "tesseract $*" >> "$FAKE_LOG"\n'
        'echo "Spaghetti carbonara"\n'
        'echo ""\n'
        'echo "400 g spaghetti"\n'
        'echo "Snijd de ui."\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    return tmp_path / "calls.log"


def test_ocr_image_runs_binary(fake_tesseract, tmp_path):
    image = tmp_path / "page1.png"
    image.write_bytes(b"\x89PNG fake")

    text = ocr_images([image])
    assert "Spaghetti carbonara" in text
    assert "400 g spaghetti" in text
    log = fake_tesseract.read_text()
    assert "stdout -l nld --psm 4" in log


def test_ocr_multiple_pages_get_markers(fake_tesseract, tmp_path):
    image1 = tmp_path / "page1.png"
    image2 = tmp_path / "page2.png"
    image1.write_bytes(b"x")
    image2.write_bytes(b"y")

    text = ocr_images([image1, image2])
    assert "--- pagina 1 ---" in text
    assert "--- pagina 2 ---" in text


def test_ocr_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    with pytest.raises(ExtractionError, match="Tesseract"):
        ocr_images([image])


def test_ocr_missing_file_raises(tmp_path):
    with pytest.raises(ExtractionError, match="niet gevonden"):
        ocr_images([tmp_path / "nope.png"])


# --- text -> recipe -----------------------------------------------------------


def test_extract_from_text_builds_recipe():
    text = textwrap.dedent(
        """
        Boerenkool stamppot

        Voor 4 personen
        Bereidingstijd: 35 minuten

        Ingrediënten:
        1,2 kg aardappelen
        400 g boerenkool
        2 el melk

        Bereiding
        Kook de aardappelen gaar.
        Stamp met de melk en meng de boerenkool erdoor.
        """
    )
    recipe, parsed = extract_from_text(text)
    assert recipe.name == "Boerenkool stamppot"
    assert recipe.servings == 4
    assert recipe.time.cook == 35
    assert recipe.ingredients[0].product == "aardappelen"
    assert recipe.ingredients[0].amount == 1.2
    assert recipe.ingredients[0].unit == "kg"
    assert len(recipe.steps) == 2
    assert parsed.warnings == []


def test_recipe_to_yaml_is_editable_and_loadable():
    recipe, _ = extract_from_text("Test\n\nIngrediënten:\n1 ui\n")
    yaml_text = recipe_to_yaml(recipe)
    assert "grocy" not in yaml_text
    assert "slug" not in yaml_text
    assert "ui" in yaml_text

    reloaded = load_recipe_from_text(yaml_text)
    assert reloaded.name == recipe.name
    assert reloaded.ingredients[0].product == "ui"


def load_recipe_from_text(yaml_text: str) -> Recipe:
    import tempfile

    from groceries_assistant.recipes.loader import load_recipe

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    return load_recipe(Path(path))


def test_unique_recipe_path_handles_collisions(tmp_path):
    recipe, _ = extract_from_text("Test\n\nIngrediënten:\n1 ui\n")
    first = unique_recipe_path(recipe, tmp_path)
    first.write_text("x")
    second = unique_recipe_path(recipe, tmp_path)
    assert first.name == "test.yaml"
    assert second.name == "test-2.yaml"


def test_build_recipe_defaults_for_bad_parse():
    parsed = parse_recipe_text("totally unrelated english text only")
    recipe = build_recipe_from_parse("...", parsed)
    assert recipe.servings == 4  # default with warning
    assert parsed.warnings
    assert recipe.ingredients[0].product == "onbekend"


# --- URL import (offline via fixture html) ------------------------------------


SCHEMA_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "Pasta pesto",
  "recipeYield": "4 personen",
  "totalTime": "PT25M",
  "recipeIngredient": [
    "400 g pasta penne",
    "1 courgette",
    "2 el olijfolie"
  ],
  "recipeInstructions": "Kook de pasta.\\nBak de courgette."
}
</script>
</head><body>recept</body></html>
"""


@respx.mock
def test_extract_from_url_parses_schema_recipe(respx_mock):
    respx_mock.get("https://example.com/recept").mock(return_value=Response(200, text=SCHEMA_HTML))

    recipe, _parsed = extract_from_url("https://example.com/recept")

    assert recipe.name == "Pasta pesto"
    assert recipe.servings == 4
    assert recipe.time.cook == 25
    assert [i.product for i in recipe.ingredients] == ["pasta penne", "courgette", "olijfolie"]
    assert recipe.ingredients[0].amount == 400
    assert len(recipe.steps) == 2


def test_extract_from_url_invalid_html_raises():
    with pytest.raises(ExtractionError, match="Geen recept"):
        extract_from_url("https://x", html="<html><body>geen recept</body></html>")


def test_extract_from_url_bad_jsonld_raises(respx_mock):
    respx_mock.get("https://example.com/x").mock(
        return_value=Response(200, text="<html><body>tekst</body></html>")
    )
    with pytest.raises(ExtractionError):
        extract_from_url("https://example.com/x")


# silence unused-import warnings for helpers kept for the web UI
_ = io
_ = TestClient


def test_normalize_ocr_text_strips_leading_symbol_runs():
    from groceries_assistant.recipes.extract import normalize_ocr_text

    assert normalize_ocr_text("© 300 g pasta") == "300 g pasta"
    assert normalize_ocr_text("® 400 gr kipfilet") == "400 gr kipfilet"
    assert normalize_ocr_text("300 g fijne spaghetti") == "300 g fijne spaghetti"
    assert normalize_ocr_text("- 600 g spinazie") == "600 g spinazie"


def test_ocr_image_requires_file(tmp_path):
    import pytest

    from groceries_assistant.recipes.extract import ExtractionError, ocr_image

    with pytest.raises(ExtractionError, match="niet gevonden"):
        ocr_image(tmp_path / "missing.jpg")


def test_ocr_image_applies_exif_rotation(tmp_path):
    """EXIF-rotated photos must come out upright (regression from spike)."""
    import shutil

    import pytest

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract not installed on host (runs in Docker)")

    from PIL import Image, ImageDraw, ImageFont

    from groceries_assistant.recipes.extract import ocr_image

    img = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 40), "400 g spaghetti", fill="black", font=ImageFont.load_default(24))
    # store the image rotated 90 CCW with EXIF 6 (= rotate 90 CW on display)
    sideways = img.transpose(Image.Transpose.ROTATE_90)
    exif = sideways.getexif()
    exif[274] = 6
    sideways.save(tmp_path / "photo.jpg", exif=exif)

    text = ocr_image(tmp_path / "photo.jpg")
    assert "spaghetti" in text
    assert "400" in text


def test_split_columns_two_column_layout():
    from groceries_assistant.recipes.extract import OcrLine, split_columns

    lines = [
        OcrLine("Titel van het recept", 50, 950, 10),          # full width -> header
        OcrLine("400 g spaghetti", 50, 400, 100),              # left column
        OcrLine("1 ui", 50, 200, 140),
        OcrLine("Kook de spaghetti.", 600, 950, 100),          # right column
        OcrLine("Fruit de ui.", 600, 940, 140),
    ]
    columns = split_columns(lines)
    assert [ln.text for ln in columns[0]] == ["Titel van het recept"]
    assert [ln.text for ln in columns[1]] == ["400 g spaghetti", "1 ui"]
    assert [ln.text for ln in columns[2]] == ["Kook de spaghetti.", "Fruit de ui."]


def test_split_columns_single_column_layout():
    from groceries_assistant.recipes.extract import OcrLine, split_columns

    lines = [
        OcrLine("400 g spaghetti", 50, 450, 10),
        OcrLine("Kook de spaghetti.", 60, 460, 60),
    ]
    columns = split_columns(lines)
    assert len(columns) == 1


def test_reclassify_moves_ingredient_lines_from_steps():
    from groceries_assistant.recipes.extract import reclassify_result
    from groceries_assistant.recipes.textparser import ParseResult

    parsed = ParseResult(
        name=None,
        steps=["Kook de spaghetti.", "2 el olijfolie", "300 g verse spinazie"],
    )
    parsed.ingredients = [__import__(
        "groceries_assistant.models", fromlist=["Ingredient"]
    ).Ingredient(product="spaghetti", amount=500, unit="g")]
    result = reclassify_result(parsed)
    products = [i.product for i in result.ingredients]
    assert "olijfolie" in products
    assert "verse spinazie" in products
    assert result.steps == ["Kook de spaghetti."]
    assert result.name is None or result.name != "Kook de spaghetti."


def test_reclassify_promotes_title_from_steps():
    from groceries_assistant.recipes.extract import reclassify_result
    from groceries_assistant.recipes.textparser import ParseResult

    parsed = ParseResult(name=None, steps=["Spaghetti bolognese met spinazie .", "Kook de pasta."])
    result = reclassify_result(parsed)
    assert result.name == "Spaghetti bolognese met spinazie"
    assert result.steps == ["Kook de pasta."]
