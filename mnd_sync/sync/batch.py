"""Sync many Mealie recipes into MyNetDiary in one run (sync-all).

Each recipe is mapped (read-only) and classified:

  ready      every ingredient matched with confidence, not a duplicate
  review     at least one ingredient needs review (low confidence, custom food,
             unmatched) but some ingredients matched
  duplicate  a recipe with this name already exists in MyNetDiary, or appears
             earlier in this same run
  empty      no usable ingredient matched (or none in Mealie): nothing to save
  error      fetching or mapping the recipe failed
  saved      (commit only) saved successfully

On commit, only ``ready`` recipes are saved by default. ``force`` also saves
``review`` recipes, skipping their unmatched ingredients. Duplicates are only
saved with ``allow_duplicates``. ``empty`` recipes are never saved. One
recipe failing never stops the run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from mealie.models import MealieRecipe
from mynetdiary.errors import MndAuthError, MndError

from .aliases import AliasMap
from .mapper import MappingResult, map_mealie_recipe

READY, REVIEW, DUPLICATE, EMPTY, ERROR, SAVED = (
    "ready", "review", "duplicate", "empty", "error", "saved")


@dataclass
class RecipeOutcome:
    slug: str
    name: str
    status: str
    result: Optional[MappingResult] = None
    saved_food_id: Optional[str] = None
    error: str = ""

    @property
    def matched(self) -> int:
        return sum(1 for r in self.result.considered if r.matched) if self.result else 0

    @property
    def total(self) -> int:
        """Ingredients the sync tried to include (left-out lines excluded)."""
        return len(self.result.considered) if self.result else 0

    @property
    def skipped(self) -> int:
        return len(self.result.skipped) if self.result else 0


@dataclass
class BatchResult:
    outcomes: list[RecipeOutcome] = field(default_factory=list)
    committed: bool = False

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def had_errors(self) -> bool:
        return self.count(ERROR) > 0


def classify(result: MappingResult, seen_names: dict[str, str]) -> str:
    recipe = result.recipe
    if recipe is None or not recipe.ingredients:
        return EMPTY
    if recipe.name.strip().lower() in seen_names:
        return DUPLICATE
    if result.needs_review:
        return REVIEW
    return READY


def sync_all(
    mnd,
    mealie,
    *,
    aliases: Optional[AliasMap] = None,
    commit: bool = False,
    force: bool = False,
    allow_duplicates: bool = False,
    only: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    meal_not_recipe: bool = False,
    delay: float = 0.0,
    on_progress: Optional[Callable[[int, int, RecipeOutcome], None]] = None,
    aliases_for: Optional[Callable[[str], AliasMap]] = None,
) -> BatchResult:
    """Map (and optionally save) every Mealie recipe. See module docstring."""
    # Fails fast on an expired session (MndAuthError) before mapping anything.
    try:
        existing = mnd.existing_recipes()
    except MndAuthError:
        raise
    except MndError:
        existing = {}
    # names already in MND, plus names saved/planned earlier in this run
    seen: dict[str, str] = dict(existing)

    summaries = mealie.list_all_recipes()
    only_set = {s.strip() for s in only} if only else None
    if only_set is not None:
        summaries = [s for s in summaries if s.get("slug") in only_set]
    if limit is not None:
        summaries = summaries[:limit]

    batch = BatchResult(committed=commit)
    total = len(summaries)
    for i, summary in enumerate(summaries, 1):
        slug = summary.get("slug", "")
        name = summary.get("name", "") or slug
        outcome = _process_one(
            mnd, mealie, slug, name, seen,
            aliases=aliases_for(slug) if aliases_for else aliases, commit=commit,
            force=force, allow_duplicates=allow_duplicates,
            meal_not_recipe=meal_not_recipe)
        batch.outcomes.append(outcome)
        if on_progress:
            on_progress(i, total, outcome)
        if outcome.status == SAVED and delay:
            time.sleep(delay)
    return batch


def _process_one(mnd, mealie, slug, name, seen, *, aliases, commit, force,
                 allow_duplicates, meal_not_recipe) -> RecipeOutcome:
    try:
        mrecipe: MealieRecipe = mealie.get_recipe(slug)
        result = map_mealie_recipe(mnd, mrecipe, aliases=aliases,
                                   existing=seen, meal_not_recipe=meal_not_recipe)
    except MndAuthError:
        raise  # session expired: abort the whole run rather than report 45 failures
    except Exception as e:  # noqa: BLE001 - isolate per-recipe failures
        return RecipeOutcome(slug=slug, name=name, status=ERROR, error=str(e))

    name = mrecipe.name or name
    status = classify(result, seen)
    outcome = RecipeOutcome(slug=slug, name=name, status=status, result=result)

    should_save = (
        status == READY
        or (status == REVIEW and force)
        or (status == DUPLICATE and allow_duplicates
            and (not result.needs_review or force))
    )
    if not should_save:
        return outcome

    key = name.strip().lower()
    if not commit:
        seen[key] = "planned"  # later recipes with this name count as duplicates
        return outcome

    try:
        saved = mnd.save_recipe(result.recipe, dry_run=False)
    except MndAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        outcome.status = ERROR
        outcome.error = f"save failed: {e}"
        return outcome
    outcome.status = SAVED
    outcome.saved_food_id = saved.food_id
    seen[key] = saved.food_id
    return outcome
