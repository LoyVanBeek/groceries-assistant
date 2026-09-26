"""Thin client for the Albert Heijn mobile API (anonymous product search).

The AH API is unofficial and can change without notice. Only the anonymous,
read-only product search is implemented here; writes to the AH shopping list
go through the `appie` CLI (see appie_cli.py) which handles the OAuth login.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    def search(self, query: str, limit: int = 5) -> list[AHProduct]:
        """Search AH products by (Dutch) keyword."""
        token = self._ensure_token()
        try:
            response = self._client.get(
                f"{BASE_URL}/mobile-services/product/search/v2",
                params={"query": query, "page": 0, "size": limit, "sortOn": "RELEVANCE"},
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"AH search failed: HTTP {response.status_code}")

        products: list[AHProduct] = []
        for item in response.json().get("products", [])[:limit]:
            price = (item.get("price") or {}).get("now")
            products.append(
                AHProduct(
                    webshop_id=int(item["webshopId"]),
                    title=str(item["title"]),
                    brand=item.get("brand"),
                    price_now=float(price) if price is not None else None,
                    unit_size=item.get("salesUnitSize"),
                )
            )
        return products
