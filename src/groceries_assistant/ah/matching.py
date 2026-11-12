"""Ingredient product -> AH product matching with a persistent mapping cache.

De koppelingen staan in de plandatabase (tabel ah_mappings); de oude
data/ah_mappings.yaml wordt bij het opstarten/importeren één keer ingelezen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from groceries_assistant.ah.client import AHProduct
from groceries_assistant.models import ProductFact
from groceries_assistant.store.db import Store

# Stopwords and size/unit tokens that add noise when comparing product names.
STOPWORDS = {"ah", "huismerk", "groot", "klein", "vers", "extra", "bio"}
SIZE_TOKENS = {"g", "kg", "ml", "l", "cl", "gram", "stuks", "stuk", "x"}


@dataclass
class AHMapping:
    product: str
    ah_webshop_id: int
    ah_title: str
    score: float
    confirmed: bool = False
    ah_image_url: str | None = None
    ah_unit_size: str | None = None
    """AH's own pack size ('350 g'), kept so it can be offered as a suggestion.

    Never applied on its own: the user declares how a product is packed.
    """


class AHMappingStore:
    """YAML-backed cache: Ingredient product name -> AH product. Human-editable."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._mappings: dict[str, AHMapping] = {}
        self._load()

    def _load(self) -> None:
        self._mappings = {}
        for row in self._store.conn.execute("SELECT * FROM ah_mappings"):
            self._mappings[row["key"]] = AHMapping(
                product=row["product"],
                ah_webshop_id=int(row["ah_webshop_id"]),
                ah_title=row["ah_title"] or "",
                score=float(row["score"] or 0.0),
                confirmed=bool(row["confirmed"]),
                ah_image_url=row["ah_image_url"],
                ah_unit_size=row["ah_unit_size"],
            )

    def save(self) -> None:
        self._store.conn.execute("DELETE FROM ah_mappings")
        self._store.conn.executemany(
            "INSERT INTO ah_mappings(key, product, ah_webshop_id, ah_title, score,"
            " confirmed, ah_image_url, ah_unit_size) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    m.product.lower(),
                    m.product,
                    m.ah_webshop_id,
                    m.ah_title,
                    round(m.score, 3),
                    int(m.confirmed),
                    m.ah_image_url,
                    m.ah_unit_size,
                )
                for m in sorted(self._mappings.values(), key=lambda m: m.product.lower())
            ],
        )
        self._store.conn.commit()

    def get(self, product: str) -> AHMapping | None:
        return self._mappings.get(product.lower())

    def all(self) -> list[AHMapping]:
        """Every mapping, product name ascending."""
        return sorted(self._mappings.values(), key=lambda m: m.product.lower())

    def put(self, mapping: AHMapping) -> None:
        self._mappings[mapping.product.lower()] = mapping
        self.save()

    def remove(self, product: str) -> None:
        self._mappings.pop(product.lower(), None)
        self.save()

    def __len__(self) -> int:
        return len(self._mappings)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [
        t for t in text.split() if t not in STOPWORDS and t not in SIZE_TOKENS and not t.isdigit()
    ]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """Combined sequence similarity and token containment, 0..1.

    Containment (overlap coefficient) is used instead of Jaccard so that a
    short Grocy name like 'pesto' can match a long AH title.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    sequence = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    containment = len(ta & tb) / min(len(ta), len(tb))
    return 0.6 * sequence + 0.4 * containment


def suggest_match(
    product: str, candidates: list[AHProduct], min_score: float = 0.45
) -> tuple[AHProduct | None, float]:
    """Pick the best AH candidate for an ingredient name."""
    best: AHProduct | None = None
    best_score = 0.0
    for candidate in candidates:
        score = similarity(product, candidate.title)
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < min_score:
        return None, best_score
    return best, best_score


# --- pack sizes ----------------------------------------------------------------
#
# AH's unit size is a free-text string ('350 g', '1 kg', '2 x 100 g', '6 stuks').
# Only the plain shapes are understood; anything else returns None so the user
# types the pack size themselves. A wrong guess here becomes a wrong list, so
# there is deliberately no clever fallback.

WEIGHT_MEASURES = {"g", "gram", "kg", "ml", "l"}
PIECE_UNITS = {"stuk", "stuks", "st"}
UNIT_SIZES = re.compile(
    r"""^\s*
        (?P<count>\d+(?:[.,]\d+)?)        # 1, 350, 2.5
        \s*(?:x\s*)?                        # '2 x 100 g'
        (?P<number>\d+(?:[.,]\d+)?)?
        \s*
        (?P<measure>[a-zA-Z]+)?
        \s*$""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class PackSize:
    """One purchase, read from a product's size string."""

    qty: float
    """How much one purchase holds, in `unit` ('2 x 100 g' -> 200)."""
    unit: str
    """The unit as written: 'g', 'kg', 'ml', 'l' or 'stuk'."""
    is_weight: bool
    """False for loose pieces: six pieces whose weight we do not know yet."""


def parse_unit_size(text: str | None) -> PackSize | None:
    """Read '350 g', '1 kg', '6 stuks', '2 x 100 g'; None for anything else.

    Deliberately narrow: a product we cannot read is shown raw and left to the
    user, never guessed at.
    """
    if not text:
        return None
    match = UNIT_SIZES.match(text.lower())
    if match is None:
        return None
    count = float(match["count"].replace(",", "."))
    number = match["number"]
    measure = match["measure"]

    if measure in PIECE_UNITS:
        if number is not None:
            return None  # '2 x 3 stuks' is not a pack we understand
        return PackSize(qty=count, unit="stuk", is_weight=False)
    if measure in WEIGHT_MEASURES:
        # '350 g' -> 350 g; '1 kg' -> 1 kg; '2 x 100 g' -> 200 g
        per_pack = count if number is None else count * float(number.replace(",", "."))
        return PackSize(qty=per_pack, unit=measure, is_weight=True)
    return None


def fact_from_unit_size(product: str, text: str | None) -> ProductFact | None:
    """A product fact pre-filled from an AH unit size, or None if unreadable.

    '350 g' means one pack of 350 g. '6 stuks' means six pieces, whose weight the
    user still has to fill in -- a piece weight is never invented.
    """
    pack = parse_unit_size(text)
    if pack is None:
        return None
    return ProductFact(
        product=product,
        pack_qty=pack.qty,
        pack_unit=pack.unit,
    )
