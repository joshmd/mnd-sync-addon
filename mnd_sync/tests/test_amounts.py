"""Amount verification, using misreadings seen in a live sync-all dry run."""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.models import MealieRecipe  # noqa: E402
from mynetdiary.models import FoodEntry, FoodSearchResult  # noqa: E402
from sync.amounts import expected_grams, first_amount  # noqa: E402
from sync.mapper import map_mealie_recipe  # noqa: E402

SYS = {"system": True}


def weights(**per_unit):
    """foodWeights list as MyNetDiary returns it: title, amount, '236 g'."""
    return [{"title": t.replace("_", " "), "amount": "1", "gmWgt": f"{g} g"}
            for t, g in per_unit.items()]


class SmartMnd:
    """Fake MND that reads "<n> g" literally and misreads other text the way
    the live run did (per-food table), returning foodWeights for each food."""

    FOODS = {
        "chicken thigh": dict(w=weights(thigh=75, gram=1, oz=28.3), misread={"16": 16}),
        "minced beef": dict(w=weights(gram=1, oz=28.3), misread={"1.3 kg": 100}),
        "chicken stock": dict(w=weights(gram=1), misread={"1200 ml": 1}),
        "frozen chopped ginger": dict(w=weights(gram=1), misread={"4 tsp": 100}),
        "butter bean": dict(w=weights(gram=1), misread={"4 can": 130}),
        "butter bean canned": dict(w=weights(can=240, gram=1), misread={"4 can": 130}),
        "spinach": dict(w=weights(cup=30, gram=1), misread={"2 cup": 60}),
        "egg": dict(w=weights(large=50, medium=44, gram=1), misread={"3": 3}),
        "basmati rice": dict(w=weights(gram=1, cup=185, serving=75), misread={"1": 1}),
        "dark soy sauce": dict(w=weights(gram=1), misread={}, no_weight=True),
        "flour": dict(w=weights(gram=1), misread={"3 tbsp": 23}),
        "tikka curry paste": dict(w=weights(gram=1), misread={}, no_weight=True, kcal_per_g=4),
        "olive oil": dict(w=weights(tsp=4.5, gram=1), misread={"1 tsp": 5}, kcal_per_g=9),
    }

    def existing_recipes(self):
        return {}

    def search_foods(self, query, limit=30):
        return [FoodSearchResult(food_id=query, description=query.title(), raw=SYS)]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        food = self.FOODS[food_id]
        m = re.fullmatch(r"(\d+(?:\.\d+)?) g", amount)
        g = float(m.group(1)) if m else food["misread"].get(amount, 100)
        raw = {"foodId": food_id, "foodLabel": {}, "foodWeights": food["w"],
               "calories": str(round(g * food.get("kcal_per_g", 1)))}
        if not food.get("no_weight"):
            raw["gmWgt"] = str(g)
        return FoodEntry(food_id=food_id, description=food_id, raw=raw)


def resolve(food, qty, unit=None):
    line = {"food": {"name": food}, "quantity": qty}
    if unit:
        line["unit"] = {"name": unit}
    mr = MealieRecipe.from_api({"name": "T", "recipeIngredient": [line]})
    return map_mealie_recipe(SmartMnd(), mr, existing={}).resolutions[0]


@pytest.mark.parametrize("food,qty,unit,grams", [
    ("chicken thigh", 16, None, "1200"),     # count -> 16 x thigh(75 g), not 16 g
    ("minced beef", 1.3, "kg", "1300"),      # kg sent as grams
    ("chicken stock", 1200, "ml", "1200"),   # misread volume -> 1 g/ml
    ("egg", 3, None, "132"),                 # count prefers 'medium'
    ("butter bean canned", 4, "can", "960"), # food's own can measure
    ("spinach", 2, "cups", "60"),            # food's cup measure agrees with MND
    ("flour", 3, "tbsp", "23"),              # plausible density (0.52 g/ml) accepted
])
def test_amount_corrected_and_verified(food, qty, unit, grams):
    r = resolve(food, qty, unit)
    assert r.matched and not r.amount_issue, r.note
    assert float(r.grams) == float(grams)


@pytest.mark.parametrize("food,qty,unit,why", [
    ("tikka curry paste", 5, "tbsp", "no tbsp measure"),  # ~74 g, ~300 kcal: blocks
    ("butter bean", 4, "can", "per can"),
    ("basmati rice", 1, None, "no count measure"),
])
def test_unverifiable_amount_needs_review(food, qty, unit, why):
    r = resolve(food, qty, unit)
    assert r.amount_issue and why in r.amount_issue
    assert r.needs_review


def test_mass_without_returned_weight_is_accepted():
    r = resolve("dark soy sauce", 30, "g")
    assert r.matched and not r.amount_issue and r.amount == "30 g"


def test_corrections_are_explained_in_the_note():
    r = resolve("chicken thigh", 16)
    assert r.amount == "1200 g"
    assert "16 × thigh (75 g)" in r.note and "read '16' as 16 g" in r.note


def test_first_amount_sends_mass_as_grams():
    assert first_amount(1.3, "kg", "1.3 kg") == "1300 g"
    assert first_amount(1.5, "lb", "1.5 lb") == "680 g"
    assert first_amount(2, "cup", "2 cup") == "2 cup"


def test_expected_grams_without_measures():
    assert expected_grams(2, "medium", {"gmWgt": "236"}).expected_g == 236
    chk = expected_grams(2, None, {"gmWgt": "2", "foodWeights": weights(gram=1, oz=28.3)})
    assert chk.expected_g is None and "no count measure" in chk.review


def test_flagged_amount_sends_estimate():
    r = resolve("butter bean", 4, "can")  # 130 g reading rejected
    assert r.amount == "1600 g"            # 4 x 400 g estimate sent instead
    assert "estimated at 400 g per can" in r.note


def test_negligible_calorie_estimate_does_not_block():
    r = resolve("frozen chopped ginger", 4, "tsp")
    assert not r.amount_issue and not r.needs_review
    assert r.amount == "20 g" and "negligible" in r.note


def test_rounding_slack_accepts_small_amounts():
    r = resolve("olive oil", 1, "tsp")  # expected 4.5 g, MND says 5 g
    assert not r.amount_issue and r.amount == "1 tsp"


class AltMnd(SmartMnd):
    """Top match has no count measure; an equally named alternative does."""

    def search_foods(self, query, limit=30):
        return [FoodSearchResult(food_id="parsnip-a", description="Parsnip", raw=SYS),
                FoodSearchResult(food_id="parsnip-b", description="Parsnips", raw=SYS),
                FoodSearchResult(food_id="far-off", description="Parsnip crisps", raw=SYS)]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        w = weights(gram=1, cup=133) if food_id == "parsnip-a" else weights(medium=170, gram=1)
        m = re.fullmatch(r"(\d+(?:\.\d+)?) g", amount)
        g = float(m.group(1)) if m else 4.0
        return FoodEntry(food_id=food_id, description=food_id, raw={
            "foodId": food_id, "foodLabel": {}, "foodWeights": w,
            "gmWgt": str(g), "calories": str(round(g * 0.75))})


def test_alternate_candidate_with_usable_measure_is_used():
    mr = MealieRecipe.from_api({"name": "T", "recipeIngredient": [
        {"food": {"name": "parsnip"}, "quantity": 4}]})
    r = map_mealie_recipe(AltMnd(), mr, existing={}).resolutions[0]
    assert r.mnd_food_id == "parsnip-b" and not r.amount_issue
    assert r.amount == "680 g"
    assert "instead of top match 'Parsnip'" in r.note


# ----- fourth live run: ready recipes that were silently wrong -----

class LiveMnd(SmartMnd):
    FOODS = {
        **SmartMnd.FOODS,
        "milk": dict(w=weights(quart=976, cup=244, gram=1), misread={"1": 1}),
        "broccoli": dict(w=weights(spear=31, head=608, cup=91, gram=1), misread={"2 whole": 91}),
        "noodle": dict(w=weights(gram=1, cup=160), misread={"8 nest": 160}),
        # returns no weight and the same calories whatever the amount
        "beef stir fry strips": dict(w=weights(gram=1), misread={}, no_weight=True, fixed_kcal=266),
    }

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        e = super().get_food_entry(food_id, amount, meal_type_id)
        fixed = self.FOODS[food_id].get("fixed_kcal")
        if fixed is not None:
            e.raw["calories"] = str(fixed)
        return e


def live(food, qty, unit=None):
    line = {"food": {"name": food}, "quantity": qty}
    if unit:
        line["unit"] = {"name": unit}
    mr = MealieRecipe.from_api({"name": "T", "recipeIngredient": [line]})
    return map_mealie_recipe(LiveMnd(), mr, existing={}).resolutions[0]


def test_quart_is_not_a_count():
    r = live("milk", 1)  # was sent as 1 quart = 976 g
    assert r.amount_issue and "no count measure" in r.amount_issue


def test_whole_prefers_whole_item_over_part():
    r = live("broccoli", 2, "whole")  # was 2 x spear = 62 g
    assert not r.amount_issue and r.amount == "1216 g"
    assert "head" in r.note


def test_unknown_unit_is_flagged_not_trusted():
    r = live("noodle", 8, "nest")  # was accepted as 160 g
    assert r.amount_issue and "isn't a measure" in r.amount_issue


def test_ignored_amount_detected_via_calories_per_100g():
    r = live("beef stir fry strips", 750, "g")  # 266 kcal for 750 g
    assert r.amount_issue and "ignores the amount" in r.amount_issue
    assert "266 kcal for both 750 g and 100 g" in r.amount_issue
    assert r.needs_review


def test_missing_weight_with_scaling_calories_is_fine():
    r = resolve("dark soy sauce", 30, "g")  # no weight, but kcal scale with amount
    assert not r.amount_issue



def test_negligible_rule_does_not_hide_an_ignored_amount():
    class StockMnd(LiveMnd):
        FOODS = {**LiveMnd.FOODS,
                 "beef stock": dict(w=weights(gram=1), misread={}, no_weight=True, fixed_kcal=20)}
    mr = MealieRecipe.from_api({"name": "T", "recipeIngredient": [
        {"food": {"name": "beef stock"}, "quantity": 320, "unit": {"name": "ml"}}]})
    r = map_mealie_recipe(StockMnd(), mr, existing={}).resolutions[0]
    assert r.amount_issue and "ignores the amount" in r.amount_issue  # 20 kcal, still flagged
