"""Translate Mealie quantity + unit into a MyNetDiary amount string.

MyNetDiary's ``amountInputString`` is free text that its parser understands,
e.g. "150 g", "2 cup", "1 tbsp", "0.5 oz". Mealie gives a numeric quantity and
a unit whose name/abbreviation varies ("gram", "grams", "g", "cup", "cups").
We normalize the unit to a compact MND-friendly token and format the quantity
(including common fractions) sensibly.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

# Canonical MND unit token for a variety of Mealie spellings.
_UNIT_ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "gr": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "mg": "mg", "milligram": "mg", "milligrams": "mg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "cup": "cup", "cups": "cup",
    "clove": "clove", "cloves": "clove",
    "slice": "slice", "slices": "slice",
    "piece": "piece", "pieces": "piece",
    "can": "can", "cans": "can",
    "pinch": "pinch", "pinches": "pinch",
    "serving": "serving", "servings": "serving",
    # dimensionless count words MND reads as sizes
    "small": "small", "medium": "medium", "large": "large",
    "": "",
}


def normalize_unit(unit: Optional[str]) -> str:
    """Map a Mealie unit name/abbreviation to a compact MND token ('' if none)."""
    if not unit:
        return ""
    key = unit.strip().lower().rstrip(".")
    if key in _UNIT_ALIASES:
        return _UNIT_ALIASES[key]
    # singularize a trailing 's' as a last resort ("heads" -> "head")
    if key.endswith("s") and key[:-1] in _UNIT_ALIASES:
        return _UNIT_ALIASES[key[:-1]]
    return key  # pass through unknown units verbatim


def format_quantity(quantity: Optional[float]) -> str:
    """Format a quantity: integers plain, else a tidy fraction or decimal."""
    if quantity is None:
        return ""
    if float(quantity).is_integer():
        return str(int(quantity))
    # Prefer a common fraction (1/2, 1/3, 1/4, 3/4, ...) when it's exact-ish.
    frac = Fraction(quantity).limit_denominator(8)
    if abs(float(frac) - quantity) < 1e-6 and frac.denominator != 1:
        whole, rem = divmod(frac.numerator, frac.denominator)
        if whole:
            return f"{whole} {rem}/{frac.denominator}"
        return f"{rem}/{frac.denominator}"
    return f"{quantity:g}"


def to_mnd_amount(quantity: Optional[float], unit: Optional[str],
                  *, default: str = "1 serving") -> str:
    """Build the MND amount string from quantity + unit.

    - "150 g", "2 cup", "1 1/2 tbsp"
    - quantity with no unit -> bare number ("3")
    - unit with no quantity -> "1 <unit>"
    - neither -> ``default``
    """
    qty = format_quantity(quantity)
    u = normalize_unit(unit)
    if qty and u:
        # weight/volume tokens read better glued or spaced; MND accepts spaced.
        return f"{qty} {u}"
    if qty:
        return qty
    if u:
        return f"1 {u}"
    return default
