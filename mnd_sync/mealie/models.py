"""Mealie recipe models (the subset the MND sync needs).

Mealie's REST API returns recipes with a ``recipeIngredient`` array. Each
ingredient may be structured (quantity + unit + food) or just a free-text
``note``/``display`` string. We normalize both into MealieIngredient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MealieIngredient:
    """A normalized Mealie ingredient line.

    ``food`` is the ingredient name used to search MyNetDiary. ``quantity`` and
    ``unit`` form the amount string. ``display`` is the original free text,
    kept as a fallback search query when structured fields are absent.
    """

    food: str
    quantity: Optional[float] = None  # None = no quantity (Mealie stores 0)
    unit: Optional[str] = None
    display: str = ""
    note: str = ""

    @property
    def has_quantity(self) -> bool:
        return self.quantity is not None and self.quantity > 0

    def amount_string(self) -> str:
        """Best-effort serving string for MND (e.g. '150 g', '2 cup')."""
        qty = ""
        if self.quantity is not None:
            qty = f"{self.quantity:g}"
        parts = [p for p in (qty, (self.unit or "").strip()) if p]
        return " ".join(parts).strip()

    def search_query(self) -> str:
        """What to search MyNetDiary for: the food name, else the display text."""
        return (self.food or self.display or "").strip()

    @classmethod
    def from_api(cls, obj: dict[str, Any]) -> "MealieIngredient":
        food = ""
        f = obj.get("food")
        if isinstance(f, dict):
            food = f.get("name", "") or ""
        elif isinstance(f, str):
            food = f
        unit = ""
        u = obj.get("unit")
        if isinstance(u, dict):
            unit = u.get("name", "") or u.get("abbreviation", "") or ""
        elif isinstance(u, str):
            unit = u
        qty = obj.get("quantity")
        if not isinstance(qty, (int, float)) or qty <= 0:
            qty = None  # Mealie uses 0 for "no quantity given"
        return cls(
            food=food,
            quantity=qty,
            unit=unit or None,
            display=obj.get("display") or obj.get("note") or "",
            note=obj.get("note") or "",
        )


@dataclass
class MealieRecipe:
    name: str
    ingredients: list[MealieIngredient] = field(default_factory=list)
    servings: Optional[str] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    instructions: Optional[str] = None
    slug: str = ""
    updated_at: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, obj: dict[str, Any]) -> "MealieRecipe":
        ings = [MealieIngredient.from_api(i) for i in obj.get("recipeIngredient", [])]
        servings = None
        rs = obj.get("recipeServings")  # numeric in newer Mealie versions
        if isinstance(rs, (int, float)) and rs > 0:
            servings = f"{rs:g}"
        else:
            yield_ = obj.get("recipeYield")
            if isinstance(yield_, str):
                # first number only: "4 servings" -> "4", "4-6 servings" -> "4"
                m = re.search(r"\d+(?:\.\d+)?", yield_)
                servings = m.group(0) if m else None
        steps = obj.get("recipeInstructions") or []
        instructions = None
        if steps:
            instructions = "\n".join(
                s.get("text", "") if isinstance(s, dict) else str(s) for s in steps
            ).strip() or None
        return cls(
            name=obj.get("name", "") or "",
            ingredients=ings,
            servings=servings,
            prep_time=obj.get("prepTime"),
            cook_time=obj.get("performTime") or obj.get("cookTime"),
            instructions=instructions,
            slug=obj.get("slug", "") or "",
            updated_at=obj.get("dateUpdated") or obj.get("updatedAt") or obj.get("updateAt"),
            raw=obj,
        )
