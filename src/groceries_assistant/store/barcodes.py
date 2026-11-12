"""The household's barcode registry: the barcodes table maps an EAN to the
product name it was scanned with.

There is no barcode lookup API (AH's gtin endpoint answers 404), so the map
learns: an unknown scan asks for the name once and remembers it. De oude
data/barcodes.yaml wordt bij het opstarten/importeren één keer ingelezen.
"""

from __future__ import annotations

import re

from groceries_assistant.store.db import Store

EAN_RE = re.compile(r"^\d{6,14}$")


def valid_ean(code: str) -> bool:
    """Plausible barcode: EAN-8/EAN-13/UPC-shaped, digits only."""
    return bool(EAN_RE.fullmatch(code))


class BarcodeStore:
    """The barcode -> product-name map, in de plandatabase."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._map: dict[str, str] = {}

    def load(self) -> None:
        self._map = {
            row["ean"]: row["title"]
            for row in self._store.conn.execute("SELECT ean, title FROM barcodes")
        }

    def save(self) -> None:
        self._store.conn.execute("DELETE FROM barcodes")
        self._store.conn.executemany(
            "INSERT INTO barcodes(ean, title) VALUES (?,?)",
            list(self._map.items()),
        )
        self._store.conn.commit()

    def get(self, ean: str) -> str | None:
        return self._map.get(ean)

    def put(self, ean: str, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("productnaam mag niet leeg zijn")
        self._map[ean] = title
        self.save()
