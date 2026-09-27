"""Albert Heijn 'mijnlijst' deep-link builder.

Adding items to the AH shopping list happens through a URL of the form
https://www.ah.nl/mijnlijst/add-multiple?p=<webshop_id>:<qty>&p=<id>:<qty>
which must be opened by a logged-in browser session.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlencode

BASE = "https://www.ah.nl/mijnlijst/add-multiple"
PRODUCT_BASE = "https://www.ah.nl/producten/product"


def build_add_multiple_url(items: Sequence[tuple[int, int]]) -> str:
    """items: (ah_webshop_id, quantity) pairs, quantities >= 1."""
    params: list[tuple[str, str]] = []
    for webshop_id, quantity in items:
        quantity = max(int(quantity), 1)
        params.append(("p", f"{int(webshop_id)}:{quantity}"))
    return f"{BASE}?{urlencode(params)}"


def product_url(webshop_id: int) -> str:
    """Human-checkable product page for an AH webshop id.

    Product pages use the ``wi`` prefix (the ``wa`` prefix shows a
    "product does not exist" page).
    """
    return f"{PRODUCT_BASE}/wi{int(webshop_id)}"


def chunk_items(
    items: Sequence[tuple[int, int]], max_items: int = 25
) -> list[list[tuple[int, int]]]:
    """Split a large list into chunks; long URLs may be truncated by browsers."""
    items = list(items)
    return [items[i : i + max(max_items, 1)] for i in range(0, len(items), max(max_items, 1))]


def generate_qr_svg(url: str, box_size: int = 6) -> str:
    """Render the deep-link as a QR code SVG (for the future kitchen display)."""
    try:
        import qrcode
        import qrcode.image.svg
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("pip install qrcode") from exc

    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(url, image_factory=factory, box_size=box_size)
    data = img.to_string()
    return data.decode() if isinstance(data, bytes) else data
