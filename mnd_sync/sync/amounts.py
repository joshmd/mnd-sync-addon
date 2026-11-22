"""Resolve a Mealie quantity+unit into an amount MyNetDiary reads correctly.

MyNetDiary parses amount text loosely, and a live dry run showed it silently
misreading many amounts: unitless counts became grams ("16" chicken thighs ->
16 g), kilograms were not understood ("1.3 kg" -> 100 g), and large volumes
or some spoon measures fell back to defaults ("1200 ml" -> 1 g, "4 tsp" ->
100 g). Each looked like a valid match.

So instead of trusting the text, we work out the expected weight ourselves:

* mass units (g, kg, mg, oz, lb) convert exactly;
* volume and count units use the food's own measures from foodWeights
  (e.g. "medium = 118 g", "cup = 30 g");
* volumes with no matching measure are accepted only at a plausible density,
  and treated as 1 g/ml for plain ml/l amounts;
* counts need a count-like measure (medium, whole, piece, ...).

If MyNetDiary's grams for the original text differ from the expectation, the
amount is re-sent in grams and checked again. Anything we can't verify is
flagged for review rather than saved silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .units import normalize_unit

MASS_G = {"g": 1.0, "mg": 0.001, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
VOLUME_ML = {"ml": 1.0, "l": 1000.0, "tsp": 4.929, "tbsp": 14.787, "cup": 236.6}
# Words naming each volume measure as it may appear in a foodWeights title.
_VOLUME_TITLE_WORDS = {
    "ml": ("ml", "milliliter", "millilitre"),
    "l": ("liter", "litre"),
    "tsp": ("tsp", "teaspoon"),
    "tbsp": ("tbsp", "tablespoon", "tbs"),
    "cup": ("cup",),
}
# Grams per unit outside which MyNetDiary's reading is implausible.
UNIT_RANGE_G = {"clove": (2, 12), "slice": (8, 90), "can": (120, 1000), "pinch": (0.05, 2)}
# Units meaning "one of the thing", resolved via the food's count measure.
COUNT_UNITS = {"", "whole", "piece", "each", "item"}
SIZE_UNITS = {"small", "medium", "large"}
# Plausible g/ml for foods measured by volume without a matching measure
# (leafy greens ~0.1, flour ~0.5, water 1.0, honey ~1.4).
DENSITY_RANGE = (0.1, 1.6)
TOLERANCE = 0.10
# MyNetDiary rounds small weights to whole grams (4.5 g -> 5 g), so allow this
# much absolute slack as well as the relative tolerance.
ABS_TOLERANCE_G = 1.0
# Best-guess grams per unit, used only to send a sane estimate for an amount
# that is flagged for review (UK standard tin, average clove, ...).
UNIT_ESTIMATE_G = {"can": 400, "clove": 3, "slice": 30, "pinch": 0.4}
# An unverified amount whose calories are at most this is noted, not blocking.
NEGLIGIBLE_KCAL = 25

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MEASURE_TITLE_RE = re.compile(
    r"\b(g|gram|grams|kg|mg|oz|ounce|ounces|lb|lbs|pound|pounds|ml|l|dl|cl|liter|"
    r"litre|liters|litres|cup|cups|tbsp|tablespoon|tablespoons|tsp|teaspoon|"
    r"teaspoons|fl|quart|quarts|qt|pint|pints|pt|gallon|gallons|serving|servings|"
    r"portion|portions|100\s?g)\b", re.IGNORECASE)
# Measures naming part of an item. "2 whole" broccoli is not 2 spears, so these
# are used for a count only when nothing better exists.
_PART_TITLE_RE = re.compile(
    r"\b(spear|spears|floret|florets|leaf|leaves|sprig|sprigs|slice|slices|stalk|"
    r"stalks|segment|segments|wedge|wedges|strip|strips|cube|cubes|chunk|chunks|"
    r"ring|rings|stick|sticks|piece|pieces|tbsp|tsp)\b", re.IGNORECASE)
_WHOLE_PREFERENCE = ("medium", "whole", "head", "large", "small", "each", "item", "unit")


def number(value: Any) -> Optional[float]:
    m = _NUM_RE.search(str(value if value is not None else "").replace(",", ""))
    return float(m.group(0)) if m else None


def grams_of(raw: dict[str, Any]) -> Optional[float]:
    """Grams MyNetDiary computed for a resolved foodEntry."""
    for key in ("gmWgt", "rawGmWgt"):
        g = number(raw.get(key))
        if g is not None:
            return g
    return None


def food_measures(raw: dict[str, Any]) -> dict[str, float]:
    """{measure title (lowercase): grams per one unit} from foodWeights."""
    out: dict[str, float] = {}
    for w in raw.get("foodWeights") or []:
        title = str(w.get("title") or "").strip().lower()
        gm, amt = number(w.get("gmWgt")), number(w.get("amount")) or 1.0
        if title and gm and amt and title not in out:
            out[title] = gm / amt
    return out


def _measure_for(measures: dict[str, float], words) -> Optional[tuple[str, float]]:
    for title, g in measures.items():
        if any(re.search(rf"\b{re.escape(w)}s?\b", title) for w in words):
            return title, g
    return None


def _count_measure(measures: dict[str, float]) -> Optional[tuple[str, float]]:
    """A 'one of these' measure: a whole-item size (medium, whole, head, ...),
    else any named item ("thigh", "breast", "leek"), else a part-of-item
    measure (spear, slice, sprig) as a last resort. Volume and weight measures
    are never counts."""
    candidates = [(t, g) for t, g in measures.items() if not _MEASURE_TITLE_RE.search(t)]
    for preferred in _WHOLE_PREFERENCE:
        for t, g in candidates:
            if re.search(rf"\b{preferred}\b", t):
                return t, g
    whole_items = [(t, g) for t, g in candidates if not _PART_TITLE_RE.search(t)]
    if whole_items:
        return whole_items[0]
    return candidates[0] if candidates else None


def _fmt_g(g: float) -> str:
    return f"{g:.0f}" if g >= 10 else f"{g:.1f}".rstrip("0").rstrip(".")


def first_amount(quantity: float, unit: Optional[str], text: str) -> str:
    """Amount text for the first lookup. Mass is sent as grams outright, since
    MyNetDiary doesn't parse kg; other units go as written."""
    u = normalize_unit(unit)
    if u in MASS_G:
        return f"{_fmt_g(quantity * MASS_G[u])} g"
    return text


@dataclass
class AmountCheck:
    expected_g: Optional[float]   # what the amount should weigh, if known
    basis: str                    # how expected_g was derived (for the report)
    review: str = ""              # non-empty: can't trust this amount


def expected_grams(quantity: float, unit: Optional[str], raw: dict[str, Any]) -> AmountCheck:
    """Work out what ``quantity unit`` should weigh for this food."""
    u = normalize_unit(unit)
    measures = food_measures(raw)
    got = grams_of(raw)

    if u in MASS_G:
        return AmountCheck(quantity * MASS_G[u], "exact")

    if u in VOLUME_ML:
        m = _measure_for(measures, _VOLUME_TITLE_WORDS[u])
        if m:
            return AmountCheck(quantity * m[1], f"{quantity:g} × {m[0]} ({_fmt_g(m[1])} g)")
        if u == "l":  # litres via a ml measure, if present
            m = _measure_for(measures, _VOLUME_TITLE_WORDS["ml"])
            if m:
                return AmountCheck(quantity * 1000 * m[1], f"{quantity:g} l via ml measure")
        ml = quantity * VOLUME_ML[u]
        if got and DENSITY_RANGE[0] <= got / ml <= DENSITY_RANGE[1]:
            return AmountCheck(got, f"MyNetDiary's {u} reading (≈{got / ml:.2f} g/ml)")
        if u in ("ml", "l"):
            return AmountCheck(ml, "assumed 1 g/ml")
        return AmountCheck(ml, f"estimated at 1 g/ml ≈ {_fmt_g(ml)} g", review=(
            f"no {u} measure for this food and MyNetDiary read it as "
            f"{_fmt_g(got) if got else '?'} g (≈{_fmt_g(ml)} ml)"))

    if u in COUNT_UNITS:
        m = _count_measure(measures)
        if m:
            return AmountCheck(quantity * m[1], f"{quantity:g} × {m[0]} ({_fmt_g(m[1])} g)")
        return AmountCheck(None, "", review=(
            "no unit in Mealie and no count measure (e.g. medium/whole) in MyNetDiary"))

    if u in SIZE_UNITS:
        m = _measure_for(measures, (u,))
        if m:
            return AmountCheck(quantity * m[1], f"{quantity:g} × {m[0]} ({_fmt_g(m[1])} g)")
        return AmountCheck(got, "MyNetDiary's reading") if got else AmountCheck(None, "")

    # other named units: clove, slice, can, pinch, ...
    m = _measure_for(measures, (u,))
    if m:
        return AmountCheck(quantity * m[1], f"{quantity:g} × {m[0]} ({_fmt_g(m[1])} g)")
    if got is not None and u in UNIT_RANGE_G:
        lo, hi = UNIT_RANGE_G[u]
        per = got / quantity
        if lo <= per <= hi:
            return AmountCheck(got, f"MyNetDiary's {u} reading ({_fmt_g(per)} g each)")
        est = UNIT_ESTIMATE_G.get(u)
        return AmountCheck(
            quantity * est if est else None,
            f"estimated at {est:g} g per {u}" if est else "",
            review=(f"MyNetDiary read {quantity:g} {u} as {_fmt_g(got)} g "
                    f"({_fmt_g(per)} g per {u}, expected {lo}–{hi} g)"))
    # unknown unit with no matching measure: MyNetDiary's reading can't be
    # checked (a live run read "8 nest" of noodles as 160 g), so flag it
    return AmountCheck(None, "", review=(
        f"unit '{u}' isn't a measure for this food, so MyNetDiary's reading "
        f"({_fmt_g(got) if got else '?'} g) can't be checked; give it in grams in Mealie"))


def close_enough(got: Optional[float], expected: float) -> bool:
    if got is None or expected <= 0:
        return False
    return abs(got - expected) <= max(TOLERANCE * expected, ABS_TOLERANCE_G)


def grams_amount(g: float) -> str:
    return f"{_fmt_g(g)} g"
