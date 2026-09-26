"""Domain models: recipes, ingredients, people, plan entries."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


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
    barcode: str | None = None
    """Optional EAN for the future barcode-scanner add-on."""

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
