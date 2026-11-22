"""Other-day logging, recipe update in place and retire."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.errors import MndRequestError  # noqa: E402
from mynetdiary.models import Recipe  # noqa: E402

LOGGED_IN = {"userType": "Free", "securityToken": "x"}


class _Resp:
    def __init__(self, data):
        self._data, self.status_code, self.text = data, 200, str(data)

    def json(self):
        return self._data


class _Session:
    def __init__(self, *responses):
        self.queue = [{**LOGGED_IN, **r} for r in responses]
        self.calls, self.headers, self.cookies = [], {}, None

    def request(self, method, url, timeout=None, params=None, json=None, **kw):
        self.calls.append((method, url.rsplit("/", 1)[-1], params, json))
        return _Resp(self.queue.pop(0))


def client(*responses):
    s = _Session(*responses)
    return MndClient(session=s, dry_run=False), s


def test_get_meals_sends_date():
    c, s = client({"meals": []})
    c.get_meals("2026-11-30")
    params = s.calls[0][2]
    assert params["date"] == "2026-11-30" and "day" not in params


def test_log_on_day_switches_day_then_posts_full_form():
    c, s = client({"meals": []}, {"entryNo": 301, "meals": [
        {"mealTypeId": 3, "foodEntries": [{"entryNo": 301, "foodName": "Soup"}]}]})
    assert c.log_on_day("123", "2026-11-30", 3, "1 serving") == (301, "Soup")
    (m1, e1, p1, _), (m2, e2, _, body) = s.calls
    assert (m1, e1, p1["date"]) == ("GET", "muiGetMeals.do", "2026-11-30")
    assert (m2, e2) == ("POST", "muiSaveFoodEntry.do")
    assert set(body) == {"foodId", "day", "timeMinutes", "mealTypeId", "timeString",
                         "amountInputString", "searchToken"}
    assert (body["mealTypeId"], body["timeString"], body["timeMinutes"]) == (3, "19:00", 1140)


def test_log_on_day_without_entry_no_raises():
    c, _ = client({"meals": []}, {"meals": []})
    with pytest.raises(MndRequestError):
        c.log_on_day("123", "2026-11-30", 3, "1 serving")


def test_update_recipe_posts_loaded_recipe_with_id():
    c, s = client(
        {"recipe": {"foodId": 80309132, "name": "old", "foods": [1],
                    "serving": "serving", "entriesNum": 0}},
        {"foodId": "80309132"})
    new = Recipe(name="new", servings="8")
    saved = c.update_recipe("80309132", new)
    assert s.calls[0][:3] == ("GET", "muiLoadRecipe.do", {"recipeId": "80309132"})
    method, endpoint, _, body = s.calls[1]
    assert (method, endpoint, body["sortOrder"]) == ("POST", "muiSaveRecipe.do", "a")
    r = body["recipe"]
    assert r["foodId"] == 80309132 and isinstance(r["foodId"], int)
    assert r["name"] == "new" and r["foods"] == [] and r["entriesNum"] == 0
    assert r["serving"] == "8"
    assert saved.food_id == "80309132"


def test_retire_food():
    c, s = client({"success": True})
    c.retire_food("80306060")
    assert s.calls[0][:3] == ("GET", "muiRetireMyFood.do", {"foodId": "80306060"})
    c, _ = client({"success": False})
    with pytest.raises(MndRequestError):
        c.retire_food("80306060")
