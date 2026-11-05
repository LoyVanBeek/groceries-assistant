"""The household's barcode registry: data/barcodes.yaml maps an EAN to the
product name it was scanned with.

There is no barcode lookup API (AH's gtin endpoint answers 404), so the map
learns: an unknown scan asks for the name once and remembers it. The file is
hand-editable.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

EAN_RE = re.compile(r"^\d{6,14}$")


def valid_ean(code: str) -> bool:
    """Plausible barcode: EAN-8/EAN-13/UPC-shaped, digits only."""
    return bool(EAN_RE.fullmatch(code))


class BarcodeStore:
    """The barcode -> product-name map, YAML-backed, hand-editable."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._map: dict[str, str] = {}

    def load(self) -> None:
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        self._map = {str(k): str(v) for k, v in raw.items()}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(self._map, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, ean: str) -> str | None:
        return self._map.get(ean)

    def put(self, ean: str, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("productnaam mag niet leeg zijn")
        self._map[ean] = title
        self.save()
