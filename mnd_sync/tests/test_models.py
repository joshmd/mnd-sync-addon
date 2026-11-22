"""Offline unit tests for MND + Mealie models (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.models import MealieIngredient, MealieRecipe  # noqa: E402
from mynetdiary.models import (  # noqa: E402
    FoodEntry,
    Recipe,
)


def _banana_entry():
    return FoodEntry(
        food_id="v1.TOKEN=",
        description="Banana",
        raw={
            "foodId": "v1.TOKEN=",
            "foodDesc": "Banana",
            "foodLabel": {"foodName": "Banana"},
            "calories": "210",
            "rawCalories": 210,
            "imageSrc": "https://assets.mynetdiary.com/bean/i611.png",
            "carbs": {"TotalCarbs": "54"},
            "gmWgt": "236",
        },
    )


def test_recipe_payload_shape():
    r = Recipe(name="Test", servings="2")
    r.add_ingredient(_banana_entry(), "2 medium")
    payload = r.to_payload()
    assert set(payload) == {"recipe"}
    rec = payload["recipe"]
    assert rec["mealNotRecipe"] is False
    assert rec["name"] == "Test"
    assert rec["serving"] == "2"
    assert rec["imageId"] == 14
    assert len(rec["foods"]) == 1
    f = rec["foods"][0]
    assert f["foodId"] == "v1.TOKEN="
    assert f["amount"] == "2 medium"
    assert f["ordinal"] == 1
    assert "foodLabel" in f  # nutrition travels with the ingredient
    assert f["rawGmWgt"] == 236.0  # gmWgt -> rawGmWgt coercion


def test_meal_omits_serving():
    r = Recipe(name="Meal", meal_not_recipe=True, servings="9")
    r.add_ingredient(_banana_entry(), "1 medium")
    rec = r.to_payload()["recipe"]
    assert rec["mealNotRecipe"] is True
    assert "serving" not in rec


def test_ordinals_increment():
    r = Recipe(name="Multi")
    r.add_ingredient(_banana_entry(), "1")
    r.add_ingredient(_banana_entry(), "2")
    foods = r.to_payload()["recipe"]["foods"]
    assert [f["ordinal"] for f in foods] == [1, 2]


def test_mealie_ingredient_amount_and_query():
    ing = MealieIngredient.from_api({
        "food": {"name": "banana"},
        "unit": {"name": "medium"},
        "quantity": 2,
        "display": "2 medium bananas",
    })
    assert ing.search_query() == "banana"
    assert ing.amount_string() == "2 medium"


def test_mealie_ingredient_freetext_fallback():
    ing = MealieIngredient.from_api({"note": "a pinch of salt"})
    assert ing.search_query() == "a pinch of salt"
    assert ing.amount_string() == ""  # no structured qty/unit


def test_mealie_recipe_from_api():
    mr = MealieRecipe.from_api({
        "name": "Smoothie",
        "recipeYield": "4 servings",
        "recipeIngredient": [
            {"food": {"name": "banana"}, "unit": {"name": "medium"}, "quantity": 2},
            {"note": "1 cup milk"},
        ],
        "recipeInstructions": [{"text": "Blend."}],
    })
    assert mr.name == "Smoothie"
    assert mr.servings == "4"
    assert len(mr.ingredients) == 2
    assert mr.instructions == "Blend."
