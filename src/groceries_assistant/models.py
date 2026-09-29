"""Domain models: recipes, dishes, ingredients, product facts, plan entries."""

from __future__ import annotations

import re
from datetime import timedelta
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
    scales_with_servings: bool = True
    """Whether ingredient amounts scale with the people count.

    False for a component: it is one purchase ('a bag of potatoes'), not a
    quantity per portion, so 1 stuk stays 1 stuk whatever the household is.
    """
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


class DishOption(BaseModel):
    """One way to fill a part, as this dish uses it.

    A component is shared between dishes, but this dish may want more of it, or
    take longer over it, than the component itself says. Values left empty are
    inherited from the component or recipe the slug points at, so a dish that
    agrees with the component stores only the slug.
    """

    slug: str
    amount: float | None = Field(default=None, gt=0)
    unit: str | None = None
    minutes: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_slug(cls, data: Any) -> Any:
        """A plain slug in a hand-written file means 'as the component has it'."""
        if isinstance(data, str):
            return {"slug": data}
        return data

    @field_validator("slug", "unit")
    @classmethod
    def _not_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value.strip() if value is not None else None


class DishPart(BaseModel):
    """One slot of a composed dish, e.g. the vegetables of a 'groente-vlees' dish.

    `key` identifies the slot in a plan (it is the pick key); when it is not
    given it is derived from the label, so the definition form only has to ask
    for a label and a list of options.
    """

    key: str = ""
    label: str
    options: list[DishOption] = Field(min_length=1)
    """The components or recipes that can fill this part."""

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
    def _options_not_empty(cls, values: list[DishOption]) -> list[DishOption]:
        cleaned = [option for option in values if option.slug]
        if not cleaned:
            raise ValueError("must have at least one option")
        return cleaned

    @property
    def option_slugs(self) -> list[str]:
        """The slugs that can fill this part, in the order they were declared."""
        return [option.slug for option in self.options]


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
        """Every recipe or component slug that can fill any part of this dish."""
        return {slug for part in self.parts for slug in part.option_slugs}

    def recipes(self, known: dict[str, Recipe]) -> dict[str, Recipe]:
        """`known` as this dish sees it: each option's own amount and time applied.

        A component is shared between dishes, but a dish may want more of it or
        take longer over it than the component says, so an option's own values
        win where it gives them and the component's are inherited where it does
        not. A dish that overrides nothing gets `known` itself, unchanged.

        The result is read-only, like `known` itself: callers must not mutate it.
        """
        overrides = {
            option.slug: option
            for part in self.parts
            for option in part.options
            if option.amount is not None or option.unit is not None or option.minutes is not None
        }
        if not overrides:
            return known
        result = dict(known)
        for slug, option in overrides.items():
            base = result.get(slug)
            if base is not None:
                result[slug] = _with_option(base, option)
        return result

    def part(self, key: str) -> DishPart | None:
        return next((p for p in self.parts if p.key == key), None)


def _with_option(base: Recipe, option: DishOption) -> Recipe:
    """The component as this dish's option uses it: its own amount and time win.

    An amount only means something for a component, which is a single product and
    so has one ingredient; a recipe with a list of ingredients keeps the amounts
    it wrote down itself.
    """
    changes: dict[str, Any] = {}
    if option.minutes is not None:
        changes["time"] = base.time.model_copy(update={"cook": option.minutes})
    if (option.amount is not None or option.unit) and len(base.ingredients) == 1:
        changes["ingredients"] = [
            base.ingredients[0].model_copy(
                update={
                    field: value
                    for field, value in (("amount", option.amount), ("unit", option.unit))
                    if value is not None
                }
            )
        ]
    return base.model_copy(update=changes)


class ProductFact(BaseModel):
    """How one purchase of a product looks, and how long what is left over keeps.

    This is what turns leftovers into arithmetic instead of bookkeeping: you buy
    whole packs, the meal uses part of one, and the rest is stock until it
    expires. Opt-in per product -- a product without a fact behaves exactly as
    before (buy the summed need, in the demand's own unit).

    `pack_qty`/`pack_unit` is one purchase: 1 stuk broccoli, 500 g mince.
    `unit_qty`/`unit_measure` is the weight of one piece, for products sold by
    piece (1 stuk = 350 g); leave it empty for a pack that is a plain weight.
    """

    product: str
    pack_qty: float = Field(gt=0)
    pack_unit: str = "stuk"
    unit_qty: float | None = Field(default=None, gt=0)
    unit_measure: str | None = None
    shelf_life_days: int = Field(default=3, ge=0, le=365)

    @field_validator("product", "pack_unit")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("pack_qty", "unit_qty")
    @classmethod
    def _whole_numbers_stay_whole(cls, value: float | None) -> float | None:
        """One pack is 1 stuk, not 1.0: the numbers are shown to the user."""
        if value is not None and float(value).is_integer():
            return int(value)
        return value

    @model_validator(mode="after")
    def _piece_needs_its_weight(self) -> ProductFact:
        if (self.unit_qty is None) != (self.unit_measure is None):
            raise ValueError("unit_qty and unit_measure go together")
        return self

    @property
    def pack_measure(self) -> str:
        """The unit a pack's contents are measured in: its weight, or 'stuk'."""
        return self.unit_measure or self.pack_unit

    @property
    def pack_contents(self) -> float:
        """How much the pack holds, in `pack_measure` (350 g for 1 stuk broccoli)."""
        if self.unit_measure and self.unit_qty is not None:
            return self.pack_qty * self.unit_qty
        return self.pack_qty

    @property
    def shelf_life(self) -> timedelta:
        return timedelta(days=self.shelf_life_days)


class Component(BaseModel):
    """A product you buy in the store, used as part of a composed dish.

    Unlike a Recipe, a Component has no steps, tags, servings or ingredients
    list. It is the item you buy: 'zak krieltjes 750 g', 'brocolli'. Its
    minutes is how long it takes to prepare (boil krieltjes, steam broccoli).
    Verdicts and product facts attach to the component's slug.
    """

    slug: str
    name: str
    amount: float = Field(default=1.0, gt=0)
    unit: str = "stuk"
    minutes: int = Field(default=0, ge=0)
    notes: str | None = None

    @field_validator("name", "slug")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
