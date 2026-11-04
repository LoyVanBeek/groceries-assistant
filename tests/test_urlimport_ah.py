"""Albert Heijn (Allerhande) import: ah.be mirror, impersonated fetch, paste mode.

ah.nl sits behind Akamai Bot Manager (TLS fingerprinting + JS challenge). The
fetch chain for its recipe URLs is: ah.be twin via curl_cffi browser
fingerprints -> real headed browser -> error suggesting the paste mode. These
tests cover the dispatch and acceptance logic offline (no live requests).
"""

import sys
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from groceries_assistant.recipes.extract import ExtractionError
from groceries_assistant.recipes.urlimport import (
    ah_be_url,
    extract_from_url,
    fetch_html,
    fetch_html_impersonated,
    normalize_pasted_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ah_recipe_jsonld.html"

AH_RECIPE_URL = (
    "https://www.ah.nl/allerhande/recept/R-R1199309/courgettelasagne-met-3-kazen-en-gehakt"
)
RECIPE_PAGE = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Recipe","name":"Pasta",'
    '"recipeIngredient":["400 g pasta"],"recipeInstructions":"Kook."}'
    "</script></head><body></body></html>"
)
CHALLENGE_PAGE = '<html><body><div id="sec-if-cpt-container">Akamai challenge</div></body></html>'


class FakeCurlResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


# --- ah_be_url -----------------------------------------------------------------


def test_ah_be_url_maps_ah_nl_recipe_urls():
    assert ah_be_url(AH_RECIPE_URL) == AH_RECIPE_URL.replace("www.ah.nl", "ah.be")
    assert (
        ah_be_url("https://ah.nl/allerhande/recept/R-R1199309/x")
        == "https://ah.be/allerhande/recept/R-R1199309/x"
    )


def test_ah_be_url_ignores_other_urls():
    assert ah_be_url("https://www.ah.nl/producten/wi1234") is None  # not a recipe path
    assert ah_be_url("https://ah.be/allerhande/recept/R-R1199309/x") is None
    assert ah_be_url("https://example.com/allerhande/recept/R-R1199309/x") is None
    assert ah_be_url("geen url") is None


# --- fetch_html_impersonated ---------------------------------------------------


def test_fetch_html_impersonated_skips_challenge_and_accepts_recipe(monkeypatch):
    """A 200 challenge page is not a recipe; the next fingerprint gets its turn."""
    responses = [
        FakeCurlResponse(403, "Access Denied"),
        FakeCurlResponse(200, CHALLENGE_PAGE),
        FakeCurlResponse(200, RECIPE_PAGE),
    ]
    calls: list[tuple[str, str]] = []

    def fake_get(url: str, impersonate: str | None = None, timeout: float | None = None):
        calls.append((url, impersonate or ""))
        return responses[len(calls) - 1]

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)

    html = fetch_html_impersonated("https://ah.be/allerhande/recept/R-R1199309/x")

    assert "ld+json" in html
    assert [imp for _, imp in calls] == ["safari184", "chrome150", "firefox155"]


def test_fetch_html_impersonated_gives_up_when_every_target_challenges(monkeypatch):
    monkeypatch.setattr(
        "curl_cffi.requests.get",
        lambda url, impersonate=None, timeout=None: FakeCurlResponse(200, CHALLENGE_PAGE),
    )
    assert fetch_html_impersonated("https://ah.be/allerhande/recept/R-R1199309/x") == ""


def test_fetch_html_impersonated_markup_flag(monkeypatch):
    """Generic TLS-retry accepts any 200; the AH path demands recipe markup."""
    plain = "<html><body>200 maar geen recept-markup</body></html>"
    monkeypatch.setattr(
        "curl_cffi.requests.get",
        lambda url, impersonate=None, timeout=None: FakeCurlResponse(200, plain),
    )
    assert fetch_html_impersonated("https://example.com/x", require_recipe_markup=False) == plain
    assert fetch_html_impersonated("https://example.com/x") == ""


def test_fetch_html_impersonated_without_curl_cffi_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    assert fetch_html_impersonated("https://ah.be/x") == ""


# --- fetch_html dispatch -------------------------------------------------------


def test_fetch_html_prefers_ah_be_mirror_for_ah_nl_recipes(monkeypatch):
    seen: dict = {}

    def fake_impersonated(url, timeout=20.0):
        seen["url"] = url
        return RECIPE_PAGE

    monkeypatch.setattr(
        "groceries_assistant.recipes.urlimport.fetch_html_impersonated", fake_impersonated
    )

    html = fetch_html(AH_RECIPE_URL)

    assert html == RECIPE_PAGE
    assert seen["url"] == AH_RECIPE_URL.replace("www.ah.nl", "ah.be")


def test_fetch_html_raises_for_browser_fallback_when_mirror_fails(monkeypatch):
    """extract_from_url catches this and retries the real browser path."""
    monkeypatch.setattr(
        "groceries_assistant.recipes.urlimport.fetch_html_impersonated",
        lambda url, timeout=20.0: "",
    )
    with pytest.raises(ExtractionError, match=r"ah\.be-spiegel"):
        fetch_html(AH_RECIPE_URL)


def test_fetch_html_uses_impersonation_for_pasted_ah_be_urls(monkeypatch):
    monkeypatch.setattr(
        "groceries_assistant.recipes.urlimport.fetch_html_impersonated",
        lambda url, timeout=20.0: RECIPE_PAGE,
    )
    assert fetch_html("https://ah.be/allerhande/recept/R-R1199309/x") == RECIPE_PAGE


@respx.mock
def test_fetch_html_keeps_plain_httpx_for_other_sites(respx_mock):
    respx_mock.get("https://example.com/recept").mock(return_value=Response(200, text=RECIPE_PAGE))
    assert fetch_html("https://example.com/recept") == RECIPE_PAGE


@respx.mock
def test_fetch_html_retries_impersonated_on_403(respx_mock, monkeypatch):
    """Cloudflare-style TLS gate: plain httpx is refused, the browser
    fingerprint gets in."""
    respx_mock.get("https://example.com/recept").mock(return_value=Response(403, text="denied"))
    monkeypatch.setattr(
        "groceries_assistant.recipes.urlimport.fetch_html_impersonated",
        lambda url, timeout=20.0, require_recipe_markup=True: RECIPE_PAGE,
    )
    assert fetch_html("https://example.com/recept") == RECIPE_PAGE


@respx.mock
def test_fetch_html_no_impersonated_retry_on_connection_error(respx_mock, monkeypatch):
    respx_mock.get("https://example.com/recept").mock(side_effect=httpx.ConnectError("dns"))
    seen = []
    monkeypatch.setattr(
        "groceries_assistant.recipes.urlimport.fetch_html_impersonated",
        lambda *a, **k: seen.append(a) or "",
    )
    with pytest.raises(ExtractionError, match="Kon de pagina niet laden"):
        fetch_html("https://example.com/recept")
    assert seen == []  # connection errors are not a TLS gate; don't retry


# --- paste mode ----------------------------------------------------------------


def test_normalize_pasted_page_wraps_bare_jsonld():
    wrapped = normalize_pasted_page('{"@type": "Recipe", "name": "Pasta"}')
    assert wrapped.startswith('<script type="application/ld+json">')
    assert '"@type": "Recipe"' in wrapped


def test_normalize_pasted_page_keeps_full_source_and_script_tags():
    full = "<html><body>paginabron</body></html>"
    assert normalize_pasted_page(full) == full
    tagged = '<script type="application/ld+json">{"@type":"Recipe"}</script>'
    assert normalize_pasted_page(tagged) == tagged


def test_extract_from_url_parses_pasted_ah_page():
    """A pasted ah.nl page source parses offline into a full recipe."""
    recipe, parsed = extract_from_url(AH_RECIPE_URL, html=FIXTURE.read_text())

    assert recipe.name == "Rijkgevulde vegan pastasalade"
    assert recipe.servings == 4
    assert recipe.time.cook == 20
    assert recipe.url == AH_RECIPE_URL
    assert recipe.ingredients[0].amount == 300
    assert recipe.ingredients[0].unit == "g"
    assert recipe.ingredients[0].product == "fusilli"
    assert len(recipe.steps) >= 3
    assert parsed.image_url and "static.ah.nl" in parsed.image_url


def test_extract_from_url_parses_bare_jsonld_paste():
    """Pasting just the JSON-LD script content works too."""
    fixture = FIXTURE.read_text()
    start = fixture.index(">", fixture.index("ld+json")) + 1
    bare = fixture[start : fixture.index("</script>")]

    recipe, _parsed = extract_from_url(AH_RECIPE_URL, html=normalize_pasted_page(bare))

    assert recipe.name == "Rijkgevulde vegan pastasalade"
    assert len(recipe.ingredients) == 11


# --- stale dedicated scrapers and hollow schema results -------------------------


class FakeScraper:
    """Shape of a recipe-scrapers result, canned."""

    def __init__(self, title="Pasta", ingredients=None, empty=False):
        self._title = title
        self._ingredients = ingredients or []
        self._empty = empty

    def title(self):
        return self._title

    def ingredients(self):
        return self._ingredients

    def instructions(self):
        return "Kook de pasta."

    def yields(self):
        return "4"

    def total_time(self):
        return None

    def prep_time(self):
        return None

    def cook_time(self):
        return None

    def image(self):
        return None

    @property
    def schema(self):
        class S:
            def instructions(self_inner):
                return "Kook de pasta."

        return S()


def test_extract_retries_scrape_html_with_neutral_org(monkeypatch):
    """A stale dedicated scraper must not shadow working JSON-LD: the retry
    runs the pure-schema extraction with a neutral org_url."""
    import groceries_assistant.recipes.urlimport as urlimport

    orgs = []

    def fake_scrape_html(html, org_url=None, wild_mode=None):
        orgs.append(org_url)
        if "keukenliefde.nl" in (org_url or ""):
            raise RuntimeError("ElementNotFoundInHtml (stale scraper)")
        return FakeScraper(ingredients=["400 g pasta"])

    monkeypatch.setattr(urlimport, "scrape_html", fake_scrape_html)

    recipe, _parsed = extract_from_url("https://www.keukenliefde.nl/recepten/x/")

    assert orgs[0].startswith("https://www.keukenliefde.nl")
    assert "schema-fallback" in orgs[1]
    assert [i.product for i in recipe.ingredients] == ["pasta"]


def test_extract_hollow_schema_never_returned(monkeypatch):
    """A 0-ingredient schema parse can't become a Recipe (the model demands at
    least one ingredient) -- it must fall through and end in the error."""
    import groceries_assistant.recipes.urlimport as urlimport

    monkeypatch.setattr(
        urlimport, "scrape_html", lambda html, org_url=None, wild_mode=None: FakeScraper(empty=True)
    )

    with pytest.raises(ExtractionError, match="Geen recept"):
        extract_from_url("https://example.com/x", html="<html><body>tekst</body></html>")


def test_extract_empty_schema_does_not_block_better_parsers(monkeypatch):
    """A hollow schema result must not win over the fallback parsers."""
    import groceries_assistant.recipes.urlimport as urlimport

    monkeypatch.setattr(
        urlimport, "scrape_html", lambda html, org_url=None, wild_mode=None: FakeScraper(empty=True)
    )
    html = (
        "<html><head><title>Soep van de markt</title></head><body>"
        "<p>Ingrediënten:</p><p>500 g tomaten</p><p>1 ui</p>"
        "<p>Bereiding:</p><p>Fruit de ui en voeg de tomaten toe, laat 20 min zachtjes koken.</p>"
        "</body></html>"
    )

    recipe, _parsed = extract_from_url("https://example.com/soep", html=html)

    assert recipe.name == "Soep van de markt"
    assert [i.product for i in recipe.ingredients] == ["tomaten", "ui"]
    assert len(recipe.steps) == 1


# --- Dutch label scan -----------------------------------------------------------


FATIMA_HTML = """
<html><head><title>Tortilla Kip Taartjes - Uit de keuken van Fatima</title></head>
<body>
<p>Leuke intro tekst over dit gerecht dat heel lekker is en snel klaar.</p>
<p>Ingredi&euml;nten:</p>
<p>Kip vulling:</p>
<p>2 etl. zonnebloemolie</p>
<p>1 rode ui, fijngesneden</p>
<p>300 gr. kipfilet, in kleine stukjes</p>
<p>2 tomaten, in stukjes</p>
<p>Bereiding:</p>
<p>Stap 1.</p>
<p>Verwarm de zonnebloemolie in de koekenpan en bak de ui en knoflook aan op middelhoog vuur.</p>
<p>Stap 2.</p>
<p>Plaats een kom op de tortilla en snijd er cirkels uit.</p>
<p>Extra tips&amp;tricks:</p>
<p>Vervang de kip door gehakt.</p>
</body></html>
"""


def test_dutch_label_fallback_parses_free_text_recipe():
    from groceries_assistant.recipes.sites import dutch_label_fallback

    result = dutch_label_fallback(FATIMA_HTML, "https://uitdekeukenvanfatima.nl/recipe/x/")

    assert result is not None
    assert result.name == "Tortilla Kip Taartjes - Uit de keuken van Fatima"
    products = {i.product: (i.amount, i.unit) for i in result.ingredients}
    assert products["zonnebloemolie"] == (2, "el")  # '2 etl.' normalized
    assert products["kipfilet"] == (300, "g")  # '300 gr.' parsed
    assert "rode ui" in products
    assert len(result.steps) == 2
    assert not any("Stap" in s for s in result.steps)
    assert not any("gehakt" in s for s in result.steps)  # stops at 'Extra tips'


def test_extract_from_url_uses_label_scan_for_schemaless_pages():
    """Full chain: schemaless Dutch blog page parses via the label scan."""
    recipe, _parsed = extract_from_url(
        "https://uitdekeukenvanfatima.nl/recipe/x/", html=FATIMA_HTML
    )

    assert recipe.name == "Tortilla Kip Taartjes - Uit de keuken van Fatima"
    assert len(recipe.ingredients) >= 3
    assert len(recipe.steps) == 2
