"""Domain models: recipes, dishes, ingredients, product facts, plan entries."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def slugify(text: str) -> str:
    """Filesystem/URL-safe slug, e.g. 'Pasta met pesto' -> 'pasta-met-pesto'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "recipe"


class Ingredient(BaseModel):
    product: str
    amount: float = Field(gt=0)
    unit: str = "stuk"

    @field_validator("product", "unit")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class TimeInfo(BaseModel):
    prep: int = Field(default=0, ge=0)
    cook: int = Field(default=0, ge=0)
    """Minutes that can be done the evening before (meal prep)."""
    prep_ahead: int = Field(default=0, ge=0)

    @property
    def total_minutes(self) -> int:
        return self.prep + self.cook

    @property
    def same_day_minutes(self) -> int:
        """Minutes needed on the evening itself (meal prep may happen earlier)."""
        return max(self.total_minutes - self.prep_ahead, 0)


class Recipe(BaseModel):
    slug: str
    name: str
    servings: int = Field(gt=0)
    time: TimeInfo = Field(default_factory=TimeInfo)
    tags: list[str] = Field(default_factory=list)
    protein: str | None = None
    """Main protein tag (e.g. 'kip', 'rund', 'vis', 'vegetarisch')."""
    steps: list[str] = Field(default_factory=list)
    ingredients: list[Ingredient] = Field(min_length=1)
    notes: str | None = None
    url: str | None = None
    """Optional link to the original recipe (e.g. the page it was copied from)."""
    barcode: str | None = None
    """Optional EAN for the future barcode-scanner add-on."""

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) url")
        return value

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class DishPart(BaseModel):
    """One slot of a composed dish, e.g. the vegetables of a 'groente-vlees' dish.

    `key` identifies the slot in a plan (it is the pick key); when it is not
    given it is derived from the label, so the definition form only has to ask
    for a label and a list of recipe slugs.
    """

    key: str = ""
    label: str
    options: list[str] = Field(min_length=1)
    """Recipe slugs that can fill this part."""

    @model_validator(mode="before")
    @classmethod
    def _derive_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and not (data.get("key") or "").strip():
            return {**data, "key": slugify(str(data.get("label", "")))}
        return data

    @field_validator("label", "key")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("options")
    @classmethod
    def _options_not_empty(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v.strip()]
        if not cleaned:
            raise ValueError("must have at least one option")
        return cleaned


class ComposedDish(BaseModel):
    """A dish assembled from parts, each part filled by one of a few recipes.

    The definition says which recipes may fill a part; which one actually does is
    a per-plan pick (see `plan_picks`).
    """

    slug: str
    name: str
    parts: list[DishPart] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _unique_part_keys(self) -> ComposedDish:
        keys = [part.key for part in self.parts]
        if len(set(keys)) != len(keys):
            raise ValueError("part keys must be unique within a dish")
        return self

    @field_validator("name", "slug")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @property
    def option_slugs(self) -> set[str]:
        """Every recipe slug that can fill any part of this dish."""
        return {slug for part in self.parts for slug in part.options}

    def part(self, key: str) -> DishPart | None:
        return next((p for p in self.parts if p.key == key), None)
