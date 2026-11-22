"""Core changes for the add-on: exclude, per-recipe aliases, JSON mapping, change detection."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.models import MealieRecipe  # noqa: E402
from mynetdiary.models import FoodEntry, FoodSearchResult, SavedFood  # noqa: E402
from sync.aliases import AliasMap  # noqa: E402
from sync.batch import sync_all  # noqa: E402
from sync.mapper import map_mealie_recipe  # noqa: E402
from sync.serialize import content_hash, result_from_json, result_to_json  # noqa: E402


class FakeMnd:
    def existing_recipes(self):
        return {}

    def search_foods(self, query, limit=8):
        return [FoodSearchResult(food_id=f"v1.{query}=", description=query.title(),
                                 raw={"system": True})]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        return FoodEntry(food_id=food_id, description="x",
                         raw={"foodId": food_id, "foodDesc": "x", "foodLabel": {},
                              "gmWgt": "100", "calories": "120"})

    def save_recipe(self, recipe, dry_run=None):
        return SavedFood(food_id="1", name=recipe.name)


def recipe(name="R", *foods, **extra):
    return MealieRecipe.from_api({
        "name": name, "slug": name.lower(), "recipeServings": 2, **extra,
        "recipeIngredient": [{"food": {"name": f}, "quantity": 100, "unit": {"name": "g"}}
                             for f in foods]})


def test_exclude_alias_skips_line():
    aliases = AliasMap()
    aliases.set("salt", "", "", exclude=True)
    res = map_mealie_recipe(FakeMnd(), recipe("R", "salt", "banana"), aliases=aliases)
    salt, banana = res.resolutions
    assert salt.skipped and not salt.matched and not salt.needs_review
    assert salt.note == "left out: you chose to leave this out"
    assert banana.matched
    assert len(res.recipe.ingredients) == 1


class FakeMealie:
    def list_all_recipes(self):
        return [{"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}]

    def get_recipe(self, slug):
        return recipe(slug.upper(), "banana")


def test_sync_all_uses_per_recipe_aliases():
    maps = {}
    for slug in "ab":
        m = AliasMap()
        m.set("banana", f"food-{slug}", "Banana")
        maps[slug] = m
    batch = sync_all(FakeMnd(), FakeMealie(), aliases_for=maps.__getitem__)
    ids = {o.slug: o.result.resolutions[0].mnd_food_id for o in batch.outcomes}
    assert ids == {"a": "food-a", "b": "food-b"}


def test_mapping_json_round_trip_keeps_flags():
    aliases = AliasMap()
    aliases.set("salt", "", "", exclude=True)
    res = map_mealie_recipe(FakeMnd(), recipe("R", "banana", "salt", "kale"), aliases=aliases)
    res.resolutions[2].amount_issue = "unverified"
    data = result_from_json(result_to_json(res))
    assert data["name"] == "R" and data["servings"] == "2"
    assert data["total_kcal"] == 240 and data["kcal_per_serving"] == 120
    banana, salt, kale = data["resolutions"]
    assert banana["grams"] == "100" and banana["calories"] == "120"
    assert banana["confidence"] == "high" and banana["needs_review"] is False
    assert salt["skipped"] is True
    assert kale["amount_issue"] == "unverified" and kale["needs_review"] is True


def test_updated_at_parsed():
    assert MealieRecipe.from_api({"name": "x", "dateUpdated": "d1"}).updated_at == "d1"
    assert MealieRecipe.from_api({"name": "x", "updatedAt": "d2"}).updated_at == "d2"
    r = MealieRecipe.from_api({"name": "x", "updateAt": "d3"})
    assert r.updated_at == "d3" and r.raw["name"] == "x"
    assert MealieRecipe.from_api({"name": "x"}).updated_at is None


def test_content_hash_stable_across_key_order():
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})
