#!/usr/bin/env python3
"""Probe why a recipe update returned no foodId: update with 0, then 2, diary entries.

Prints MyNetDiary's reply minus the account envelope (no tokens or emails).
Only touches 'ZZZ PART2 PROBE' and removes what it creates.

    $env:MND_EMAIL = "you@example.com"; python scripts/live_update_probe.py
"""
import datetime as dt
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.login import mobile_login  # noqa: E402
from mynetdiary.models import MealType, Recipe  # noqa: E402

NAME = "ZZZ PART2 PROBE"
DAYS = [(dt.date.today() + dt.timedelta(days=d)).isoformat() for d in (7, 8)]
ENVELOPE = {"securityToken", "diabetesActivated", "freeOrPro", "userType", "mealPlanAdmin", "mdConvention",
            "use24hr", "weekStartSunday", "userZoneOffset", "fastingsEnabled", "commonsConfiguration",
            "userSystemMessages", "webFeatureDiscovery", "webFeatureDiscoveryDisplayedDuringSession",
            "calendar", "dayTotals", "foodLabelSettings"}


def recipe(c, banana, amount, servings):
    r = Recipe(name=NAME, servings=servings)
    r.add_ingredient(c.get_food_entry(banana.food_id, amount), amount)
    return r


def update(c, fid, r, label):
    body = c.update_recipe(fid, r, dry_run=True)["body"]  # the exact request update_recipe would send
    reply = c._request("POST", "muiSaveRecipe.do", json=body)
    shown = {k: v for k, v in reply.items() if k not in ENVELOPE} if isinstance(reply, dict) else reply
    print(f"  {label}: {json.dumps(shown, default=str)[:1500]}")
    return str(reply.get("foodId") or "") if isinstance(reply, dict) else ""


def main() -> int:
    email = os.environ.get("MND_EMAIL") or input("MyNetDiary email: ").strip()
    s = requests.Session()
    mobile_login(s, email, getpass.getpass("MyNetDiary password (hidden): "))
    c = MndClient(session=s, dry_run=False)
    banana = c.search_one("banana")
    ids, entries = [], []
    try:
        fid = c.save_recipe(recipe(c, banana, "1 medium", "4"), dry_run=False).food_id
        ids.append(fid)
        print("A. update with 0 diary entries")
        a = update(c, fid, recipe(c, banana, "2 medium", "8"), "reply")
        for day in DAYS:
            entries.append((day, c.log_on_day(fid, day, MealType.DINNER, "1 serving")[0]))
        print("B. update with 2 diary entries")
        b = update(c, fid, recipe(c, banana, "3 medium", "8"), "reply")
        for x in (a, b):
            if x and x not in ids:
                ids.append(x)
        print(f"  ids seen: {ids}")
    finally:
        print("Cleanup")
        for day, n in entries:
            try:
                c.delete_food_entry(day, n)
            except Exception as e:  # noqa: BLE001  # keep cleaning up
                print(f"  FAIL delete entry {n}: {type(e).__name__}")
        for i in ids:
            try:
                c.retire_food(i)
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL retire {i}: {type(e).__name__}")
        print("  recipe gone:", NAME.lower() not in c.existing_recipes())
    return 0


if __name__ == "__main__":
    sys.exit(main())
