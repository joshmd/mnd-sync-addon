"""Map a MealieRecipe into a MyNetDiary Recipe (Route 2).

Pipeline per ingredient:
  1. Alias map  — a recorded override wins outright (deterministic).
  2. Search     — fetch several MND candidates for the ingredient name.
  3. Match      — fuzzy-score candidates; take the best, with a confidence band.
  4. Resolve    — turn the chosen foodId + amount into a labelled food entry.

Nothing is saved. The result reports every ingredient (matched / low-confidence
/ unmatched) plus whether a recipe of this name already exists in MND, so the
caller can review before committing.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Optional

from mealie.models import MealieIngredient, MealieRecipe
from mynetdiary.client import MndClient
from mynetdiary.errors import MndAuthError, MndRequestError, MndSearchError
from mynetdiary.models import Recipe

from .aliases import AliasMap
from .amounts import (NEGLIGIBLE_KCAL, close_enough, expected_grams, first_amount,
                      grams_amount, grams_of, number)
from .matching import HIGH_THRESHOLD, Confidence, ranked_matches, simplify_query
from .units import to_mnd_amount

# Matches below this confidence are added but flagged for review. Medium
# matches were the source of most wrong foods in a live run, so only high
# confidence (or an alias) passes unreviewed.
DEFAULT_MIN_CONFIDENCE = Confidence.HIGH
_CONF_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


@dataclass
class IngredientResolution:
    source: MealieIngredient
    query: str
    amount: str
    matched: bool
    mnd_food_id: Optional[str] = None
    mnd_description: Optional[str] = None
    confidence: Optional[Confidence] = None
    score: Optional[float] = None
    via_alias: bool = False
    custom_food: bool = False  # matched a user custom food (not a database food)
    skipped: bool = False      # deliberately left out (no quantity, to serve, ...)
    amount_issue: str = ""          # non-empty: the amount couldn't be verified
    grams: Optional[str] = None     # weight MND computed for this amount
    calories: Optional[str] = None  # calories MND computed for this amount
    note: str = ""

    @property
    def needs_review(self) -> bool:
        if self.skipped:
            return False
        if not self.matched:
            return True
        if self.via_alias:
            return False  # an alias is an explicit human decision
        if self.amount_issue:
            return True   # MyNetDiary may have misread the amount
        if self.custom_food:
            return True   # custom foods may reject generic amounts
        return _CONF_ORDER.get(self.confidence, 0) < _CONF_ORDER[DEFAULT_MIN_CONFIDENCE]


@dataclass
class MappingResult:
    recipe: Optional[Recipe]
    resolutions: list[IngredientResolution] = field(default_factory=list)
    existing_food_id: Optional[str] = None  # set if a recipe of this name exists

    @property
    def considered(self) -> list[IngredientResolution]:
        """Ingredients the sync tried to include (i.e. not skipped)."""
        return [r for r in self.resolutions if not r.skipped]

    @property
    def skipped(self) -> list[IngredientResolution]:
        return [r for r in self.resolutions if r.skipped]

    @property
    def all_matched(self) -> bool:
        c = self.considered
        return bool(c) and all(r.matched for r in c)

    @property
    def needs_review(self) -> list[IngredientResolution]:
        return [r for r in self.resolutions if r.needs_review]

    @property
    def unmatched(self) -> list[IngredientResolution]:
        return [r for r in self.resolutions if not r.matched and not r.skipped]

    @property
    def is_duplicate(self) -> bool:
        return self.existing_food_id is not None


# Lines that are serving suggestions or optional extras, not part of the dish.
_SERVE_RE = re.compile(
    r"\b(to serve|for serving|serve with|to garnish|for garnish|optional|"
    r"of (your )?choice|your choice|to taste)\b", re.IGNORECASE)


def _resolve_amount(ing: MealieIngredient, alias_amount: Optional[str]) -> Optional[str]:
    """MND amount string, or None when Mealie gives no quantity."""
    if alias_amount:
        return alias_amount
    if not ing.has_quantity:
        return None
    return to_mnd_amount(ing.quantity, ing.unit, default="") or None


def _skip_reason(ing: MealieIngredient, query: str, amount: Optional[str]) -> Optional[str]:
    """Why an ingredient line is left out of the MND recipe, or None to include it."""
    if not query:
        return "blank line"
    if amount is None:
        text = " ".join(t for t in (ing.display, ing.note, ing.food) if t)
        if _SERVE_RE.search(text):
            return "serving suggestion / optional (no quantity)"
        return "no quantity in Mealie"
    return None


def _serve_note(ing: MealieIngredient) -> str:
    text = " ".join(t for t in (ing.display, ing.note) if t)
    return "marked to serve / optional in Mealie; included" if _SERVE_RE.search(text) else ""


def _resolve_entry(client, food_id: str, ing: MealieIngredient, amount: str,
                   *, trusted_amount: bool = False):
    """Look up food_id at the given amount, verifying MyNetDiary's grams.

    Returns (entry, amount_sent, basis_note, amount_issue). See sync.amounts.
    ``trusted_amount`` (an alias-supplied amount) skips verification. When an
    amount can only be estimated, the estimate is sent (so a forced save is
    sane) and the issue is kept, unless it is worth at most NEGLIGIBLE_KCAL.
    """
    if trusted_amount or not ing.has_quantity:
        return client.get_food_entry(food_id, amount), amount, "", ""
    send = first_amount(ing.quantity, ing.unit, amount)
    entry = client.get_food_entry(food_id, send)
    check = expected_grams(ing.quantity, ing.unit, entry.raw)
    if check.expected_g is None:
        return entry, send, "", check.review
    got = grams_of(entry.raw)
    sent_as_grams = send.endswith(" g")
    if not check.review and got is None and sent_as_grams:
        # No weight returned: check calories scale with the amount instead.
        scales = _kcal_scales(client, food_id, entry.raw, check.expected_g)
        if scales is False:
            return entry, send, "", _ignored_amount_msg(client, food_id, entry.raw, send)
        return entry, send, ("" if check.basis == "exact" else check.basis), ""
    if not check.review and close_enough(got, check.expected_g):
        return entry, send, ("" if check.basis == "exact" else check.basis), ""
    # misread or estimated: send the expected weight in grams instead
    send2 = grams_amount(check.expected_g)
    entry2 = entry if send2 == send else client.get_food_entry(food_id, send2)
    got2 = grams_of(entry2.raw)
    basis = f"sent as grams ({check.basis}); MyNetDiary read '{send}' as " \
            f"{'?' if got is None else round(got)} g"
    issue = check.review
    if got2 is None:
        if _kcal_scales(client, food_id, entry2.raw, check.expected_g) is False:
            issue = "; ".join(x for x in (issue, _ignored_amount_msg(
                client, food_id, entry2.raw, send2)) if x)
    elif not close_enough(got2, check.expected_g):
        issue = "; ".join(x for x in (issue, f"MyNetDiary gave {round(got2)} g for {send2}") if x)
    # Negligible-calorie lines are noted, not blocking, unless the entry ignores
    # amounts: then its calories are for some default serving, not this amount.
    if issue and _IGNORED_TAG not in issue:
        kcal = number(entry2.raw.get("calories"))
        if kcal is not None and kcal <= NEGLIGIBLE_KCAL:
            return entry2, send2, f"{basis}; {issue}; negligible ({kcal:g} kcal), not blocking", ""
    return entry2, send2, basis, issue


_PROBE_CACHE: dict = {}


def _kcal_per_100g(client, food_id: str) -> Optional[float]:
    key = (id(client), food_id)
    if key not in _PROBE_CACHE:
        try:
            _PROBE_CACHE[key] = number(client.get_food_entry(food_id, "100 g").raw.get("calories"))
        except MndRequestError:
            _PROBE_CACHE[key] = None
    return _PROBE_CACHE[key]


def _kcal_scales(client, food_id: str, raw: dict, grams: float) -> Optional[bool]:
    """When MyNetDiary returns no weight, check its calories against the food's
    calories per 100 g. False means the amount was evidently ignored (a live
    run gave 266 kcal for 750 g of beef strips). None means it can't be told."""
    kcal = number(raw.get("calories"))
    per100 = _kcal_per_100g(client, food_id)
    if kcal is None or not per100 or grams <= 0:
        return None
    expected = per100 * grams / 100
    return abs(kcal - expected) <= max(0.15 * expected, 5)


_IGNORED_TAG = "ignores the amount"


def _ignored_amount_msg(client, food_id: str, raw: dict, sent: str) -> str:
    kcal, per100 = number(raw.get("calories")), _kcal_per_100g(client, food_id)
    if per100 is not None and abs(kcal - per100) <= 1:
        what = f"returns {kcal:g} kcal for both {sent} and 100 g"
    else:
        what = f"returns {kcal:g} kcal for {sent} but {per100:g} kcal for 100 g"
    return (f"this MyNetDiary entry {_IGNORED_TAG} (it {what}); "
            "pick another food with an alias")


def _weight_kcal(entry) -> dict:
    """Grams and calories MyNetDiary computed for the chosen amount."""
    raw = getattr(entry, "raw", {}) or {}
    grams = raw.get("gmWgt") if raw.get("gmWgt") not in (None, "") else raw.get("rawGmWgt")
    cal = raw.get("calories") if raw.get("calories") not in (None, "") else raw.get("rawCalories")
    return {"grams": None if grams is None else str(grams),
            "calories": None if cal is None else str(cal)}


def _ranked_candidates(client, query: str, limit: int) -> list:
    """Search MND (retrying with a simplified query when the first is weak) and
    return that search's candidates as Matches, best first."""
    best: list = []
    for q in dict.fromkeys([query, simplify_query(query)]):  # ordered, de-duped
        if not q:
            continue
        try:
            candidates = client.search_foods(q, limit=limit)
        except MndSearchError:
            candidates = []
        ranked = ranked_matches(q, candidates) if candidates else []
        if ranked and (not best or ranked[0].score > best[0].score):
            best = ranked
        if best and best[0].confidence is Confidence.HIGH:
            break
    return best


# When the top match's amount can't be verified (e.g. it has no tsp or count
# measure), try this many next-best candidates that are nearly as good a name
# match, and use the first whose amount verifies.
MAX_ALTERNATES = 3
ALTERNATE_SCORE_SLACK = 0.10


def map_mealie_recipe(
    client: MndClient,
    mealie_recipe: MealieRecipe,
    *,
    meal_not_recipe: bool = False,
    aliases: Optional[AliasMap] = None,
    candidate_limit: int = 30,
    existing: Optional[dict[str, str]] = None,
) -> MappingResult:
    """Build an MND Recipe from a MealieRecipe. Read-only: no save performed.

    ``aliases``  — optional AliasMap of recorded overrides.
    ``existing`` — optional {name_lower: foodId} from client.existing_recipes()
                   for duplicate detection; pass {} to skip the lookup.
    """
    recipe = Recipe(
        name=mealie_recipe.name,
        servings=mealie_recipe.servings,
        meal_not_recipe=meal_not_recipe,
        prep_time=mealie_recipe.prep_time,
        cooking_time=mealie_recipe.cook_time,
        cooking_instructions=mealie_recipe.instructions,
    )

    if existing is None:
        try:
            existing = client.existing_recipes()
        except MndAuthError:
            raise  # a dead session must stop the run, not look like "no recipes"
        except Exception:  # noqa: BLE001 - duplicate check is best-effort
            existing = {}
    existing_food_id = existing.get(mealie_recipe.name.strip().lower())

    resolutions: list[IngredientResolution] = []
    for ing in mealie_recipe.ingredients:
        query = ing.search_query()

        # 1. alias override wins (and may supply the amount)
        alias = aliases.get(query) if aliases and query else None
        amount = _resolve_amount(ing, alias.amount if alias else None)

        # 2. leave out lines that aren't part of the dish or have no quantity
        if alias and alias.exclude:
            reason = "you chose to leave this out"
        else:
            reason = None if (alias and amount) else _skip_reason(ing, query, amount)
        if reason:
            resolutions.append(IngredientResolution(
                source=ing, query=query, amount=amount or "", matched=False,
                skipped=True, note=f"left out: {reason}"))
            continue

        if alias:
            try:
                entry, amount, basis, issue = _resolve_entry(
                    client, alias.food_id, ing, amount, trusted_amount=bool(alias.amount))
            except MndRequestError as e:
                resolutions.append(IngredientResolution(
                    source=ing, query=query, amount=amount, matched=False,
                    mnd_food_id=alias.food_id, mnd_description=alias.description,
                    via_alias=True, note=f"alias food rejected: {e}"))
                continue
            recipe.add_ingredient(entry, amount)
            resolutions.append(IngredientResolution(
                source=ing, query=query, amount=amount, matched=True,
                mnd_food_id=alias.food_id, mnd_description=alias.description,
                confidence=Confidence.HIGH, via_alias=True,
                amount_issue=issue,
                note="; ".join(n for n in ("alias override", basis, issue) if n),
                **_weight_kcal(entry)))
            continue

        # 3. search (with a simplified retry) + match
        ranked = _ranked_candidates(client, query, candidate_limit)
        if not ranked:
            resolutions.append(IngredientResolution(
                source=ing, query=query, amount=amount, matched=False,
                note="no MyNetDiary match"))
            continue

        # 4. resolve the labelled entry, trying near-equal alternatives if the
        #    top match's amount can't be verified
        top = ranked[0]
        floor = max(HIGH_THRESHOLD, top.score - ALTERNATE_SCORE_SLACK)
        tries = [top] + [m for m in ranked[1:MAX_ALTERNATES + 1] if m.score >= floor]
        picked = first_error = None
        for m in tries:
            try:
                got = _resolve_entry(client, m.candidate.food_id, ing, amount)
            except MndRequestError as e:
                first_error = first_error or e
                continue
            if picked is None or (picked[1][3] and not got[3]):
                picked = (m, got)
            if not got[3]:
                break
        if picked is None:
            resolutions.append(IngredientResolution(
                source=ing, query=query, amount=amount, matched=False,
                mnd_food_id=top.candidate.food_id, mnd_description=top.candidate.description,
                note=f"MyNetDiary rejected it: {first_error}"))
            continue
        match, (entry, amount_used, basis, issue) = picked
        chosen = match.candidate
        alt_note = ""
        if match is not top:
            alt_note = (f"used '{chosen.description}' instead of top match "
                        f"'{top.candidate.description}' (its amount could be verified)")
        recipe.add_ingredient(entry, amount_used)
        custom = not match.is_system
        resolutions.append(IngredientResolution(
            source=ing, query=query, amount=amount_used, matched=True,
            mnd_food_id=chosen.food_id, mnd_description=chosen.description,
            confidence=match.confidence, score=round(match.score, 3),
            custom_food=custom, amount_issue=issue, **_weight_kcal(entry),
            note="; ".join(n for n in (
                "custom food: may reject generic amounts; pin with an alias to accept"
                if custom else "",
                issue and f"amount: {issue}",
                alt_note,
                basis,
                _serve_note(ing),
            ) if n)))

    return MappingResult(recipe=recipe, resolutions=resolutions,
                         existing_food_id=existing_food_id)
