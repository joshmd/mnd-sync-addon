"""MyNetDiary web API client (mui*.do JSON endpoints).

Auth is by web session cookie (JSESSIONID). Every write method honours
``dry_run``: when true (the client default) it builds and returns the exact
request that WOULD be sent, without sending it.

Endpoint shapes are verified against two HAR captures. Programmatic login is
not captured yet; supply an existing JSESSIONID via ``from_session_id`` or the
``MND_JSESSIONID`` env var.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .errors import MndAuthError, MndRequestError, MndSearchError
from .models import FoodEntry, FoodSearchResult, MealType, Recipe, SavedFood

BASE_URL = "https://www.mynetdiary.com"
_DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0"


def today_iso() -> str:
    return _dt.date.today().isoformat()


class MndClient:
    def __init__(self, session=None, *, base_url: str = BASE_URL,
                 dry_run: bool = True, timeout: float = 30.0):
        if requests is None:
            raise RuntimeError("The 'requests' package is required: pip install requests")
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", _DEFAULT_UA)
        self.session.headers.setdefault("Accept", "application/json, text/plain, */*")
        self.session.headers.setdefault("X-Requested-With", "XMLHttpRequest")

    # ----- construction / auth -------------------------------------------

    @classmethod
    def from_session_id(cls, jsessionid: str, **kwargs) -> "MndClient":
        c = cls(**kwargs)
        c.session.cookies.set("JSESSIONID", jsessionid, domain="www.mynetdiary.com")
        return c

    @classmethod
    def from_env(cls, **kwargs) -> "MndClient":
        sid = os.environ.get("MND_JSESSIONID")
        if not sid:
            raise MndAuthError(
                "No session: set MND_JSESSIONID (copy JSESSIONID from a logged-in "
                "browser) or use MndClient.from_session_id(...)."
            )
        return cls.from_session_id(sid, **kwargs)

    def login(self, logon_name: str, password: str) -> None:
        """TODO: web login to obtain a JSESSIONID (not captured yet)."""
        raise NotImplementedError(
            "Programmatic web login isn't captured yet; use from_session_id()."
        )

    # ----- low-level request helper --------------------------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:  # type: ignore[union-attr]
            raise MndRequestError(f"network error calling {endpoint}: {e}",
                                  endpoint=endpoint) from e
        if resp.status_code in (401, 403):
            raise MndAuthError(f"{endpoint} returned {resp.status_code}: session likely expired.")
        if resp.status_code >= 400:
            raise MndRequestError(f"{endpoint} returned HTTP {resp.status_code}",
                                  endpoint=endpoint, status=resp.status_code,
                                  payload=resp.text[:500])
        try:
            data = resp.json()
        except ValueError as e:
            raise MndRequestError(f"{endpoint} returned non-JSON response",
                                  endpoint=endpoint, status=resp.status_code,
                                  payload=resp.text[:500]) from e
        _check_session(endpoint, data)
        return data

    # ----- reads ---------------------------------------------------------

    def search_foods(self, query: str, *, limit: int = 20) -> list[FoodSearchResult]:
        """POST /muiFoodSearch.do  body {"searchToken": q, "skipLabel": true}."""
        body = {"searchToken": query, "skipLabel": True}
        data = self._request("POST", "muiFoodSearch.do", json=body)
        found = data.get("foundFoods", []) if isinstance(data, dict) else []
        results = [FoodSearchResult.from_payload(o) for o in found if o.get("foodId")]
        return results[:limit]

    def search_one(self, query: str) -> FoodSearchResult:
        hits = self.search_foods(query, limit=1)
        if not hits:
            raise MndSearchError(f"no food found for {query!r}", endpoint="muiFoodSearch.do")
        return hits[0]

    def get_food_entry(self, food_id: str, amount: str,
                       *, meal_type_id: int = MealType.BREAKFAST) -> FoodEntry:
        """GET /muiGetFoodEntry.do — resolve foodId + amount into a labelled food.

        Works for both opaque search tokens and saved numeric foodIds. This is
        the call that attaches the foodLabel needed to build a recipe ingredient.
        """
        params = {"mealTypeId": meal_type_id, "foodId": food_id,
                  "amountInputString": amount}
        data = self._request("GET", "muiGetFoodEntry.do", params=params)
        if isinstance(data, dict) and "foodEntry" not in data:
            _raise_if_error("muiGetFoodEntry.do", data)
        return FoodEntry.from_response(data)

    def calculate_entry_update(self, food_id: str, amount: str) -> Any:
        """POST /muiCalculateFoodEntryUpdate.do — recompute nutrition for amount."""
        body = {"foodId": food_id, "amountInputString": amount}
        return self._request("POST", "muiCalculateFoodEntryUpdate.do", json=body)

    def list_recipes(self) -> Any:
        """GET /muiGetRecipes.do — raw response (recipes under 'foods')."""
        return self._request("GET", "muiGetRecipes.do")

    def existing_recipes(self) -> dict[str, str]:
        """Map of existing recipe/meal name (lowercased) -> numeric foodId.

        Used for duplicate detection so a re-sync can skip or update instead of
        creating a second copy. muiGetRecipes.do returns items under 'foods'.
        """
        data = self.list_recipes()
        foods = data.get("foods", []) if isinstance(data, dict) else []
        out: dict[str, str] = {}
        for f in foods:
            name = (f.get("foodName") or f.get("foodDesc") or "").strip().lower()
            fid = f.get("foodId")
            if name and fid:
                out[name] = str(fid)
        return out

    def load_recipe_scaffold(self, meal_type_id: int = 3) -> Any:
        """GET /muiLoadRecipe.do — empty editor scaffold (image list etc.)."""
        return self._request("GET", "muiLoadRecipe.do", params={"mealTypeId": meal_type_id})

    def get_meals(self, day: Optional[str] = None) -> Any:
        """GET /muiGetMeals.do — a day's meals, each with foodEntries[].

        Used to read back a logged entry (to find its entryNo) and to confirm
        which meal an entry landed in. ``day`` defaults to the session's day.
        """
        params: dict[str, Any] = {"testMode": "false"}
        if day:
            params["day"] = day
        return self._request("GET", "muiGetMeals.do", params=params)

    @staticmethod
    def entry_no_from_response(resp: Any) -> Optional[int]:
        """Pull the created/edited entryNo out of a muiSaveFoodEntry.do response.

        The response carries it as foodEntry.entryNo (and sometimes a top-level
        entryNo). This is the reliable way to learn a new entry's id — far more
        so than re-reading the day, since muiGetMeals.do ignores a day parameter
        and a stored entry's foodId differs from the search token used to log it.
        """
        if not isinstance(resp, dict):
            return None
        fe = resp.get("foodEntry")
        if isinstance(fe, dict) and fe.get("entryNo") is not None:
            return fe["entryNo"]
        return resp.get("entryNo")

    def iter_day_entries(self, day: Optional[str] = None):
        """Yield (meal_type_id, meal_name, entryNo, food_name) for a day.

        Note: muiGetMeals.do returns the session's current day regardless of the
        ``day`` argument, so this reflects whatever day the session is on.
        """
        data = self.get_meals(day)
        for meal in (data.get("meals", []) if isinstance(data, dict) else []):
            mtid = meal.get("mealTypeId")
            mname = meal.get("mealTypeName")
            for fe in meal.get("foodEntries", []):
                yield (mtid, mname, fe.get("entryNo"),
                       fe.get("foodName") or fe.get("foodDesc"))

    # ----- writes (dry-run aware) ----------------------------------------

    def log_food(self, food_id: str, meal_type_id: int, amount: str, *,
                 day: Optional[str] = None, time_string: str = "0:01",
                 time_minutes: Optional[int] = 1, entry_no: Optional[int] = None,
                 input_string: str = "", dry_run: Optional[bool] = None) -> Any:
        """POST /muiSaveFoodEntry.do — log a food to a given day + meal.

        ``day`` is YYYY-MM-DD (defaults to today). ``entry_no`` identifies the
        diary row; omit to create a new entry, pass it to edit an existing one.
        """
        body: dict[str, Any] = {
            "foodId": food_id,
            "day": day or today_iso(),
            "timeMinutes": time_minutes,
            "mealTypeId": meal_type_id,
            "timeString": time_string,
            "amountInputString": amount,
            "inputString": input_string,
        }
        if entry_no is not None:
            body["entryNo"] = entry_no
            body["initialDay"] = day or today_iso()
        return self._maybe_send("POST", "muiSaveFoodEntry.do", body, dry_run)

    def log_food_inline(self, food_id: str, meal_type_id: int, amount: str,
                        *, search_token: str = "", dry_run: Optional[bool] = None) -> Any:
        """POST /muiSaveFoodEntry.do (inline form) — quick-add to current day."""
        body = {"inlineEntry": True, "mealTypeId": meal_type_id, "foodId": food_id,
                "amountInputString": amount, "searchToken": search_token}
        return self._maybe_send("POST", "muiSaveFoodEntry.do", body, dry_run)

    def delete_food_entry(self, day: str, entry_no: int,
                          *, dry_run: Optional[bool] = None) -> Any:
        """POST /muiDeleteFoodEntry.do  body {"day": ..., "entryNo": ...}."""
        body = {"day": day, "entryNo": entry_no}
        return self._maybe_send("POST", "muiDeleteFoodEntry.do", body, dry_run)

    def save_recipe(self, recipe: Recipe, *, dry_run: Optional[bool] = None) -> Any:
        """POST /muiSaveRecipe.do — create/update a recipe or meal.

        Returns a SavedFood (new numeric foodId) when sent; the request body
        dict when dry-run.
        """
        body = recipe.to_payload()
        result = self._maybe_send("POST", "muiSaveRecipe.do", body, dry_run)
        if self._is_dry(dry_run):
            return result
        _raise_if_error("muiSaveRecipe.do", result)
        saved = SavedFood.from_save_response(result)
        if not saved.food_id:
            raise MndRequestError("muiSaveRecipe.do returned no foodId",
                                  endpoint="muiSaveRecipe.do", payload=str(result)[:500])
        return saved

    def save_custom_food(self, custom_food: dict[str, Any], *,
                         dry_run: Optional[bool] = None) -> Any:
        """POST /muiSaveCustomFood.do  body {"customFood": {<NUTR>: {...}, ...}}.

        TODO: model the customFood nutrient map as a builder once needed.
        """
        body = {"customFood": custom_food}
        return self._maybe_send("POST", "muiSaveCustomFood.do", body, dry_run)

    # ----- high-level: build a recipe from (query, amount) pairs ----------

    def build_recipe(self, name: str, ingredients: list[tuple[str, str]], *,
                     servings: Optional[str] = None, meal_not_recipe: bool = False,
                     **recipe_kwargs) -> Recipe:
        """Resolve (search_query, amount) pairs into a ready-to-save Recipe.

        For each pair: search -> best hit -> get_food_entry(labelled) -> add.
        This is the backbone of the Mealie -> MND sync (Route 2). Read-only
        (no writes); call save_recipe(result) to persist.
        """
        recipe = Recipe(name=name, servings=servings,
                        meal_not_recipe=meal_not_recipe, **recipe_kwargs)
        for query, amount in ingredients:
            hit = self.search_one(query)
            entry = self.get_food_entry(hit.food_id, amount)
            recipe.add_ingredient(entry, amount)
        return recipe

    # ----- internals -----------------------------------------------------

    def _is_dry(self, dry_run: Optional[bool]) -> bool:
        return self.dry_run if dry_run is None else dry_run

    def _maybe_send(self, method: str, endpoint: str, body: dict[str, Any],
                    dry_run: Optional[bool]) -> Any:
        if self._is_dry(dry_run):
            return {"_dry_run": True, "method": method, "endpoint": endpoint, "body": body}
        return self._request(method, endpoint, json=body)


def _raise_if_error(endpoint: str, data: Any) -> None:
    """Raise MndRequestError if an mui*.do response carries errorText.

    MyNetDiary reports validation failures as HTTP 200 with an ``errorText``
    field (e.g. an amount a custom food can't accept), so status codes alone
    don't reveal them.
    """
    if isinstance(data, dict) and data.get("errorText"):
        raise MndRequestError(f"{endpoint}: {data['errorText']}",
                              endpoint=endpoint, payload=data["errorText"])


def _check_session(endpoint: str, data: Any) -> None:
    """Raise MndAuthError if a response isn't from a logged-in session.

    MyNetDiary does not answer an expired session with 401: it returns valid
    JSON with no data, which silently looks like "no search results". Every
    logged-in mui*.do response carries the account fields userType and
    securityToken (52 of 52 captured responses), so their absence means the
    session has ended.
    """
    if isinstance(data, dict) and "userType" not in data and "securityToken" not in data:
        keys = ", ".join(sorted(map(str, data))[:6]) or "none"
        raise MndAuthError(
            f"{endpoint} returned a response without your account details (keys: {keys}). "
            "Your MyNetDiary session has most likely expired: log in at mynetdiary.com, "
            "copy a fresh JSESSIONID cookie and set MND_JSESSIONID again.")
