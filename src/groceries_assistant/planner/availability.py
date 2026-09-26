"""Evening availability model (manual input, calendar import is a later bonus)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Free-minute categories, for display only.
CATEGORY_QUICK = "quick"
CATEGORY_NORMAL = "normal"
CATEGORY_RELAXED = "relaxed"
QUICK_MAX = 30
NORMAL_MAX = 60


@dataclass(frozen=True)
class EveningAvailability:
    """One evening's cooking budget, entered manually in the weekly template.

    `joiners` lists the person ids eating that evening (None = everyone);
    absent people's verdicts don't steer the recipe choice and their absence
    reduces the portion count.
    """

    date: date
    free_minutes: int
    cook: str | None = None
    allow_leftovers: bool = False
    joiners: tuple[str, ...] | None = None

    @property
    def category(self) -> str:
        if self.free_minutes < QUICK_MAX:
            return CATEGORY_QUICK
        if self.free_minutes < NORMAL_MAX:
            return CATEGORY_NORMAL
        return CATEGORY_RELAXED

    @property
    def people_count(self) -> int:
        """Portions needed; caller supplies the default when joiners is None."""
        return len(self.joiners) if self.joiners is not None else 0
