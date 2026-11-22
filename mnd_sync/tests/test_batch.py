"""Offline tests for sync-all batch logic using fake Mealie + MND clients."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.models import MealieRecipe  # noqa: E402
from mynetdiary.errors import MndRequestError  # noqa: E402
from mynetdiary.models import FoodEntry, FoodSearchResult, SavedFood  # noqa: E402
from sync.batch import sync_all  # noqa: E402
from sync.report import render_batch_markdown  # noqa: E402

SYS = {"system": True}


def ing(name, qty=1, unit="medium"):
    return {"food": {"name": name}, "quantity": qty, "unit": {"name": unit}}


RECIPES = {
    "smoothie": {"name": "Smoothie", "recipeIngredient": [ing("banana")]},
    "mystery": {"name": "Mystery", "recipeIngredient": [ing("banana"), ing("unobtainium")]},
    "nothing": {"name": "Nothing", "recipeIngredient": [ing("unobtainium")]},
    "existing": {"name": "Porridge", "recipeIngredient": [ing("banana")]},
    "smoothie-2": {"name": "Smoothie", "recipeIngredient": [ing("banana")]},
    "broken": None,  # get_recipe raises
}


class FakeMealie:
    def __init__(self, slugs):
        self.slugs = slugs

    def list_all_recipes(self):
        return [{"slug": s, "name": (RECIPES[s] or {}).get("name", s)} for s in self.slugs]

    def get_recipe(self, slug):
        data = RECIPES[slug]
        if data is None:
            raise RuntimeError("mealie 500")
        return MealieRecipe.from_api({**data, "slug": slug})


class FakeMnd:
    def __init__(self, existing=None, fail_save_for=()):
        self._existing = existing or {}
        self.saved = []
        self.fail_save_for = set(fail_save_for)

    def existing_recipes(self):
        return dict(self._existing)

    def search_foods(self, query, limit=8):
        if "unobtainium" in query.lower():
            return []
        return [FoodSearchResult(food_id=f"v1.{query}=", description=query.title(), raw=SYS)]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        return FoodEntry(food_id=food_id, description="x",
                         raw={"foodId": food_id, "foodDesc": "x", "foodLabel": {}})

    def save_recipe(self, recipe, dry_run=None):
        assert dry_run is False
        if recipe.name in self.fail_save_for:
            raise MndRequestError("muiSaveRecipe.do: boom")
        self.saved.append(recipe.name)
        return SavedFood(food_id=str(90000 + len(self.saved)), name=recipe.name)


def statuses(batch):
    return {o.slug: o.status for o in batch.outcomes}


ALL = ["smoothie", "mystery", "nothing", "existing", "smoothie-2", "broken"]


def test_dry_run_classifies_and_saves_nothing():
    mnd = FakeMnd(existing={"porridge": "123"})
    batch = sync_all(mnd, FakeMealie(ALL))
    assert mnd.saved == []
    assert statuses(batch) == {
        "smoothie": "ready",
        "mystery": "review",
        "nothing": "empty",
        "existing": "duplicate",
        "smoothie-2": "duplicate",  # same name planned earlier in this run
        "broken": "error",
    }


def test_commit_saves_only_ready():
    mnd = FakeMnd(existing={"porridge": "123"})
    batch = sync_all(mnd, FakeMealie(ALL), commit=True)
    assert mnd.saved == ["Smoothie"]
    s = statuses(batch)
    assert s["smoothie"] == "saved" and s["mystery"] == "review"
    assert s["smoothie-2"] == "duplicate"


def test_force_also_saves_review_but_never_empty_or_duplicates():
    mnd = FakeMnd(existing={"porridge": "123"})
    sync_all(mnd, FakeMealie(ALL), commit=True, force=True)
    assert mnd.saved == ["Smoothie", "Mystery"]


def test_allow_duplicates():
    mnd = FakeMnd(existing={"porridge": "123"})
    sync_all(mnd, FakeMealie(["existing"]), commit=True, allow_duplicates=True)
    assert mnd.saved == ["Porridge"]


def test_save_failure_is_isolated():
    mnd = FakeMnd(fail_save_for={"Smoothie"})
    batch = sync_all(mnd, FakeMealie(["smoothie", "existing"]), commit=True)
    s = statuses(batch)
    assert s["smoothie"] == "error" and "boom" in batch.outcomes[0].error
    assert s["existing"] == "saved"  # run continued
    assert batch.had_errors


def test_only_and_limit():
    batch = sync_all(FakeMnd(), FakeMealie(ALL), only=["mystery", "smoothie"])
    assert [o.slug for o in batch.outcomes] == ["smoothie", "mystery"]
    batch = sync_all(FakeMnd(), FakeMealie(ALL), limit=2)
    assert len(batch.outcomes) == 2


def test_batch_report_renders():
    batch = sync_all(FakeMnd(existing={"porridge": "123"}), FakeMealie(ALL))
    md = render_batch_markdown(batch)
    assert "sync-all (dry run)" in md
    assert "1 recipe(s) would be saved" in md
    assert "| Mystery | 1/2 |  | needs review |" in md
    assert "## Details" in md
    assert "earlier in this sync run" in md


def test_mealie_pagination():
    from mealie.client import MealieClient

    pages = {1: {"items": [{"slug": "a"}, {"slug": "b"}], "page": 1, "total_pages": 2},
             2: {"items": [{"slug": "c"}], "page": 2, "total_pages": 2}}
    client = MealieClient.__new__(MealieClient)  # skip network setup
    client._get = lambda path, params=None: pages[params["page"]]
    assert [r["slug"] for r in client.list_all_recipes(per_page=2)] == ["a", "b", "c"]


def test_mealie_pagination_without_total():
    from mealie.client import MealieClient

    pages = {1: {"items": [{"slug": "a"}, {"slug": "b"}]}, 2: {"items": [{"slug": "c"}]}}
    client = MealieClient.__new__(MealieClient)
    client._get = lambda path, params=None: pages[params["page"]]
    assert len(client.list_all_recipes(per_page=2)) == 3


def test_report_shows_weights_and_left_out():
    from sync.report import render_markdown, _sum_numeric
    from sync.mapper import map_mealie_recipe
    mr = MealieRecipe.from_api({"name": "W", "recipeServings": 2, "recipeIngredient": [
        ing("banana", 2), {"food": {"name": "salt"}, "quantity": 0}]})

    class WMnd(FakeMnd):
        def get_food_entry(self, food_id, amount, meal_type_id=1):
            return FoodEntry(food_id=food_id, description="x", raw={
                "foodId": food_id, "foodLabel": {}, "gmWgt": "236", "calories": "210cals"})

    md = render_markdown(map_mealie_recipe(WMnd(), mr, existing={}))
    assert "236 g / 210cals" in md
    assert "Total ≈ 210 kcal · ≈ 105 kcal per serving" in md
    assert "left out: 1" in md and "SKIP" in md
    assert _sum_numeric(["1,050", "x"]) == 1050


def test_ready_recipes_get_full_breakdown_too():
    """Ready recipes must be listed ingredient by ingredient, not just counted:
    a confident match can still be the wrong food."""
    batch = sync_all(FakeMnd(existing={"porridge": "123"}), FakeMealie(ALL))
    md = render_batch_markdown(batch)
    ready = [o for o in batch.outcomes if o.status == "ready"]
    assert ready
    for o in ready:
        assert f"### Sync review: {o.name}" in md
    mapped = [o for o in batch.outcomes if o.result is not None]
    assert md.count("### Sync review:") == len(mapped)
