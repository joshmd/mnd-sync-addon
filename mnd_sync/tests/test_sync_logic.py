"""Offline tests for the sync logic: units, matching, aliases, report."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.aliases import AliasMap, normalize_key  # noqa: E402
from sync.matching import Confidence, band, best_match, score  # noqa: E402
from sync.units import format_quantity, normalize_unit, to_mnd_amount  # noqa: E402


# ----- units -----

def test_normalize_unit_variants():
    assert normalize_unit("grams") == "g"
    assert normalize_unit("Gram") == "g"
    assert normalize_unit("tablespoons") == "tbsp"
    assert normalize_unit("cups") == "cup"
    assert normalize_unit("") == ""
    assert normalize_unit("fistfuls") == "fistfuls"  # unknown passes through


def test_format_quantity_fractions():
    assert format_quantity(2) == "2"
    assert format_quantity(0.5) == "1/2"
    assert format_quantity(1.5) == "1 1/2"
    assert format_quantity(0.25) == "1/4"
    assert format_quantity(None) == ""


def test_to_mnd_amount():
    assert to_mnd_amount(150, "g") == "150 g"
    assert to_mnd_amount(2, "cups") == "2 cup"
    assert to_mnd_amount(1.5, "tbsp") == "1 1/2 tbsp"
    assert to_mnd_amount(3, None) == "3"
    assert to_mnd_amount(None, "clove") == "1 clove"
    assert to_mnd_amount(None, None) == "1 serving"


# ----- matching -----

class _Cand:
    def __init__(self, food_id, description):
        self.food_id = food_id
        self.description = description


def test_score_prefers_exact_over_superset():
    assert score("banana", "Banana") > score("banana", "Banana bread")
    assert score("banana", "Banana") > score("banana", "strawberry yogurt")


def test_band_thresholds():
    assert band(0.95) is Confidence.HIGH
    assert band(0.6) is Confidence.MEDIUM
    assert band(0.2) is Confidence.LOW


def test_best_match_picks_closest():
    cands = [_Cand("1", "Banana bread"), _Cand("2", "Banana"),
             _Cand("3", "Banana chips")]
    m = best_match("banana", cands)
    assert m.candidate.food_id == "2"
    assert m.confidence is Confidence.HIGH


def test_best_match_empty():
    assert best_match("x", []) is None


# ----- aliases -----

def test_normalize_key():
    assert normalize_key("  Maida Flour! ") == "maida flour"


def test_aliasmap_roundtrip(tmp_path):
    p = tmp_path / "pantry.json"
    amap = AliasMap.load(p)
    assert len(amap) == 0
    amap.set("Maida", "40112233", "White flour, all purpose", amount="100 g")
    amap.save()
    # reload and confirm persisted + normalized key lookup
    amap2 = AliasMap.load(p)
    assert "maida" in amap2
    entry = amap2.get("  MAIDA ")
    assert entry.food_id == "40112233"
    assert entry.amount == "100 g"


# ----- system-food preference -----

class _SysCand(_Cand):
    def __init__(self, food_id, description, system):
        super().__init__(food_id, description)
        self.is_system = system


def test_prefers_database_food_over_close_custom():
    cands = [_SysCand("c", "custom banana", False), _SysCand("s", "Banana", True)]
    m = best_match("banana", cands)
    assert m.candidate.food_id == "s"
    assert m.is_system


def test_keeps_custom_when_no_decent_database_food():
    cands = [_SysCand("c", "Grandma's kale", False),
             _SysCand("s", "Strawberry yogurt", True)]
    m = best_match("grandma's kale", cands)
    assert m.candidate.food_id == "c"
    assert not m.is_system


def test_prefer_system_can_be_disabled():
    cands = [_SysCand("c", "Banana", False), _SysCand("s", "Banana", True)]
    assert best_match("banana", cands, prefer_system=False).candidate.food_id == "c"


# ----- Mealie servings parsing -----

from mealie.models import MealieRecipe  # noqa: E402


def test_servings_range_takes_first_number():
    assert MealieRecipe.from_api({"name": "x", "recipeYield": "4-6 servings"}).servings == "4"


def test_servings_prefers_numeric_field():
    mr = MealieRecipe.from_api({"name": "x", "recipeServings": 3, "recipeYield": "6"})
    assert mr.servings == "3"


def test_slug_kept():
    assert MealieRecipe.from_api({"name": "x", "slug": "my-x"}).slug == "my-x"



# ----- scoring against real mismatches seen in a live dry run -----

@pytest.mark.parametrize("query,wrong,right", [
    ("cheese", "Mac & cheese", "Cheddar cheese"),
    ("mint", "Mint tea", "Mint, fresh"),
    ("butter bean", "Cashew butter pure vanilla bean dark chocolate by hu", "Butter beans"),
    ("tomato", "Spinach & tomato quiche by higgidy", "Tomatoes"),
    ("red lentil", "Red Lentil Soup or Dal", "Lentils red"),
    ("chive", "Sour cream & chive snack by snack a jacks", "Chives"),
    ("potato", "Sweet potato", "Potatoes"),
])
def test_right_food_outranks_wrong_one(query, wrong, right):
    assert score(query, right) > score(query, wrong)


@pytest.mark.parametrize("query,wrong", [
    ("cheese", "Mac & cheese"), ("mint", "Mint tea"),
    ("butter bean", "Cashew butter pure vanilla bean dark chocolate by hu"),
    ("tomato", "Spinach & tomato quiche by higgidy"), ("red lentil", "Red Lentil Soup or Dal"),
])
def test_prepared_dish_mismatch_is_low_confidence(query, wrong):
    assert band(score(query, wrong)) is Confidence.LOW


@pytest.mark.parametrize("query,candidate", [
    ("pea", "Peas"), ("mixed herb", "Herbs mixed"), ("fusilli", "Fusilli by asda"),
    ("cooked chicken", "Chicken cooked"), ("tikka curry paste", "Tikka curry paste by tesco"),
])
def test_plurals_word_order_and_brands_match_exactly(query, candidate):
    assert score(query, candidate) == 1.0


def test_simplify_query():
    from sync.matching import simplify_query
    assert simplify_query("frozen chopped chilli") == "chili"
    assert simplify_query("Boneless skinless chicken thighs") == "chicken thighs"



def test_soft_drink_does_not_match_pepper():
    assert band(score("pepper", "Dr. Pepper")) is Confidence.LOW


def test_one_word_vs_two_word_name_is_reviewed():
    assert band(score("onion", "Red onion")) is not Confidence.HIGH
