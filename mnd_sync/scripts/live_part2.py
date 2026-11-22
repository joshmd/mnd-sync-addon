#!/usr/bin/env python3
"""Live check of the part-2 MyNetDiary calls against a REAL account.

Only touches items named 'ZZZ PART2...' and removes what it creates.

    MND_EMAIL=you@example.com python scripts/live_part2.py   # prompts for password
"""
import datetime as dt
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.login import mobile_login  # noqa: E402
from mynetdiary.models import MealType, Recipe  # noqa: E402

NAME = "ZZZ PART2 TEST"
DAY1 = (dt.date.today() + dt.timedelta(days=7)).isoformat()
DAY2 = (dt.date.today() + dt.timedelta(days=8)).isoformat()
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {label} {detail}")
    if not cond:
        failures.append(label)


def recipe(c, banana, amount, servings):
    r = Recipe(name=NAME, servings=servings)
    r.add_ingredient(c.get_food_entry(banana.food_id, amount), amount)
    return r


def kcal(c, food_id):
    return c.get_food_entry(food_id, "1 serving").raw.get("calories")


def entry_fields(c):
    print("1. Diary entry fields")
    banana = c.search_one("banana")
    n, name = c.log_on_day(banana.food_id, DAY1, MealType.DINNER, "1 medium")
    try:
        meals = c.get_meals(DAY1)["meals"]
        keys = [sorted(fe) for m in meals for fe in m["foodEntries"] if fe.get("entryNo") == n]
        print("  foodEntries keys:", keys)
        check("entry has entryNo and name", bool(name), f"(entryNo={n}, name={name!r})")
        check("day_entries maps entryNo", bool(c.day_entries(DAY1).get(n)))
    finally:
        c.delete_food_entry(DAY1, n, dry_run=False)
    check("entry gone after delete", n not in c.day_entries(DAY1))


def recipe_flow(c):
    print("2. Recipe in use, then updated")
    banana = c.search_one("banana")
    ids, entries = [], []
    try:
        fid = c.save_recipe(recipe(c, banana, "1 medium", "4"), dry_run=False).food_id
        ids.append(fid)
        for day in (DAY1, DAY2):
            entries.append((day, c.log_on_day(fid, day, MealType.DINNER, "1 serving")[0]))
        before = kcal(c, fid)
        saved = c.update_recipe(fid, recipe(c, banana, "2 medium", "8"), dry_run=False)
        if saved.food_id not in ids:
            ids.append(saved.food_id)
        check("update keeps same foodId (no fork)", saved.food_id == fid,
              f"(before={fid}, after={saved.food_id})")
        loaded = c._request("GET", "muiLoadRecipe.do", params={"recipeId": saved.food_id})["recipe"]
        print("  serving:", loaded.get("serving"), "entriesNum:", loaded.get("entriesNum"))
        check("servings took", str(loaded.get("serving")) == "8")
        print("  calories per 1 serving, before:", before, "after:", kcal(c, saved.food_id))
    finally:
        print("3. Retire")
        for day, n in entries:
            c.delete_food_entry(day, n, dry_run=False)
        for i in ids:
            c.retire_food(i, dry_run=False)
        check("recipe gone from existing_recipes",
              NAME.lower() not in c.existing_recipes())


def main() -> int:
    email = os.environ.get("MND_EMAIL") or input("MyNetDiary email: ").strip()
    password = getpass.getpass("MyNetDiary password (hidden): ")
    s = requests.Session()
    mobile_login(s, email, password)
    c = MndClient(session=s, dry_run=False)
    entry_fields(c)
    recipe_flow(c)
    print("FAIL: " + ", ".join(failures) if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
