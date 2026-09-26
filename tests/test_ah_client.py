import respx

from groceries_assistant.ah.client import BASE_URL, USER_AGENT, AHClient

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
