"""Thin client for the Albert Heijn mobile API (anonymous product search).

The AH API is unofficial and can change without notice. Only the anonymous,
read-only product search is implemented here; writes to the AH shopping list
go through the `appie` CLI (see appie_cli.py) which handles the OAuth login.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

BASE_URL = "https://api.ah.nl"
USER_AGENT = "Appie/9.28 (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"
CLIENT_ID = "appie-ios"
CLIENT_VERSION = "9.28"


class AHError(Exception):
    """Base error for AH API failures."""


class AHConnectionError(AHError):
    """AH is unreachable."""


class AHApiError(AHError):
    """AH returned an error response."""


@dataclass(frozen=True)
class AHProduct:
    webshop_id: int
    title: str
    brand: str | None = None
    price_now: float | None = None
    unit_size: str | None = None
    image_url: str | None = None
    """First product image on AH's public CDN (hotlinkable, no auth)."""
    bonus: bool = False
    """Whether the product is in the bonus (aanbieding) right now."""
    bonus_end: date | None = None
    """Last day of the bonus period, when the product is in bonus."""


class AHClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AHClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "x-client-name": CLIENT_ID,
            "x-client-version": CLIENT_VERSION,
            "x-application": "AHWEBSHOP",
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        try:
            response = self._client.post(
                f"{BASE_URL}/mobile-auth/v1/auth/token/anonymous",
                json={"clientId": CLIENT_ID},
                headers={k: v for k, v in self._headers().items() if k != "Authorization"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"Anonymous token request failed: HTTP {response.status_code}")
        self._token = response.json()["access_token"]
        return self._token

    def search(
        self, query: str, limit: int = 5, bonus_filter: str | None = None
    ) -> list[AHProduct]:
        """Search AH products by (Dutch) keyword.

        `bonus_filter` restricts the results to products in the bonus
        promotion; the value is one the search API honours (see
        `working_bonus_filter`).
        """
        token = self._ensure_token()
        params: dict = {"query": query, "page": 0, "size": limit, "sortOn": "RELEVANCE"}
        if bonus_filter:
            params["bonus"] = bonus_filter
        try:
            response = self._client.get(
                f"{BASE_URL}/mobile-services/product/search/v2",
                params=params,
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"AH search failed: HTTP {response.status_code}")

        products: list[AHProduct] = []
        for item in response.json().get("products", [])[:limit]:
            products.append(self._product_from(item))
        return products

    def _product_from(self, item: dict) -> AHProduct:
        """One AH API product dict -> AHProduct."""
        price = (item.get("price") or {}).get("now")
        images = item.get("images") or []
        image_url = images[0].get("url") if images else None
        end = item.get("bonusEndDate")
        return AHProduct(
            webshop_id=int(item["webshopId"]),
            title=str(item["title"]),
            brand=item.get("brand"),
            price_now=float(price) if price is not None else None,
            unit_size=item.get("salesUnitSize"),
            image_url=image_url,
            bonus=bool(item.get("isBonus")),
            bonus_end=_parse_iso(end),
        )

    def bonus_products_for_queries(self, queries: list[str], size: int = 10) -> list[AHProduct]:
        """Products in the bonus, found by searching the household's ingredient
        names and keeping the hits marked `isBonus` whose bonus period still
        runs today.

        The plain search API is the one product endpoint that works with
        anonymous tokens: its items carry `isBonus`/`bonusEndDate` directly
        (the bonuspage endpoints answer HTTP 500, detail/v4 is 404).
        """
        today_iso = date.today().isoformat()
        products: list[AHProduct] = []
        seen: set[int] = set()
        for query in queries:
            for product in self.search(query, limit=size):
                if not product.bonus:
                    continue
                if product.bonus_end is not None and product.bonus_end.isoformat() < today_iso:
                    continue
                if product.webshop_id not in seen:
                    seen.add(product.webshop_id)
                    products.append(product)
        return products


def _parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
