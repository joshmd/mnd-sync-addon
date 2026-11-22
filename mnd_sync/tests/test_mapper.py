"""Offline test for the Mealie->MND mapper using a fake MND client."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.models import MealieRecipe  # noqa: E402
from mynetdiary.errors import MndRequestError  # noqa: E402
from mynetdiary.models import FoodEntry, FoodSearchResult  # noqa: E402

SYS = {"system": True}
from sync.aliases import AliasMap  # noqa: E402
from sync.mapper import map_mealie_recipe  # noqa: E402
from sync.matching import Confidence  # noqa: E402
from sync.report import render_markdown  # noqa: E402


class FakeMnd:
    """Stand-in for MndClient.

    search_foods returns a few candidates; for 'banana' the exact match is NOT
    first, so a working matcher must still choose it. 'unobtainium' yields none.
    """

    def __init__(self, existing=None):
        self._existing = existing or {}

    def existing_recipes(self):
        return self._existing

    def search_foods(self, query, limit=8):
        q = query.lower()
        if "unobtainium" in q:
            return []
        if "banana" in q:
            return [
                FoodSearchResult(food_id="1", description="Banana bread", raw=SYS),
                FoodSearchResult(food_id="2", description="Banana", raw=SYS),
                FoodSearchResult(food_id="3", description="Banana chips", raw=SYS),
            ]
        if "kale" in q:  # only a custom food exists
            return [FoodSearchResult(food_id="c1", description="Kale", raw={"system": False})]
        if "rejectme" in q:
            return [FoodSearchResult(food_id="bad", description="Rejectme", raw=SYS)]
        return [FoodSearchResult(food_id=f"v1.{q}=", description=query.title(), raw=SYS)]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        if food_id == "bad":
            raise MndRequestError("muiGetFoodEntry.do: amount not accepted")
        return FoodEntry(food_id=food_id, description="x",
                         raw={"foodId": food_id, "foodDesc": "x",
                              "foodLabel": {}, "gmWgt": "100"})


def test_mapper_matches_best_not_first():
    mr = MealieRecipe.from_api({
        "name": "Smoothie",
        "recipeYield": "2 servings",
        "recipeIngredient": [
            {"food": {"name": "banana"}, "quantity": 2, "unit": {"name": "medium"}},
        ],
    })
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    r = result.resolutions[0]
    assert r.matched and r.mnd_food_id == "2"  # exact "Banana", not "Banana bread"
    assert r.confidence is Confidence.HIGH
    assert result.recipe.to_payload()["recipe"]["foods"][0]["amount"] == "2 medium"


def test_mapper_miss_and_empty():
    mr = MealieRecipe.from_api({
        "name": "Mixed",
        "recipeIngredient": [
            {"food": {"name": "unobtainium"}, "quantity": 1},
            {"note": ""},
        ],
    })
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    assert not result.all_matched
    assert len(result.unmatched) == 1          # unobtainium
    assert len(result.skipped) == 1            # the blank line is left out
    assert len(result.recipe.ingredients) == 0
    assert [r.needs_review for r in result.resolutions] == [True, False]


def test_mapper_alias_override_wins():
    mr = MealieRecipe.from_api({
        "name": "Flatbread",
        "recipeIngredient": [{"food": {"name": "maida"}, "quantity": 100,
                              "unit": {"name": "g"}}],
    })
    amap = AliasMap()
    amap.set("maida", "40112233", "White flour", amount="120 g")
    result = map_mealie_recipe(FakeMnd(), mr, aliases=amap, existing={})
    r = result.resolutions[0]
    assert r.via_alias and r.mnd_food_id == "40112233"
    assert r.amount == "120 g"          # alias amount overrides structured
    assert not r.needs_review           # aliases are trusted


def test_mapper_duplicate_detection():
    mr = MealieRecipe.from_api({
        "name": "Smoothie",
        "recipeIngredient": [{"food": {"name": "banana"}, "quantity": 1,
                              "unit": {"name": "medium"}}],
    })
    existing = {"smoothie": "80200000"}
    result = map_mealie_recipe(FakeMnd(existing), mr, existing=existing)
    assert result.is_duplicate
    assert result.existing_food_id == "80200000"


def test_report_renders():
    mr = MealieRecipe.from_api({
        "name": "Smoothie",
        "recipeYield": "2 servings",
        "recipeIngredient": [
            {"food": {"name": "banana"}, "quantity": 2, "unit": {"name": "medium"}},
            {"food": {"name": "unobtainium"}, "quantity": 1},
        ],
    })
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    md = render_markdown(result)
    assert "# Sync review: Smoothie" in md
    assert "Banana" in md
    assert "review needed" in md  # one ingredient unmatched


def test_custom_food_match_needs_review():
    mr = MealieRecipe.from_api({
        "name": "Greens",
        "recipeIngredient": [{"food": {"name": "kale"}, "quantity": 1,
                              "unit": {"name": "cup"}}],
    })
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    r = result.resolutions[0]
    assert r.matched and r.custom_food
    assert r.needs_review
    assert "custom food" in r.note


def test_custom_food_accepted_via_alias():
    mr = MealieRecipe.from_api({
        "name": "Greens",
        "recipeIngredient": [{"food": {"name": "kale"}, "quantity": 1}],
    })
    amap = AliasMap()
    amap.set("kale", "c1", "Kale (my custom)", amount="1 serving")
    r = map_mealie_recipe(FakeMnd(), mr, aliases=amap, existing={}).resolutions[0]
    assert r.via_alias and not r.needs_review


def test_resolve_error_becomes_unmatched():
    mr = MealieRecipe.from_api({
        "name": "Bad",
        "recipeIngredient": [{"food": {"name": "rejectme"}, "quantity": 1}],
    })
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    r = result.resolutions[0]
    assert not r.matched
    assert "rejected" in r.note
    assert len(result.recipe.ingredients) == 0


def test_zero_quantity_is_left_out_not_sent_as_zero():
    mr = MealieRecipe.from_api({"name": "S", "recipeIngredient": [
        {"food": {"name": "salt"}, "quantity": 0},
        {"food": {"name": "banana"}, "quantity": 2, "unit": {"name": "medium"}},
    ]})
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    salt, banana = result.resolutions
    assert salt.skipped and "no quantity" in salt.note and not salt.needs_review
    assert banana.matched
    assert [i.amount for i in result.recipe.ingredients] == ["2 medium"]
    assert result.all_matched  # skipped lines don't count against it


def test_serving_suggestions_left_out():
    lines = [
        {"note": "cooked basmati rice, poppadoms and mango chutney, to serve (To serve)"},
        {"note": "Carb of choice, to serve (To serve)"},
        {"note": "Favourite seasoning to taste (To taste)"},
        {"food": {"name": "basmati rice"}, "quantity": 200, "unit": {"name": "g"},
         "note": "optional"},
    ]
    mr = MealieRecipe.from_api({"name": "C", "recipeIngredient": lines})
    result = map_mealie_recipe(FakeMnd(), mr, existing={})
    *suggestions, rice = result.resolutions
    # serving suggestions with no quantity are left out...
    assert all(r.skipped for r in suggestions)
    # ...but a real quantity is part of the dish, even if marked optional
    assert not rice.skipped and rice.matched
    assert "marked to serve / optional" in rice.note
    assert len(result.recipe.ingredients) == 1


def test_alias_amount_rescues_missing_quantity():
    mr = MealieRecipe.from_api({"name": "S", "recipeIngredient": [
        {"food": {"name": "salt"}, "quantity": 0}]})
    amap = AliasMap()
    amap.set("salt", "s1", "Salt", amount="1/4 tsp")
    r = map_mealie_recipe(FakeMnd(), mr, aliases=amap, existing={}).resolutions[0]
    assert r.matched and not r.skipped and r.amount == "1/4 tsp"


class PoolMnd(FakeMnd):
    """Real-world shapes: the right food is far down the list, or only found
    by a simplified retry."""

    def __init__(self):
        super().__init__()
        self.queries = []

    def search_foods(self, query, limit=8):
        self.queries.append((query, limit))
        q = query.lower()
        if q == "cheese":
            junk = [FoodSearchResult(food_id=f"j{i}", description=f"Mac & cheese {i}", raw=SYS)
                    for i in range(20)]
            return (junk + [FoodSearchResult(food_id="ok", description="Cheese", raw=SYS)])[:limit]
        if q == "frozen chopped chilli":
            return []
        if q == "chili":
            return [FoodSearchResult(food_id="chili", description="Chili peppers", raw=SYS)]
        return []


def test_wide_candidate_pool_finds_plain_food():
    mnd = PoolMnd()
    mr = MealieRecipe.from_api({"name": "X", "recipeIngredient": [
        {"food": {"name": "cheese"}, "quantity": 30, "unit": {"name": "g"}}]})
    r = map_mealie_recipe(mnd, mr, existing={}).resolutions[0]
    assert r.mnd_food_id == "ok"
    assert mnd.queries[0] == ("cheese", 30)


def test_simplified_retry_search():
    mnd = PoolMnd()
    mr = MealieRecipe.from_api({"name": "X", "recipeIngredient": [
        {"food": {"name": "frozen chopped chilli"}, "quantity": 4, "unit": {"name": "tsp"}}]})
    r = map_mealie_recipe(mnd, mr, existing={}).resolutions[0]
    assert r.matched and r.mnd_food_id == "chili"
    assert [q for q, _ in mnd.queries] == ["frozen chopped chilli", "chili"]
