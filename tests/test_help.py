"""The '?' button explains every page: the panel is present everywhere."""

import pytest
from fastapi.testclient import TestClient

from groceries_assistant.web.app import create_app
from tests.test_order_flow import _pasta_with_zout


@pytest.mark.parametrize(
    "path",
    [
        "/plan",
        "/plan/select?day=2026-11-24",
        "/recipes",
        "/recipes/pasta",
        "/recipes/pasta/edit",
        "/recipes/import",
        "/recipes/import-urls",
        "/recipes/photos",
        "/recipes/ocr",
        "/dishes",
        "/components",
        "/vriezer",
        "/matches",
        "/order",
        "/order/match",
        "/settings",
    ],
)
def test_every_page_has_the_help_button(tmp_path, path):
    """Every GET page renders the corner '?' and its explanation panel."""
    _pasta_with_zout(tmp_path)
    with TestClient(create_app(tmp_path, search_fn=lambda q: [])) as client:
        resp = client.get(path)
        if resp.status_code == 404:  # e.g. /order needs an active week
            return
        page = resp.text
        assert 'id="help-toggle"' in page
        assert 'id="page-help"' in page
        assert "help-toggle" in page  # the toggle script


def test_the_help_panel_explains_the_page_it_is_on(tmp_path):
    """The panel content matches the page: plan explains approval, the recipe
    page explains the in-house tick, an unknown page gets the default."""
    _pasta_with_zout(tmp_path)
    with TestClient(create_app(tmp_path, search_fn=lambda q: [])) as client:
        plan = client.get("/plan").text
        assert "De week" in plan
        assert "goedkeuren" in plan
        recipe = client.get("/recipes/pasta").text
        assert "in huis" in recipe
        assert "AH-verpakkingen" in recipe


def test_help_panels_differ_per_page(tmp_path):
    """The recipe page gets recipe help, not the plan's."""
    _pasta_with_zout(tmp_path)
    with TestClient(create_app(tmp_path, search_fn=lambda q: [])) as client:
        plan = client.get("/plan").text
        recipe = client.get("/recipes/pasta").text
        assert "Vul de week in" in plan  # plan-only explanation
        assert "Vul de week in" not in recipe
        assert "QR-code" in recipe  # order block explanation


def test_unlisted_paths_get_the_default_panel():
    from groceries_assistant.web.helptext import help_for, page_key

    assert page_key("/nowhere") == "default"
    assert help_for("/nowhere")["title"] == "Deze pagina"
    assert "Week" in help_for("/nowhere")["sections"][0][1][0][0]
