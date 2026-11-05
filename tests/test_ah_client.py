from datetime import date

import pytest
import respx

from groceries_assistant.ah.client import BASE_URL, USER_AGENT, AHApiError, AHClient

SEARCH_URL = f"{BASE_URL}/mobile-services/product/search/v2"
TOKEN_URL = f"{BASE_URL}/mobile-auth/v1/auth/token/anonymous"

SEARCH_RESPONSE = {
    "products": [
        {
            "webshopId": 514008,
            "title": "AH Pesto rosso",
            "brand": "AH",
            "price": {"now": 2.19},
            "salesUnitSize": "190 g",
        },
        {
            "webshopId": 275341,
            "title": "Barilla Pesto alla Genovese",
            "brand": "Barilla",
            "price": None,
            "salesUnitSize": "190 g",
        },
    ]
}


def test_search_parses_products():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL).respond(
            json={"access_token": "tok123", "refresh_token": "r"}
        )
        search_route = mock.get(SEARCH_URL).respond(json=SEARCH_RESPONSE)

        with AHClient() as client:
            products = client.search("pesto", limit=2)

    assert token_route.called
    assert search_route.called
    request = search_route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok123"
    assert request.headers["x-application"] == "AHWEBSHOP"
    assert request.headers["User-Agent"] == USER_AGENT
    assert "query=pesto" in str(request.url)

    assert products[0].webshop_id == 514008
    assert products[0].title == "AH Pesto rosso"
    assert products[0].price_now == 2.19
    assert products[1].price_now is None  # missing price handled


def test_all_bonus_products_walks_periods_sections_and_groups():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(json={"access_token": "tok123", "refresh_token": "r"})
        mock.get(f"{BASE_URL}/mobile-services/bonuspage/v1/metadata").respond(
            json={
                "periods": [
                    {
                        "bonusStartDate": "2026-11-02",
                        "bonusEndDate": "2026-11-08",
                        "urlMetadataList": [{"url": "bonuspage/v1/section/actueel"}],
                    }
                ]
            }
        )
        section = mock.get(f"{BASE_URL}/mobile-services/bonuspage/v1/section/actueel").respond(
            json={
                "bonusGroupOrProducts": [
                    {
                        "product": {
                            "webshopId": 1,
                            "title": "Hak tomatenblokjes",
                            "price": {"now": 1.09},
                        }
                    },
                    {"product": {"webshopId": 1, "title": "dubbel, wordt gededupliceerd"}},
                    {"bonusGroup": {"id": "99"}},
                ]
            }
        )
        segment = mock.get(f"{BASE_URL}/mobile-services/bonuspage/v1/segment").respond(
            json={
                "products": [
                    {"webshopId": 2, "title": "AH Spaghetti 500 g", "price": {"now": 0.89}}
                ]
            }
        )

        with AHClient() as client:
            products = client.all_bonus_products(date(2026, 11, 5))

    assert [p.webshop_id for p in products] == [1, 2]  # deduplicated, group products included
    assert products[1].title == "AH Spaghetti 500 g"
    assert segment.calls.last.request.url.params["date"] == "2026-11-05"
    assert segment.calls.last.request.url.params["segmentId"] == "99"
    assert section.called

    # a day outside the bonus period fetches nothing
    with respx.mock(assert_all_called=False) as empty:
        empty.post(TOKEN_URL).respond(json={"access_token": "tok123", "refresh_token": "r"})
        empty.get(f"{BASE_URL}/mobile-services/bonuspage/v1/metadata").respond(
            json={
                "periods": [
                    {
                        "bonusStartDate": "2026-11-02",
                        "bonusEndDate": "2026-11-08",
                        "urlMetadataList": [{"url": "bonuspage/v1/section/actueel"}],
                    }
                ]
            }
        )
        with AHClient() as client:
            assert client.all_bonus_products(date(2026, 12, 24)) == []


def test_fallback_finds_bonus_products_via_search_filter():
    """bonuspage/v1 refuses anonymous tokens (HTTP 500): the fallback queries
    the search API per ingredient with the bonus filter, self-calibrating the
    undocumented param value against a probe query."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(json={"access_token": "tok123", "refresh_token": "r"})
        mock.get(f"{BASE_URL}/mobile-services/bonuspage/v1/metadata").respond(status_code=500)
        base = {
            "products": [
                {"webshopId": 10, "title": "AH Volkoren brood"},
                {"webshopId": 11, "title": "AH Melk halfvol"},
            ]
        }
        # filters the API ignores return the same set as the unfiltered probe
        mock.get(SEARCH_URL, params={"bonus": "Bonus"}).respond(json=base)
        mock.get(SEARCH_URL, params={"bonus": "DISCOUNT"}).respond(json=base)
        mock.get(SEARCH_URL, params={"bonus": "BONUS_AND_DISCOUNT"}).respond(json=base)
        honoured = mock.get(SEARCH_URL, params={"bonus": "BONUS"}).respond(
            json={
                "products": [
                    {"webshopId": 20, "title": "AH Spaghetti 500 g"},
                    {"webshopId": 21, "title": "Hak Tomatenblokjes"},
                ]
            }
        )
        plain = mock.get(SEARCH_URL).respond(json=base)  # the probe without filter

        with AHClient() as client:
            products = client.bonus_products_by_search(["spaghetti", "ui"])

    assert honoured.called
    assert plain.called
    assert [p.webshop_id for p in products] == [20, 21]  # deduplicated over the queries


def test_fallback_raises_when_the_filter_is_ignored():
    """If no bonus= value changes the result set, say so instead of marking
    every ingredient as in-the-bonus."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(json={"access_token": "tok123", "refresh_token": "r"})
        mock.get(SEARCH_URL).respond(
            json={
                "products": [
                    {"webshopId": 10, "title": "AH Volkoren brood"},
                    {"webshopId": 11, "title": "AH Melk halfvol"},
                ]
            }
        )

        with AHClient() as client, pytest.raises(AHApiError, match="bonus-filter"):
            client.bonus_products_by_search(["spaghetti"])


def test_search_parses_product_image_url():
    """The first AH image is hotlinked from their public CDN."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(json={"access_token": "tok123", "refresh_token": "r"})
        mock.get(SEARCH_URL).respond(
            json={
                "products": [
                    {
                        "webshopId": 1,
                        "title": "AH Pesto",
                        "images": [
                            {"width": 800, "height": 800, "url": "https://static.ah.nl/a.webp"},
                            {"url": "https://static.ah.nl/b.webp"},
                        ],
                    },
                    {"webshopId": 2, "title": "Zonder afbeelding"},
                ]
            }
        )

        with AHClient() as client:
            products = client.search("pesto", limit=2)

    assert products[0].image_url == "https://static.ah.nl/a.webp"
    assert products[1].image_url is None


def test_token_is_cached_between_searches():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL).respond(
            json={"access_token": "tok123", "refresh_token": "r"}
        )
        mock.get(SEARCH_URL).respond(json=SEARCH_RESPONSE)

        with AHClient() as client:
            client.search("pesto")
            client.search("courgette")

    assert token_route.call_count == 1
