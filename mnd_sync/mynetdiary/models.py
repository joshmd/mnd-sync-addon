"""Data models for MyNetDiary foods, ingredients, recipes and log entries.

Shapes mirror the captured mui*.do traffic:

- muiFoodSearch.do -> response.foundFoods[]  (lightweight; skipLabel:true, NO foodLabel)
- muiGetFoodEntry.do -> response.foodEntry   (full foodLabel + nutrition for a
  foodId + amount); this is how a search token becomes a labelled ingredient.
- muiSaveRecipe.do -> recipe.foods[] elements are a *mapped* form of a foodEntry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class MealType:
    """mealTypeId values, confirmed from muiGetMeals.do mealTypes[]."""

    BREAKFAST = 1
    LUNCH = 2
    DINNER = 3
    SNACKS = 4


@dataclass
class FoodSearchResult:
    """One entry from muiFoodSearch.do response.foundFoods[].

    Lightweight: carries no foodLabel (search is called with skipLabel:true).
    Call MndClient.get_food_entry(...) to resolve the full labelled food.
    """

    food_id: str
    description: str
    calories: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_opaque_token(self) -> bool:
        return self.food_id.startswith("v1.")

    @property
    def is_system(self) -> bool:
        """True for a MyNetDiary database food (has per-100g nutrition, so it
        accepts generic amounts like '1 medium' / '150 g'). User custom foods
        are system=False and may only accept their own serving unit."""
        return bool(self.raw.get("system"))

    @classmethod
    def from_payload(cls, obj: dict[str, Any]) -> "FoodSearchResult":
        return cls(
            food_id=obj.get("foodId", ""),
            description=obj.get("foodDesc") or obj.get("foodName") or "",
            calories=obj.get("calories"),
            raw=obj,
        )


@dataclass
class FoodEntry:
    """A fully-resolved food from muiGetFoodEntry.do response.foodEntry.

    Carries the foodLabel nutrition block, so it can be mapped into a recipe
    ingredient element.
    """

    food_id: str
    description: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, resp: dict[str, Any]) -> "FoodEntry":
        fe = resp.get("foodEntry", resp)
        return cls(food_id=fe.get("foodId", ""),
                  description=fe.get("foodDesc", ""), raw=fe)

    def to_recipe_food(self, amount: str, ordinal: int) -> dict[str, Any]:
        """Map a foodEntry into the foods[] element muiSaveRecipe.do expects.

        Observed recipe-food keys: foodId, foodDesc, foodLabel, calories,
        rawCalories, imageSrc, amount, carbs, ordinal, rawGmWgt. The server
        recomputes authoritative totals from foodId+amount, so this is a
        best-effort projection of the labelled entry.
        """
        fe = self.raw
        element: dict[str, Any] = {
            "foodId": fe.get("foodId"),
            "foodDesc": fe.get("foodDesc"),
            "amount": amount,
            "ordinal": ordinal,
        }
        for k in ("foodLabel", "calories", "rawCalories", "imageSrc", "carbs"):
            if k in fe:
                element[k] = fe[k]
        # recipe foods use rawGmWgt; foodEntry exposes gmWgt / rawGmWgt
        if "rawGmWgt" in fe:
            element["rawGmWgt"] = fe["rawGmWgt"]
        elif "gmWgt" in fe:
            try:
                element["rawGmWgt"] = float(fe["gmWgt"])
            except (TypeError, ValueError):
                pass
        return element


@dataclass
class Ingredient:
    """A recipe ingredient: a resolved labelled food plus a serving amount."""

    entry: FoodEntry
    amount: str
    ordinal: int = 1

    def to_recipe_food(self) -> dict[str, Any]:
        return self.entry.to_recipe_food(self.amount, self.ordinal)


@dataclass
class Recipe:
    """A recipe (reusable, has servings) or a meal (one-off bundle).

    meal_not_recipe=False -> recipe; True -> meal (omits ``serving``).
    """

    name: str
    ingredients: list[Ingredient] = field(default_factory=list)
    servings: Optional[str] = None
    meal_not_recipe: bool = False
    image_id: int = 14
    prep_time: Optional[str] = None
    cooking_time: Optional[str] = None
    cooking_instructions: Optional[str] = None

    def add_ingredient(self, entry: FoodEntry, amount: str) -> Ingredient:
        ing = Ingredient(entry=entry, amount=amount, ordinal=len(self.ingredients) + 1)
        self.ingredients.append(ing)
        return ing

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the ``{"recipe": {...}}`` body of muiSaveRecipe.do."""
        recipe: dict[str, Any] = {
            "mealNotRecipe": self.meal_not_recipe,
            "imageId": self.image_id,
            "name": self.name,
            "foods": [ing.to_recipe_food() for ing in self.ingredients],
            "findIngredient": False,
        }
        if not self.meal_not_recipe and self.servings is not None:
            recipe["serving"] = str(self.servings)
        if self.prep_time is not None:
            recipe["prepTime"] = self.prep_time
        if self.cooking_time is not None:
            recipe["cookingTime"] = self.cooking_time
        if self.cooking_instructions is not None:
            recipe["cookingInstructions"] = self.cooking_instructions
        return {"recipe": recipe}


@dataclass
class SavedFood:
    """Result of muiSaveRecipe.do: a permanent numeric foodId, loggable."""

    food_id: str
    name: str
    calories: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_save_response(cls, resp: dict[str, Any]) -> "SavedFood":
        mf = resp.get("recipeMyFood", {})
        return cls(
            food_id=str(resp.get("foodId", mf.get("foodId", ""))),
            name=mf.get("foodName") or mf.get("foodDesc") or "",
            calories=mf.get("calories"),
            raw=resp,
        )
