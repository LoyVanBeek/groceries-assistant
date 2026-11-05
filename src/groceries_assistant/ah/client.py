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
            products.append(self._product_from(item))
        return products

    def _product_from(self, item: dict) -> AHProduct:
        """One AH API product dict -> AHProduct."""
        price = (item.get("price") or {}).get("now")
        images = item.get("images") or []
        image_url = images[0].get("url") if images else None
        return AHProduct(
            webshop_id=int(item["webshopId"]),
            title=str(item["title"]),
            brand=item.get("brand"),
            price_now=float(price) if price is not None else None,
            unit_size=item.get("salesUnitSize"),
            image_url=image_url,
        )

    def bonus_metadata(self) -> list[dict]:
        """The active bonus periods, each with the section URLs to walk."""
        token = self._ensure_token()
        try:
            response = self._client.get(
                f"{BASE_URL}/mobile-services/bonuspage/v1/metadata",
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"AH bonus metadata failed: HTTP {response.status_code}")
        data = response.json()
        return data.get("periods", []) if isinstance(data, dict) else []

    def bonus_section(self, path: str) -> list[dict]:
        """One bonus section: loose products and bonus groups (`url` from the
        period's urlMetadataList)."""
        token = self._ensure_token()
        try:
            response = self._client.get(
                f"{BASE_URL}/mobile-services/{path.lstrip('/')}",
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"AH bonus section failed: HTTP {response.status_code}")
        data = response.json()
        return data.get("bonusGroupOrProducts", []) if isinstance(data, dict) else []

    def bonus_segment_products(self, segment_id: str, day: date) -> list[dict]:
        """The products behind one bonus group."""
        token = self._ensure_token()
        try:
            response = self._client.get(
                f"{BASE_URL}/mobile-services/bonuspage/v1/segment",
                params={
                    "date": day.isoformat(),
                    "segmentId": segment_id,
                    "includeActivatableDiscount": "false",
                },
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise AHConnectionError(f"Cannot reach ah.nl: {exc}") from exc
        if response.is_error:
            raise AHApiError(f"AH bonus segment failed: HTTP {response.status_code}")
        data = response.json()
        return data.get("products", []) if isinstance(data, dict) else []

    def all_bonus_products(self, day: date) -> list[AHProduct]:
        """Every product in the bonus on `day`: the sections' loose products
        plus the products of each bonus group (deduplicated by webshop id)."""
        products: list[AHProduct] = []
        seen: set[int] = set()
        for period in self.bonus_metadata():
            start = str(period.get("bonusStartDate", ""))
            end = str(period.get("bonusEndDate", ""))
            if start and end and not start <= day.isoformat() <= end:
                continue
            for meta in period.get("urlMetadataList", []):
                url = meta.get("url") if isinstance(meta, dict) else None
                if not url:
                    continue
                for item in self.bonus_section(url):
                    if not isinstance(item, dict):
                        continue
                    if item.get("product"):
                        product = self._product_from(item["product"])
                        if product.webshop_id not in seen:
                            seen.add(product.webshop_id)
                            products.append(product)
                    group = item.get("bonusGroup") or {}
                    if group.get("id"):
                        for raw in self.bonus_segment_products(str(group["id"]), day):
                            product = self._product_from(raw)
                            if product.webshop_id not in seen:
                                seen.add(product.webshop_id)
                                products.append(product)
        return products
