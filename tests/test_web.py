"""Web smoke tests: propose -> swap -> approve -> review -> AH deep-link."""

import re
from datetime import date, timedelta
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from groceries_assistant.models import slugify
from groceries_assistant.store.db import Store
from groceries_assistant.web.app import create_app

PASTA = """\
name: Pasta
servings: 4
time: {cook: 30}
ingredients:
  - {product: spaghetti, amount: 500, unit: g}
  - {product: ui, amount: 1, unit: stuk}
steps:
  - Kook de spaghetti.
"""


def make_client(tmp_path: Path, search_fn=None):
    recipes = tmp_path / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(tmp_path, search_fn=search_fn))


def seed_recipe(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    (recipes / "pasta.yaml").write_text(PASTA, encoding="utf-8")


def test_pages_render(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        assert client.get("/plan").status_code == 200
        assert client.get("/recipes").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/recipes/import").status_code == 200


def test_empty_recipe_dir_shows_import_hint(tmp_path):
    (tmp_path / "recipes").mkdir(parents=True, exist_ok=True)
    with make_client(tmp_path) as client:
        assert "Importeer" in client.get("/plan").text


def test_full_flow_to_ah_link(tmp_path):
    seed_recipe(tmp_path)
    search_results = {
        "spaghetti": [_FakeProduct(123, "AH Spaghetti 500 g")],
    }

    def search(query: str):
        return search_results.get(query.lower(), [])

    with make_client(tmp_path, search_fn=search) as client:
        plan_page = client.get("/plan")
        assert plan_page.status_code == 200
        assert "Pasta" in plan_page.text

        approve = client.post("/plan/approve", follow_redirects=False)
        assert approve.status_code == 303

        order = client.get("/order")
        assert order.status_code == 200
        assert "spaghetti" in order.text

        # tick 'ui' as already at home
        have = client.post(
            "/order/have",
            data={"product": "ui", "unit": "stuk", "have": "1"},
            follow_redirects=False,
        )
        assert have.status_code == 303

        matched = client.get("/order/match")
        assert "/producten/product/wi123" in matched.text  # matched article link
        link_page = client.get("/order/link")
        assert link_page.status_code == 200
        # spaghetti is matched via the stub search; ui is not in the stub
        assert "/mijnlijst/add-multiple" in link_page.text


def test_import_text_and_save_recipe(tmp_path):
    with make_client(tmp_path) as client:
        editor = client.post(
            "/recipes/import",
            data={
                "text": "Pasta pesto\nVoor 4 personen\n"
                "400 g spaghetti\n25 g basilicum\n"
                "Bereiding:\nKook de pasta."
            },
        )
        assert editor.status_code == 200
        assert "Pasta pesto" in editor.text

        saved = client.post(
            "/recipes/save",
            data={
                "name": "Pasta pesto",
                "servings": "4",
                "prep": "0",
                "cook": "25",
                "prep_ahead": "0",
                "protein": "vegetarisch",
                "ingredients": "400 g spaghetti\n25 g basilicum",
                "steps": "Kook de pasta.",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (tmp_path / "recipes" / "pasta-pesto.yaml").exists()

        listing = client.get("/recipes")
        assert listing.status_code == 200
        assert "Pasta pesto" in listing.text


def test_import_from_url_prefills_source_url(tmp_path, monkeypatch):
    """Importing by URL keeps that URL in the editor's source-link field."""
    import groceries_assistant.web.app as app_module
    from groceries_assistant.models import Recipe
    from groceries_assistant.recipes.textparser import parse_recipe_text
    from groceries_assistant.recipes.urlimport import UrlParseResult

    def fake_extract(url: str):
        parsed = parse_recipe_text(
            "Pasta pesto\nVoor 4 personen\n400 g spaghetti\nBereiding:\nKook de pasta."
        )
        recipe = Recipe(
            slug="pasta-pesto",
            name=parsed.name or "Pasta pesto",
            servings=parsed.servings or 4,
            steps=parsed.steps,
            ingredients=[
                {"product": i.product, "amount": i.amount, "unit": i.unit}
                for i in parsed.ingredients
            ]
            or [{"product": "spaghetti", "amount": 400, "unit": "g"}],
        )
        return recipe, UrlParseResult(source_url=url)

    monkeypatch.setattr(app_module, "extract_from_url", fake_extract)

    with make_client(tmp_path) as client:
        editor = client.post("/recipes/import", data={"url": "https://example.com/pasta-pesto"})
        assert editor.status_code == 200
        assert 'value="https://example.com/pasta-pesto"' in editor.text


def _fake_ocr():
    """Stand-in for the tesseract pipeline, naming each page after its own file.

    Records the paths it was handed (and whether they existed) so a test can
    assert the app hands over real temp files and cleans them up afterwards.
    """
    from groceries_assistant.recipes.textparser import parse_recipe_text

    seen: list[Path] = []
    alive: list[bool] = []

    def fake_extract(paths, binary="tesseract", lang="nld"):
        seen.extend(paths)
        alive.extend(path.is_file() for path in paths)
        pages = [f"Recept {path.stem}\n\n400 g spaghetti\n\nKook de spaghetti." for path in paths]
        return parse_recipe_text("\n".join(pages)), "raw"

    return fake_extract, seen, alive


def test_import_reads_several_photos_as_one_recipe(tmp_path, monkeypatch):
    """Front and back of one card: both photos are read as a single recipe."""
    import groceries_assistant.web.app as app_module

    fake, seen, alive = _fake_ocr()
    monkeypatch.setattr(app_module, "extract_from_images", fake)

    with make_client(tmp_path) as client:
        editor = client.post(
            "/recipes/import",
            files=[
                ("photos", ("voorkant.png", _png_bytes(), "image/png")),
                ("photos", ("achterkant.png", _png_bytes(), "image/png")),
            ],
        )
    assert editor.status_code == 200
    assert len(seen) == 2, "beide foto's moeten aan de OCR mee"
    assert all(alive), "temp bestanden bestaan tijdens het uitlezen"
    assert not any(path.exists() for path in seen), "en worden daarna opgeruimd"
    # one recipe out of it: a single editor form, with the ingredients pre-filled
    assert editor.text.count('action="/recipes/save"') == 1
    assert "400 g spaghetti" in editor.text
    assert "Kook de spaghetti." in editor.text


def test_bulk_ocr_reviews_each_photo_as_its_own_recipe(tmp_path, monkeypatch):
    """A pile of cards: one photo per recipe, checked one at a time."""
    import groceries_assistant.web.app as app_module

    fake, _seen, _alive = _fake_ocr()
    monkeypatch.setattr(app_module, "extract_from_images", fake)

    with make_client(tmp_path) as client:
        added = client.post(
            "/recipes/ocr/add",
            files=[
                ("photos", ("kaart1.png", _png_bytes(), "image/png")),
                ("photos", ("kaart2.png", _png_bytes(), "image/png")),
                ("photos", ("notities.gif", b"GIF89a", "image/gif")),
            ],
            follow_redirects=False,
        )
        assert added.headers["location"] == "/recipes/ocr?toegevoegd=2&genegeerd=1"

        queue = client.get("/recipes/photos")  # the other queue, must stay empty
        assert "In de rij" not in queue.text
        page = client.get("/recipes/ocr")
        uids = re.findall(r'href="/recipes/ocr/([0-9a-f]{32})"', page.text)
        assert len(uids) == 2
        assert "In de rij (2)" in page.text

        # the first card opens in the editor, pre-filled and named after its own photo
        first = client.get(f"/recipes/ocr/{uids[0]}")
        assert 'name="ocr_stage" value="' + uids[0] + '"' in first.text
        assert f'value="Recept {uids[0]}"' in first.text
        assert "Recept 1 van 2" in first.text
        assert f'href="/recipes/ocr/{uids[1]}"' in first.text  # 'volgende overslaan'
        assert "/recipes/ocr/staged/" in first.text  # the card is shown
        assert 'name="card_photo" checked' in first.text

        # saving it creates one recipe, keeps the card as its photo, and retires it
        saved = client.post(
            "/recipes/save",
            data={
                "name": "Lasagne",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook de spaghetti.",
                "ocr_stage": uids[0],
                "card_photo": "on",
            },
            follow_redirects=False,
        )
        assert saved.headers["location"] == "/recipes/ocr?opgeslagen=1"
        assert (tmp_path / "recipes" / "lasagne.yaml").exists()
        assert (tmp_path / "recipes" / "img" / "lasagne.png").exists()
        after = client.get(saved.headers["location"]).text
        assert "Recept opgeslagen" in after

        # the second card is still waiting
        left = client.get("/recipes/ocr").text
        assert f'href="/recipes/ocr/{uids[0]}"' not in left
        assert f'href="/recipes/ocr/{uids[1]}"' in left

        # discard it and the queue is empty
        client.post("/recipes/ocr/discard", data={"uid": uids[1]}, follow_redirects=False)
        assert "Nog geen foto's in de rij" in client.get("/recipes/ocr").text


def test_bulk_ocr_card_photo_is_optional(tmp_path, monkeypatch):
    """Unticking 'bewaren als receptfoto' saves the recipe without an image."""
    import groceries_assistant.web.app as app_module

    fake, _seen, _alive = _fake_ocr()
    monkeypatch.setattr(app_module, "extract_from_images", fake)

    with make_client(tmp_path) as client:
        client.post(
            "/recipes/ocr/add",
            files=[("photos", ("kaart.png", _png_bytes(), "image/png"))],
            follow_redirects=False,
        )
        uid = re.findall(r'href="/recipes/ocr/([0-9a-f]{32})"', client.get("/recipes/ocr").text)[0]
        client.post(
            "/recipes/save",
            data={
                "name": "Lasagne",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook de spaghetti.",
                "ocr_stage": uid,
            },
            follow_redirects=False,
        )
        assert (tmp_path / "recipes" / "lasagne.yaml").exists()
        assert not (tmp_path / "recipes" / "img" / "lasagne.png").exists()
        assert list((tmp_path / "ocr-staging").glob("*")) == []


def test_bulk_ocr_warns_when_nothing_was_recognised(tmp_path, monkeypatch):
    import groceries_assistant.web.app as app_module
    from groceries_assistant.recipes.textparser import parse_recipe_text

    monkeypatch.setattr(
        app_module,
        "extract_from_images",
        lambda paths, binary="tesseract", lang="nld": (parse_recipe_text(""), ""),
    )
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/ocr/add",
            files=[("photos", ("kaart.png", _png_bytes(), "image/png"))],
            follow_redirects=False,
        )
        uid = re.findall(r'href="/recipes/ocr/([0-9a-f]{32})"', client.get("/recipes/ocr").text)[0]
        page = client.get(f"/recipes/ocr/{uid}").text
        assert "Geen ingrediënten herkend" in page


def test_ocr_queue_rejects_unknown_or_foreign_ids(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get(f"/recipes/ocr/{'a' * 32}").status_code == 404
        assert client.get("/recipes/ocr/staged/../state.db").status_code == 404
        assert client.get("/recipes/ocr/staged/notastagedphoto.png").status_code == 404

        # saving without a real staged card is just a normal save
        saved = client.post(
            "/recipes/save",
            data={
                "name": "Pasta",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
                "ocr_stage": "a" * 32,
            },
            follow_redirects=False,
        )
        assert saved.headers["location"] == "/recipes"
        assert (tmp_path / "recipes" / "pasta.yaml").exists()


def test_verdict_and_swap_flow(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        # add a person
        assert (
            client.post(
                "/settings/people",
                data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                "/recipes/verdict",
                data={"slug": "pasta", "person": "kid1", "verdict": "love"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        page = client.get("/recipes")
        assert 'value="love" selected' in page.text


class _FakeImageProduct:
    """Search stub that also carries an image url, like the real AH API."""

    def __init__(self, webshop_id: int, title: str, image_url: str) -> None:
        self.webshop_id = webshop_id
        self.title = title
        self.image_url = image_url
        self.unit_size = "500 g"


class _FakeProduct:
    def __init__(self, webshop_id: int, title: str) -> None:
        self.webshop_id = webshop_id
        self.title = title


def test_recipe_edit_page_prefills(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        page = client.get("/recipes/pasta/edit")
        assert page.status_code == 200
        assert 'value="Pasta"' in page.text
        assert 'name="original_slug" value="pasta"' in page.text


def test_rename_recipe_updates_files_and_state(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
        )
        client.post(
            "/recipes/verdict",
            data={"slug": "pasta", "person": "kid1", "verdict": "love"},
        )
        # trigger a proposal so plan rows reference the old slug
        client.get("/plan")
        saved = client.post(
            "/recipes/save",
            data={
                "original_slug": "pasta",
                "name": "Pasta alla Norma",
                "servings": "4",
                "cook": "30",
                "ingredients": "500 g spaghetti",
                "steps": "Kook.",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert not (tmp_path / "recipes" / "pasta.yaml").exists()
        assert (tmp_path / "recipes" / "pasta-alla-norma.yaml").exists()

        # verdicts and plan references followed the rename
        from groceries_assistant.store.db import Store

        store = Store(tmp_path / "state.db")
        assert store.verdicts() == {"pasta-alla-norma": {"kid1": "love"}}
        rows = store.plan_entries(store.load_proposal()[0])
        assert all(row.recipe_slug == "pasta-alla-norma" for row in rows)
        store.close()


def test_rename_collision_rejected(tmp_path):
    seed_recipe(tmp_path)
    second = tmp_path / "recipes" / "curry.yaml"
    second.write_text("name: Curry\nservings: 4\ningredients:\n  - {product: rijst, amount: 1}\n")
    with make_client(tmp_path) as client:
        conflict = client.post(
            "/recipes/save",
            data={
                "original_slug": "pasta",
                "name": "Curry",
                "servings": "4",
                "ingredients": "500 g spaghetti",
                "steps": "Kook.",
            },
        )
        assert conflict.status_code == 400
        # both files untouched
        assert (tmp_path / "recipes" / "pasta.yaml").exists()
        assert second.exists()


def test_theme_toggle_sets_cookie_and_attribute(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        page = client.get("/plan")
        assert "<html lang=" in page.text  # no cookie: system preference decides
        flipped = client.post(
            "/theme", data={"back": "/plan", "to": "dark"}, follow_redirects=False
        )
        assert flipped.status_code == 303
        assert "theme=dark" in flipped.headers.get("set-cookie", "")
        page = client.get("/plan")
        assert 'data-theme="dark"' in page.text


def test_new_recipe_page_renders_blank_editor(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/recipes/new")
        assert page.status_code == 200
        assert 'value=""' in page.text.replace('value="" ', 'value=""')
        assert "original_slug" not in page.text or 'name="original_slug"' not in page.text


def test_recipe_view_page(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        page = client.get("/recipes/pasta")
        assert page.status_code == 200
        assert "Pasta" in page.text
        assert "500 g spaghetti" in page.text


def test_recipe_source_url_round_trip(tmp_path):
    """The optional source-URL field saves, prefills and shows up as a link."""
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        saved = client.post(
            "/recipes/save",
            data={
                "original_slug": "pasta",
                "name": "Pasta",
                "servings": "4",
                "prep": "0",
                "cook": "30",
                "prep_ahead": "0",
                "protein": "",
                "url": "https://example.com/pasta-met-tomaten",
                "ingredients": "500 g spaghetti\n1 stuk ui",
                "steps": "Kook de spaghetti.",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        yaml_text = (tmp_path / "recipes" / "pasta.yaml").read_text(encoding="utf-8")
        assert "url: https://example.com/pasta-met-tomaten" in yaml_text

        view = client.get("/recipes/pasta").text
        assert 'href="https://example.com/pasta-met-tomaten"' in view
        assert "Originele recept" in view

        editor = client.get("/recipes/pasta/edit").text
        assert 'value="https://example.com/pasta-met-tomaten"' in editor

    # clearing the field removes the link
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/save",
            data={
                "original_slug": "pasta",
                "name": "Pasta",
                "servings": "4",
                "prep": "0",
                "cook": "30",
                "prep_ahead": "0",
                "protein": "",
                "url": "",
                "ingredients": "500 g spaghetti",
                "steps": "Kook de spaghetti.",
            },
            follow_redirects=False,
        )
        assert "url:" not in (tmp_path / "recipes" / "pasta.yaml").read_text(encoding="utf-8")
        assert "Originele recept" not in client.get("/recipes/pasta").text


def test_plan_evening_sets_cook_and_absences(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
        )
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        client.get("/plan")  # create proposal
        from groceries_assistant.store.db import Store

        monday = Store(tmp_path / "state.db").load_proposal()[1]

        # adult cook who joins: saved
        response = client.post(
            "/plan/evening",
            data={"day": monday.isoformat(), "cook": "mama", "joined": ["kid1", "mama"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        store = Store(tmp_path / "state.db")
        override = store.week_override(monday)
        assert override is not None and override[0] == "mama"
        assert store.absences(monday) == set()

        # kid as cook: rejected, nothing written
        tuesday = monday.replace(day=monday.day + 1)
        rejected = client.post(
            "/plan/evening",
            data={"day": tuesday.isoformat(), "cook": "kid1", "joined": "kid1"},
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        assert "error=kok" in rejected.headers["location"]
        assert store.week_override(tuesday) is None

        # everyone absent -> all marked absent
        client.post("/plan/evening", data={"day": monday.isoformat(), "cook": "", "joined": ""})
        assert store.absences(monday) == {"kid1", "mama"}


def test_plan_page_cook_inference(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
        )
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        client.post(
            "/settings/people",
            data={"person_id": "papa", "name": "Papa", "role": "adult", "weight": "1"},
        )
        client.get("/plan")
        from groceries_assistant.store.db import Store

        s = Store(tmp_path / "state.db")
        monday = s.load_proposal()[1]
        page = client.get("/plan")
        # two joining adults, no cook set: '?' with both names
        assert "? (Mama of Papa)" in page.text
        # kid never appears as a cook option
        assert 'value="kid1"' not in page.text.split("<select")[1].split("</select>")[0]

        # only mama joins: auto cook shown as text, never preselected as a value
        client.post("/plan/evening", data={"day": monday.isoformat(), "cook": "", "joined": "mama"})
        page = client.get("/plan").text
        assert "auto: Mama" in page
        # the only select in Monday's evening form is the cook select
        kok_form = page.split('id="evening-1"')[1].split("</form>")[0]
        cook_select = kok_form.split("<select")[1].split("</select>")[0]
        assert 'value="mama" selected' not in cook_select


def test_plan_page_warns_when_no_adult_joins(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
        )
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        client.get("/plan")
        from groceries_assistant.store.db import Store

        monday = Store(tmp_path / "state.db").load_proposal()[1]
        client.post("/plan/evening", data={"day": monday.isoformat(), "cook": "", "joined": "kid1"})
        page = client.get("/plan")
        assert "geen kok" in page.text


def test_settings_template_adults_dropdown(tmp_path):
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "kid1", "name": "Kees", "role": "kid", "weight": "1"},
        )
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        page = client.get("/settings")
        assert 'value="mama"' in page.text
        assert "per avond bepalen" in page.text
        # kid is never offered as a cook in the template selects
        cook_selects = [
            fragment
            for fragment in page.text.split("<select")[1:]
            if 'name="cook_' in fragment.split(">")[0]
        ]
        assert len(cook_selects) == 7
        for select in cook_selects:
            assert 'value="kid1"' not in select.split("</select>")[0]


def test_save_rejects_bad_ingredient_line_with_feedback(tmp_path):
    with make_client(tmp_path) as client:
        page = client.post(
            "/recipes/save",
            data={
                "name": "Test",
                "servings": "4",
                "ingredients": "400 g spaghetti\nwat groentes",
                "steps": "Kook.",
            },
        )
        assert page.status_code == 200
        assert "Niet herkend" in page.text
        assert "wat groentes" in page.text
        assert "400 g spaghetti" in page.text  # input preserved


def test_steps_renumbered_and_stored(tmp_path):
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/save",
            data={
                "name": "Nummertest",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "1. Kook de pasta.\nRoer erdoor.\n2. Serveer.",
            },
            follow_redirects=False,
        )
        import yaml

        data = yaml.safe_load((tmp_path / "recipes" / "nummertest.yaml").read_text())
        assert data["steps"] == ["1. Kook de pasta.", "2. Roer erdoor.", "3. Serveer."]


def test_cook_view_inference_rules():
    from groceries_assistant.web.views import cook_view

    members = [
        {"id": "kid1", "name": "Kees", "role": "kid", "absent": False},
        {"id": "mama", "name": "Mama", "role": "adult", "absent": False},
        {"id": "papa", "name": "Papa", "role": "adult", "absent": False},
    ]

    # two joining adults, nothing stored: '?' with both candidates
    result = cook_view("", members, ["kid1", "mama", "papa"])
    assert result["effective_cook"] == ""
    assert result["no_cook_available"] is False
    assert result["cook_unset_label"] == "Mama of Papa"

    # exactly one joining adult: auto cook
    result = cook_view("", members, ["mama"])
    assert result["effective_cook"] == "mama"

    # stored cook who is absent falls through to the single remaining adult
    result = cook_view("papa", members, ["mama"])
    assert result["effective_cook"] == "mama"
    assert result["auto_cook"] == "Mama"  # inferred, display-only
    assert result["stored_valid"] is False

    # stored kid cook is never accepted
    result = cook_view("kid1", members, ["kid1", "mama", "papa"])
    assert result["effective_cook"] == ""

    # explicit stored cook stays selected (not turned into auto text)
    result = cook_view("mama", members, ["kid1", "mama", "papa"])
    assert result["effective_cook"] == "mama"
    assert result["stored_valid"] is True
    assert result["auto_cook"] is None

    # no joining adults: warning state
    result = cook_view("", members, ["kid1"])
    assert result["no_cook_available"] is True
    assert result["effective_cook"] == ""


def test_swap_with_default_option_is_noop_not_error(tmp_path):
    """Regression: '(behoud)' posts an empty value; FastAPI must not 422."""
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.get("/plan")
        response = client.post(
            "/plan/swap",
            data={"day": "2026-09-28", "choice": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        keep = client.post(
            "/plan/swap",
            data={"day": "2026-09-28", "choice": "__keep__"},
            follow_redirects=False,
        )
        assert keep.status_code == 303
        # real swap still works
        swapped = client.post(
            "/plan/swap",
            data={"day": "2026-09-28", "choice": "pasta"},
            follow_redirects=False,
        )
        assert swapped.status_code == 303
        page = client.get("/plan")
        assert page.text.count("Pasta") >= 1


def test_toch_koken_converts_leftovers_to_best_fit(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.get("/plan")
        from groceries_assistant.store.db import Store

        s = Store(tmp_path / "state.db")
        monday = s.load_proposal()[1]
        tuesday = monday.replace(day=monday.day + 1)
        # make Tuesday a leftovers evening
        client.post(
            "/plan/swap",
            data={"day": tuesday.isoformat(), "choice": "__leftovers__"},
            follow_redirects=False,
        )
        rows = {(r.day.isoformat()): r for r in s.plan_entries(s.load_proposal()[0])}
        assert rows[tuesday.isoformat()].kind == "leftovers"

        # 'toch koken' -> cook evening with the best-fitting dish (only pasta exists,
        # excluded as the source, so the fallback picks it)
        r = client.post(
            "/plan/swap",
            data={"day": tuesday.isoformat(), "choice": "__cook__"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        s2 = Store(tmp_path / "state.db")
        rows = {r.day.isoformat(): r for r in s2.plan_entries(s2.load_proposal()[0])}
        assert rows[tuesday.isoformat()].kind == "cook"
        assert rows[tuesday.isoformat()].recipe_slug == "pasta"
        page = client.get("/plan")
        assert "wissel" in page.text


def test_grid_checkboxes_bound_to_their_own_row(tmp_path):
    """Regression: person checkboxes must reference their own evening's form.

    A Jinja nested-loop shadowing bug made every row's Loy checkbox submit
    Monday's form, so checking attendance silently edited other evenings.
    """
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        for person_id, role in (("kid1", "kid"), ("mama", "adult"), ("papa", "adult")):
            client.post(
                "/settings/people",
                data={"person_id": person_id, "name": person_id, "role": role, "weight": "1"},
            )
        page = client.get("/plan").text
        # each evening id: exactly 3 checkbox refs + 1 opslaan button (3 people)
        for n in range(1, 8):
            refs = page.count(f'form="evening-{n}"')
            assert refs == 4, f"evening-{n} referenced {refs}x, expected 4"
        # the checkboxes in evening-5's own segment reference evening-5 (not another row)
        segment = page.split('id="evening-5"')[1].split('id="evening-6"')[0]
        first_checkbox = next(
            chunk for chunk in segment.split("<input") if 'type="checkbox"' in chunk
        )
        assert 'form="evening-5"' in first_checkbox


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_recipe_image_upload_and_view(tmp_path):
    with make_client(tmp_path) as client:
        saved = client.post(
            "/recipes/save",
            data={
                "name": "Fotorecept",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            files={"image": ("foto.png", _png_bytes(), "image/png")},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (tmp_path / "recipes" / "img" / "fotorecept.png").exists()

        page = client.get("/recipes/fotorecept")
        assert "/recipes/img/fotorecept.png" in page.text
        listing = client.get("/recipes")
        assert '<img class="thumb"' in listing.text

        # serving the image works
        served = client.get("/recipes/img/fotorecept.png")
        assert served.status_code == 200


def test_recipe_image_rejected_for_bad_extension(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/recipes/save",
            data={"name": "X", "servings": "4", "ingredients": "400 g spaghetti", "steps": "Kook."},
            files={"image": ("foto.gif", b"GIF89a", "image/gif")},
        )
        assert response.status_code == 400


def test_recipe_image_moves_on_rename(tmp_path):
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/save",
            data={
                "name": "Oud",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            files={"image": ("foto.png", _png_bytes(), "image/png")},
            follow_redirects=False,
        )
        client.post(
            "/recipes/save",
            data={
                "original_slug": "oud",
                "name": "Nieuwe naam",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            follow_redirects=False,
        )
        assert not (tmp_path / "recipes" / "img" / "oud.png").exists()
        assert (tmp_path / "recipes" / "img" / "nieuwe-naam.png").exists()


def test_evening_minutes_adjustable_from_plan_page(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        client.get("/plan")
        from groceries_assistant.store.db import Store

        monday = Store(tmp_path / "state.db").load_proposal()[1]
        client.post(
            "/plan/evening",
            data={"day": monday.isoformat(), "cook": "", "joined": "", "minutes": "60"},
            follow_redirects=False,
        )
        store = Store(tmp_path / "state.db")
        override = store.week_override(monday)
        assert override is not None and override[1] == 60
        page = client.get("/plan").text
        kok_form = page.split('id="evening-1"')[1].split("</form>")[0]
        assert 'name="minutes" value="60"' in kok_form


def test_plan_page_shows_recipe_thumbnail(tmp_path):
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/save",
            data={
                "name": "Fotopasta",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            files={"image": ("foto.png", _png_bytes(), "image/png")},
            follow_redirects=False,
        )
        client.get("/plan")
        page = client.get("/plan").text
        assert '<img class="thumb" src="/recipes/img/fotopasta.png"' in page


def test_image_picker_multiple_photos_and_featured(tmp_path):
    with make_client(tmp_path) as client:
        client.post(
            "/recipes/save",
            data={
                "name": "Galerij",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            files=[
                ("image", ("a.png", _png_bytes(), "image/png")),
                ("image", ("b.png", _png_bytes(), "image/png")),
                ("image", ("c.png", _png_bytes(), "image/png")),
            ],
            follow_redirects=False,
        )
        img_dir = tmp_path / "recipes" / "img"
        assert (img_dir / "galerij.png").exists()  # featured
        assert (img_dir / "galerij--2.png").exists()
        assert (img_dir / "galerij--3.png").exists()

        editor = client.get("/recipes/galerij/edit")
        assert editor.text.count("img-card") >= 3

        # pick an extra as featured: names swap
        client.post(
            "/recipes/img/select",
            data={"slug": "galerij", "filename": "galerij--2.png"},
            follow_redirects=False,
        )
        assert (img_dir / "galerij--2.png").exists()  # old featured moved aside
        featured = client.get("/recipes/img/galerij.png")
        assert featured.status_code == 200

        # delete one extra
        client.post(
            "/recipes/img/delete",
            data={"slug": "galerij", "filename": "galerij--3.png"},
            follow_redirects=False,
        )
        assert not (img_dir / "galerij--3.png").exists()

        # foreign filenames are rejected
        rogue = client.post(
            "/recipes/img/delete",
            data={"slug": "galerij", "filename": "anderrecept.png"},
        )
        assert rogue.status_code == 400


def _save_recipe(client, name: str) -> None:
    client.post(
        "/recipes/save",
        data={"name": name, "servings": "4", "ingredients": "400 g spaghetti", "steps": "Kook."},
        follow_redirects=False,
    )


def _staged_names(page_text: str) -> list[str]:
    return re.findall(r'name="photo_([0-9a-f]{32})"', page_text)


def test_editor_offers_camera_and_drop_zone(tmp_path):
    with make_client(tmp_path) as client:
        _save_recipe(client, "Fotorecept")
        client.post(
            "/recipes/save",
            data={
                "original_slug": "fotorecept",
                "name": "Fotorecept",
                "servings": "4",
                "ingredients": "400 g spaghetti",
                "steps": "Kook.",
            },
            files=[
                ("image", ("a.png", _png_bytes(), "image/png")),
                ("image", ("b.png", _png_bytes(), "image/png")),
            ],
            follow_redirects=False,
        )
        page = client.get("/recipes/fotorecept/edit").text

        # drag & drop zone plus the camera button, both feeding the one file input
        assert 'class="photo-drop"' in page
        assert 'class="drop-hint"' in page
        assert 'class="camera-button"' in page
        assert 'class="camera-input" accept="image/*" capture="environment"' in page
        assert '<input type="file" name="image" accept="image/*" multiple>' in page
        assert "/static/photos.js" in page

        # the gallery's pick/delete controls live outside the save form: a nested
        # <form> is dropped by the browser, which would post to /recipes/save
        assert 'form="img-featured"' in page
        assert 'form="img-delete"' in page
        assert page.index('id="img-featured"') > page.index("</form>")
        assert page.index('id="img-delete"') > page.index("</form>")


def test_bulk_photo_import_assigns_photos_to_recipes(tmp_path):
    with make_client(tmp_path) as client:
        _save_recipe(client, "Pasta")
        _save_recipe(client, "Curry")
        added = client.post(
            "/recipes/photos/add",
            files=[
                ("photos", ("a.png", _png_bytes(), "image/png")),
                ("photos", ("b.png", _png_bytes(), "image/png")),
                ("photos", ("c.png", _png_bytes(), "image/png")),
            ],
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert added.headers["location"] == "/recipes/photos?toegevoegd=3"

        page = client.get("/recipes/photos").text
        uids = _staged_names(page)
        assert len(uids) == 3
        # each staged photo is offered as a thumbnail with a recipe to pick
        for uid in uids:
            assert f"/recipes/photos/staged/{uid}.png" in page
        assert '<option value="pasta">Pasta</option>' in page
        assert '<option value="curry">Curry</option>' in page

        assigned = client.post(
            "/recipes/photos/assign",
            data={f"photo_{uids[0]}": "pasta", f"photo_{uids[1]}": "curry"},
            follow_redirects=False,
        )
        assert assigned.headers["location"] == "/recipes/photos?toegewezen=2"
        img_dir = tmp_path / "recipes" / "img"
        assert (img_dir / "pasta.png").exists()
        assert (img_dir / "curry.png").exists()

        # the unassigned photo stays staged, the assigned ones are gone
        left = _staged_names(client.get("/recipes/photos").text)
        assert left == [uids[2]]
        assert client.get(f"/recipes/photos/staged/{left[0]}.png").status_code == 200

        # a recipe that already has a photo gets the next one as an extra
        client.post(
            "/recipes/photos/assign",
            data={f"photo_{left[0]}": "pasta"},
            follow_redirects=False,
        )
        assert (img_dir / "pasta--2.png").exists()
        assert list((tmp_path / "photo-staging").glob("*")) == []


def test_bulk_photo_import_skips_unsupported_and_can_discard(tmp_path):
    with make_client(tmp_path) as client:
        _save_recipe(client, "Pasta")
        added = client.post(
            "/recipes/photos/add",
            files=[
                ("photos", ("a.png", _png_bytes(), "image/png")),
                ("photos", ("b.gif", b"GIF89a", "image/gif")),
            ],
            follow_redirects=False,
        )
        assert added.headers["location"] == "/recipes/photos?toegevoegd=1&genegeerd=1"

        uid = _staged_names(client.get("/recipes/photos").text)[0]
        # discard it, then stage another and wipe everything
        client.post("/recipes/photos/assign", data={f"drop_{uid}": "on"}, follow_redirects=False)
        assert _staged_names(client.get("/recipes/photos").text) == []

        client.post(
            "/recipes/photos/add",
            files=[("photos", ("a.png", _png_bytes(), "image/png"))],
            follow_redirects=False,
        )
        client.post("/recipes/photos/assign", data={"clear": "1"}, follow_redirects=False)
        assert "Nog geen foto's gekozen" in client.get("/recipes/photos").text


def test_staged_photo_route_rejects_foreign_names(tmp_path):
    with make_client(tmp_path) as client:
        for name in ("../state.db", "notastagedphoto.png", "a" * 32 + ".png"):
            assert client.get(f"/recipes/photos/staged/{name}").status_code == 404


def test_recipes_page_links_to_bulk_photo_import(tmp_path):
    seed_recipe(tmp_path)
    with make_client(tmp_path) as client:
        assert 'href="/recipes/photos"' in client.get("/recipes").text
        assert client.get("/recipes/photos").status_code == 200


def test_order_match_pin_unpin(tmp_path):
    class FakeProduct:
        def __init__(self, webshop_id, title, unit_size="500 g"):
            self.webshop_id = webshop_id
            self.title = title
            self.unit_size = unit_size

    def search(query):
        return [FakeProduct(111, "AH Spaghetti 500 g"), FakeProduct(222, "AH Penne 500 g")]

    with make_client(tmp_path, search_fn=search) as client:
        seed_recipe(tmp_path)
        client.get("/plan")
        client.post("/plan/approve", follow_redirects=False)

        page = client.get("/order/match")
        assert page.status_code == 200
        assert "AH Spaghetti 500 g" in page.text  # suggestion for spaghetti
        assert "/producten/product/wi111" in page.text  # article link
        assert 'action="/order/match/pin"' in page.text

        # pin spaghetti to webshop 111
        client.post(
            "/order/match/pin",
            data={
                "product": "spaghetti",
                "unit": "g",
                "webshop_id": "111",
                "title": "AH Spaghetti 500 g",
            },
            follow_redirects=False,
        )
        from groceries_assistant.store.db import Store

        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        items = {(i["product"], i["unit"]): i for i in store.order_items(plan_id)}
        assert items[("spaghetti", "g")]["webshop_id"] == 111

        # pin persisted in the mapping store as confirmed
        import yaml

        saved = yaml.safe_load((tmp_path / "ah_mappings.yaml").read_text())
        assert saved["mappings"]["spaghetti"]["confirmed"] is True

        # matched row now links to the pinned article
        matched = client.get("/order/match")
        assert "/producten/product/wi111" in matched.text
        link_page = client.get("/order/link")
        assert link_page.status_code == 200
        assert "111%3A1" in link_page.text

        # unpin clears both
        client.post(
            "/order/match/unpin",
            data={"product": "spaghetti", "unit": "g"},
            follow_redirects=False,
        )
        items = {(i["product"], i["unit"]): i for i in store.order_items(plan_id)}
        assert items[("spaghetti", "g")]["webshop_id"] is None
        assert not (tmp_path / "ah_mappings.yaml").read_text().count("spaghetti")


def test_dish_picker_shows_images_and_combines_current_dish(tmp_path):
    """The dish cell is one control: summary = current dish (thumb + name),
    body = alternatives with thumbs (regression for the two picker TODOs)."""
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        (tmp_path / "recipes" / "pasta.yaml").write_text(
            "name: Pasta\nservings: 4\ntime: {cook: 20}\n"
            "ingredients:\n  - {product: spaghetti, amount: 500, unit: g}\n",
            encoding="utf-8",
        )
        (tmp_path / "recipes" / "curry.yaml").write_text(
            "name: Curry\nservings: 4\ntime: {cook: 30}\n"
            "ingredients:\n  - {product: rijst, amount: 400, unit: g}\n",
            encoding="utf-8",
        )
        (tmp_path / "recipes" / "img").mkdir()
        (tmp_path / "recipes" / "img" / "pasta.png").write_bytes(_png_bytes())

        page = client.get("/plan").text
        assert '<details class="dish-picker">' in page
        # Monday (25 min) picks pasta; the summary IS the selected dish
        summary = page.split("<summary")[1].split("</summary>")[0]
        assert "Pasta" in summary
        assert 'src="/recipes/img/pasta.png"' in summary
        # Tuesday (45 min) is a curry evening: its picker lists pasta (with
        # thumb) as an alternative option row
        tuesday_options = page.split("dish-options")[2].split("</details>")[0]
        assert "Pasta" in tuesday_options
        assert 'src="/recipes/img/pasta.png"' in tuesday_options
        assert 'name="choice"' in tuesday_options
        assert "laten staan" in tuesday_options
        # wissel select is gone; only the 7 cook selects remain
        assert page.count("<select") == 7


def test_order_match_custom_entry(tmp_path):
    """The koppel page allows pinning a manually looked-up AH id."""
    with make_client(tmp_path, search_fn=lambda q: []) as client:
        seed_recipe(tmp_path)
        client.get("/plan")
        client.post("/plan/approve", follow_redirects=False)

        page = client.get("/order/match")
        assert 'action="/order/match/pin"' in page.text
        assert 'name="webshop_id"' in page.text  # custom entry fields

        client.post(
            "/order/match/pin",
            data={
                "product": "spaghetti",
                "unit": "g",
                "webshop_id": "999",
                "title": "Spaghetti uit de AH-app",
            },
            follow_redirects=False,
        )
        from groceries_assistant.store.db import Store

        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        items = {(i["product"], i["unit"]): i for i in store.order_items(plan_id)}
        assert items[("spaghetti", "g")]["webshop_id"] == 999
        # the custom pin flows through to the deep-link
        link_page = client.get("/order/link")
        assert "999%3A1" in link_page.text


def test_no_cook_evening(tmp_path):
    """'Niet koken' (eating elsewhere) drops the dish and its groceries."""
    seed_recipe(tmp_path)
    (tmp_path / "recipes" / "soep.yaml").write_text(
        "name: Soep\nservings: 4\ntime: {cook: 10}\n"
        "ingredients:\n  - {product: ui, amount: 2, unit: stuk}\n",
        encoding="utf-8",
    )
    with make_client(tmp_path) as client:
        client.post(
            "/settings/people",
            data={"person_id": "mama", "name": "Mama", "role": "adult", "weight": "1"},
        )
        client.get("/plan")  # create proposal
        from groceries_assistant.store.db import Store

        store = Store(tmp_path / "state.db")
        plan_id, start, _rows = store.load_proposal()
        monday = start

        # every evening offers the option
        page = client.get("/plan").text
        assert page.count('value="__no_cook__"') == 7

        swapped = client.post(
            "/plan/swap",
            data={"day": monday.isoformat(), "choice": "__no_cook__"},
            follow_redirects=False,
        )
        assert swapped.status_code == 303

        rows = {row.day: row for row in store.plan_entries(plan_id)}
        assert rows[monday].kind == "no_cook"
        assert rows[monday].recipe_slug is None

        # the evening shows as niet-koken, without cook/attendance controls
        page = client.get("/plan").text
        assert "Niet koken (we eten elders)" in page
        assert "Geen kok nodig" in page  # kok cell replaced
        assert "Niet van toepassing" in page  # attendance cells replaced
        # its picker offers fitting dishes to switch back to cooking
        # (Monday only has 25 minutes, so the 10-minute soup fits, pasta does not)
        options = page.split('class="dish-options"')[1].split("</details>")[0]
        assert 'value="soep"' in options
        assert 'value="pasta"' not in options
        # ... and no 'niet koken' option on that row itself
        assert "__no_cook__" not in options

        # switching back to cooking works and the dish returns
        client.post(
            "/plan/swap",
            data={"day": monday.isoformat(), "choice": "pasta"},
            follow_redirects=False,
        )
        rows = {row.day: row for row in store.plan_entries(plan_id)}
        assert rows[monday].kind == "cook"
        assert rows[monday].recipe_slug == "pasta"

        # mark it again and approve: no groceries for that evening
        client.post(
            "/plan/swap",
            data={"day": monday.isoformat(), "choice": "__no_cook__"},
            follow_redirects=False,
        )
        assert client.post("/plan/approve", follow_redirects=False).status_code == 303
        items = {i["product"] for i in store.order_items(plan_id)}
        assert items == {"spaghetti", "ui"}
        assert store.plan_entries(plan_id)[0].kind == "no_cook"


def test_koppel_page_shows_ah_thumbnails(tmp_path):
    """Suggestions and pinned items show the AH product image (hotlinked)."""
    image = "https://static.ah.nl/dam/product/AHI_4354523130303637?rendition=800x800_WEBP"
    with make_client(
        tmp_path,
        search_fn=lambda q: [_FakeImageProduct(111, "AH Pasta", image)] if q == "spaghetti" else [],
    ) as client:
        seed_recipe(tmp_path)
        client.get("/plan")
        client.post("/plan/approve", follow_redirects=False)

        page = client.get("/order/match")
        assert f'src="{image}"' in page.text
        assert 'loading="lazy"' in page.text

        pinned = client.post(
            "/order/match/pin",
            data={
                "product": "spaghetti",
                "unit": "g",
                "webshop_id": "111",
                "title": "AH Pasta",
                "image": image,
            },
            follow_redirects=False,
        )
        assert pinned.status_code == 303

        # the pin remembers the image so the 'gekoppeld' row keeps a thumbnail
        assert image in (tmp_path / "ah_mappings.yaml").read_text(encoding="utf-8")
        matched = client.get("/order/match").text
        assert f'src="{image}"' in matched
        assert "AH Pasta" in matched  # title instead of the raw webshop-id


def test_order_match_custom_entry_accepts_pasted_url(tmp_path):
    """Pasting an ah.nl product link in the custom field pins the right article."""
    with make_client(tmp_path, search_fn=lambda q: []) as client:
        seed_recipe(tmp_path)
        client.get("/plan")
        client.post("/plan/approve", follow_redirects=False)

        # the field is free text now, so a full URL fits
        assert "AH-id of product-URL" in client.get("/order/match").text

        client.post(
            "/order/match/pin",
            data={
                "product": "spaghetti",
                "unit": "g",
                "webshop_id": "https://www.ah.nl/producten/product/wi58169/ah-volkoren-spaghetti",
                "title": "",
            },
            follow_redirects=False,
        )
        from groceries_assistant.store.db import Store

        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        items = {(i["product"], i["unit"]): i for i in store.order_items(plan_id)}
        assert items[("spaghetti", "g")]["webshop_id"] == 58169
        # the slug filled in the title, and the deep-link points at the right article
        assert "volkoren spaghetti" in client.get("/order/match").text
        assert "58169%3A1" in client.get("/order/link").text

        # an unparseable reference is rejected with a hint
        rejected = client.post(
            "/order/match/pin",
            data={"product": "ui", "unit": "stuk", "webshop_id": "geen id", "title": "x"},
            follow_redirects=False,
        )
        assert rejected.headers["location"] == "/order/match?error=id"
        items = {(i["product"], i["unit"]): i for i in store.order_items(plan_id)}
        assert items[("ui", "stuk")]["webshop_id"] is None


# --- composed dishes (/dishes) -------------------------------------------------

AARDAPPEL = """\
name: Aardappels uit de oven
servings: 4
time: {cook: 30}
ingredients:
  - {product: aardappels, amount: 1000, unit: g}
"""

SPERZIE = """\
name: Sperziebonen
servings: 4
time: {cook: 10}
ingredients:
  - {product: sperziebonen, amount: 300, unit: g}
"""

KIP = """\
name: Kipdoner
servings: 4
time: {cook: 20}
ingredients:
  - {product: kip, amount: 600, unit: g}
"""


def seed_part_recipes(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    (recipes / "aardappels-uit-de-oven.yaml").write_text(AARDAPPEL, encoding="utf-8")
    (recipes / "sperziebonen.yaml").write_text(SPERZIE, encoding="utf-8")
    (recipes / "kip-doner.yaml").write_text(KIP, encoding="utf-8")


def test_dishes_page_renders_empty(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/dishes")
        assert page.status_code == 200
        assert "Nog geen gerechten" in page.text


def test_creating_a_dish_writes_the_file_and_lists_it(tmp_path):
    seed_part_recipes(tmp_path)
    with make_client(tmp_path) as client:
        created = client.post(
            "/dishes",
            data={
                "name": "Aardappel-Groente-Vlees",
                "parts": "Groente: sperziebonen\nVlees: kip-doner",
            },
            follow_redirects=False,
        )

        assert created.status_code == 303
        assert created.headers["location"] == "/dishes"
        path = tmp_path / "dishes" / "aardappel-groente-vlees.yaml"
        assert path.is_file()
        assert "sperziebonen" in path.read_text(encoding="utf-8")

        page = client.get("/dishes")
        assert "Aardappel-Groente-Vlees" in page.text
        # the part options are shown as recipe names, not slugs
        assert "Groente: Sperziebonen" in page.text


def test_a_line_without_a_colon_is_rejected_with_its_number(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/dishes", data={"name": "Fout", "parts": "Groente: sperziebonen\ngeen punt"}
        )

        assert response.status_code == 200
        assert "regel 2" in response.text
        assert not (tmp_path / "dishes" / "fout.yaml").exists()
        # the form keeps what you typed
        assert "Groente: sperziebonen" in response.text


def test_a_duplicate_dish_name_is_rejected(tmp_path):
    with make_client(tmp_path) as client:
        data = {"name": "Samen", "parts": "Groente: sperziebonen"}
        client.post("/dishes", data=data)

        response = client.post("/dishes", data=data)

        assert "bestaat al" in response.text


def test_a_dish_needs_at_least_one_part(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post("/dishes", data={"name": "Leeg", "parts": "  \n"})
        assert "minstens één onderdeel" in response.text


def test_editing_a_dish_keeps_its_slug(tmp_path):
    seed_part_recipes(tmp_path)
    with make_client(tmp_path) as client:
        client.post(
            "/dishes",
            data={"name": "Aardappel-groente-vlees", "parts": "Groente: sperziebonen"},
        )

        page = client.get("/dishes/aardappel-groente-vlees")
        assert page.status_code == 200
        assert "Groente: sperziebonen" in page.text

        saved = client.post(
            "/dishes/aardappel-groente-vlees",
            data={
                "name": "Aardappels, groente en vlees",
                "parts": "Groente: sperziebonen\nVlees: kip-doner",
            },
            follow_redirects=False,
        )

        assert saved.status_code == 303
        # still the same file: plans keep pointing at the slug
        assert not (tmp_path / "dishes" / "aardappels-groente-en-vlees.yaml").exists()
        page = client.get("/dishes")
        assert "Aardappels, groente en vlees" in page.text
        assert "Kipdoner" in page.text


def test_editing_an_unknown_dish_is_a_404(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/dishes/weg").status_code == 404
        assert (
            client.post(
                "/dishes/weg", data={"name": "x", "parts": "A: b"}, follow_redirects=False
            ).status_code
            == 404
        )


def test_deleting_a_dish(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/dishes", data={"name": "Samen", "parts": "Groente: sperziebonen"})

        deleted = client.post("/dishes/samen/delete", follow_redirects=False)

        assert deleted.status_code == 303
        assert not (tmp_path / "dishes" / "samen.yaml").exists()
        assert "Nog geen gerechten" in client.get("/dishes").text
        assert client.post("/dishes/samen/delete", follow_redirects=False).status_code == 404


def test_unknown_recipe_slugs_are_warned_about_not_rejected(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/dishes", data={"name": "Samen", "parts": "Groente: sperziebonen, weg-recept"})

        page = client.get("/dishes")
        assert "Onbekende recepten" in page.text
        assert "weg-recept" in page.text


def test_a_broken_dish_file_does_not_hide_the_page(tmp_path):
    seed_part_recipes(tmp_path)
    dishes = tmp_path / "dishes"
    dishes.mkdir(parents=True, exist_ok=True)
    (dishes / "kapot.yaml").write_text("name: [kapot\n", encoding="utf-8")

    with make_client(tmp_path) as client:
        page = client.get("/dishes")
        assert page.status_code == 200
        assert "Ongeldige gerechtbestanden" in page.text


# --- composed dishes in the week view ------------------------------------------


def seed_dish(tmp_path: Path, name: str = "Aardappel-groente-vlees", parts: str = "") -> None:
    """Write a dish definition next to the seeded part recipes."""
    default = "Aardappels: aardappels-uit-de-oven\nGroente: sperziebonen\nVlees: kip-doner"
    rows = [
        {
            "key": slugify(label),
            "label": label.strip(),
            "options": [s.strip() for s in opts.split(",")],
        }
        for label, opts in (
            line.split(":", 1) for line in (parts or default).splitlines() if line.strip()
        )
    ]
    directory = tmp_path / "dishes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{slugify(name)}.yaml").write_text(
        yaml.safe_dump({"slug": slugify(name), "name": name, "parts": rows}, allow_unicode=True),
        encoding="utf-8",
    )


def _proposal(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    return store, store.load_proposal()[0]


def _open_week(client, tmp_path: Path):
    """The proposal is created on the first /plan visit."""
    client.get("/plan")
    return _proposal(tmp_path)


def plan_a_dish(
    client, tmp_path: Path, index: int = 0, dish: str = "aardappel-groente-vlees"
) -> date:
    """Swap one evening of the proposal onto a dish; returns that day."""
    store, plan_id = _open_week(client, tmp_path)
    day = store.plan_entries(plan_id)[index].day
    client.post("/plan/swap", data={"day": day.isoformat(), "choice": dish}, follow_redirects=False)
    return day


def pick(client, day: date, **parts: str):
    return client.post(
        "/plan/picks",
        data={"day": day.isoformat(), **{f"part_{key}": slug for key, slug in parts.items()}},
        follow_redirects=False,
    )


def test_a_dish_can_be_planned_and_its_parts_chosen(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)

        page = client.get("/plan")
        assert "Aardappel-groente-vlees" in page.text
        assert "nog te kiezen" in page.text
        for key in ("aardappels", "groente", "vlees"):
            assert f'name="part_{key}"' in page.text

        assert (
            pick(
                client,
                day,
                aardappels="aardappels-uit-de-oven",
                groente="sperziebonen",
                vlees="kip-doner",
            ).status_code
            == 303
        )

        # the row shows the parts joined with ' + ', under the dish name
        assert "Aardappels uit de oven + Sperziebonen + Kipdoner" in client.get("/plan").text


def test_picking_partly_shows_only_the_chosen_parts(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, aardappels="aardappels-uit-de-oven", groente="")

        text = client.get("/plan").text
        assert "Aardappels uit de oven" in text
        # the row names what is still missing
        assert "nog te kiezen: Groente, Vlees" in text


def test_clearing_a_part_removes_it_from_the_row(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(
            client,
            day,
            aardappels="aardappels-uit-de-oven",
            groente="sperziebonen",
            vlees="kip-doner",
        )
        assert (
            pick(
                client, day, aardappels="aardappels-uit-de-oven", groente="sperziebonen"
            ).status_code
            == 303
        )

        store, plan_id = _proposal(tmp_path)
        assert store.plan_picks(plan_id)[day] == {
            "aardappels": "aardappels-uit-de-oven",
            "groente": "sperziebonen",
        }


def test_an_option_that_is_not_on_offer_is_ignored(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)

        pick(client, day, groente="kip-doner")  # a real recipe, but not a part option

        store, plan_id = _proposal(tmp_path)
        assert store.plan_picks(plan_id).get(day, {}) == {}


def test_picks_on_a_plain_recipe_evening_are_refused(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        store, plan_id = _open_week(client, tmp_path)
        day = store.plan_entries(plan_id)[0].day

        assert pick(client, day, groente="sperziebonen").status_code == 303

        assert store.plan_picks(plan_id).get(day, {}) == {}


def test_swapping_the_evening_to_another_dish_drops_the_picks(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    seed_dish(tmp_path, "Alleen groente", parts="Groente: sperziebonen")
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, aardappels="aardappels-uit-de-oven", groente="sperziebonen")

        client.post(
            "/plan/swap",
            data={"day": day.isoformat(), "choice": "alleen-groente"},
            follow_redirects=False,
        )

        store, plan_id = _proposal(tmp_path)
        assert store.plan_picks(plan_id).get(day, {}) == {}  # 'aardappels' is not a part now
        assert "Alleen groente" in client.get("/plan").text


def test_swapping_the_evening_to_a_plain_recipe_drops_the_picks(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, groente="sperziebonen")

        client.post(
            "/plan/swap",
            data={"day": day.isoformat(), "choice": "kip-doner"},
            follow_redirects=False,
        )

        store, plan_id = _proposal(tmp_path)
        assert store.plan_picks(plan_id).get(day, {}) == {}


def test_a_leftovers_evening_of_a_dish_copies_its_picks(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, aardappels="aardappels-uit-de-oven", groente="sperziebonen")
        following = day + timedelta(days=1)

        client.post(
            "/plan/swap",
            data={"day": following.isoformat(), "choice": "__leftovers__"},
            follow_redirects=False,
        )

        store, plan_id = _proposal(tmp_path)
        picks = store.plan_picks(plan_id)
        assert picks[following] == picks[day]
        text = client.get("/plan").text
        assert "Restjes (Aardappel-groente-vlees)" in text
        assert "Aardappels uit de oven + Sperziebonen" in text


def test_approving_a_week_keeps_the_picks(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, groente="sperziebonen")

        assert client.post("/plan/approve", follow_redirects=False).status_code == 303

        store = Store(tmp_path / "state.db")
        assert store.plan_picks(store.latest_plan()[0])[day]["groente"] == "sperziebonen"


def test_the_week_row_shows_the_longest_part_and_the_total(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(
            client,
            day,
            aardappels="aardappels-uit-de-oven",  # 30 min
            groente="sperziebonen",  # 10 min
            vlees="kip-doner",  # 20 min
        )
        # 60 minutes of parts in a 45-minute evening
        client.post(
            "/plan/evening", data={"day": day.isoformat(), "minutes": 45}, follow_redirects=False
        )

        text = client.get("/plan").text
        assert "30/45 min" in text  # the longest part decides the evening
        assert "60 min samen" in text  # the sum is shown as a note


def test_a_dish_appears_in_the_picker_with_its_part_labels(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        text = client.get("/plan").text
        assert "Aardappel-groente-vlees" in text
        assert "(Aardappels + Groente + Vlees)" in text


def test_an_unfilled_dish_buys_only_the_filled_parts_and_warns(tmp_path):
    seed_part_recipes(tmp_path)
    seed_dish(tmp_path)
    with make_client(tmp_path) as client:
        day = plan_a_dish(client, tmp_path)
        pick(client, day, groente="sperziebonen")

        client.post("/plan/approve", follow_redirects=False)

        store = Store(tmp_path / "state.db")
        items = {i["product"] for i in store.order_items(store.latest_plan()[0])}
        assert "sperziebonen" in items
        # the order says what is missing, so the list is not silently short
        assert "zonder Aardappels en Vlees" in client.get("/order").text
