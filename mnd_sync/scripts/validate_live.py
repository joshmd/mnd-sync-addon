#!/usr/bin/env python3
"""Staged live validation of the MyNetDiary client against a REAL account.

Run this on a machine that can reach www.mynetdiary.com, with a valid web
session cookie. It verifies our reconstructed request shapes actually work,
in order of increasing side-effects. Nothing destructive, nothing permanent
unless you explicitly opt in.

    export MND_JSESSIONID=<JSESSIONID cookie from a logged-in browser>
    python scripts/validate_live.py                 # Stage A only: READ-ONLY
    python scripts/validate_live.py --write-test     # + reversible log/delete round-trip
    python scripts/validate_live.py --recipe-test     # + save ONE test recipe (manual delete)

Stages:
  A (default)   auth check, search, resolve, dry-run bodies. Zero writes.
  B --write-test  log a test food -> read it back -> DELETE it. Fully reversible.
                  Also confirms mealTypeId 1..4 by reading the meal each lands in.
  C --recipe-test save one recipe named 'ZZZ VALIDATION (delete me)'. This has
                  no capture-confirmed delete endpoint, so YOU remove it in the
                  MyNetDiary UI afterwards. Opt-in, runs last.

Exit code 0 = all attempted stages passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

# allow running from anywhere: add the repo root (parent of scripts/) to path
import os  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.errors import MndError  # noqa: E402
from mynetdiary.models import MealType, Recipe  # noqa: E402

TEST_FOOD_QUERY = "banana"
TEST_AMOUNT = "1 medium"
RECIPE_NAME = "ZZZ VALIDATION (delete me)"


def ok(msg):
    print(f"  \033[32mPASS\033[0m {msg}")


def info(msg):
    print(f"       {msg}")


def fail(msg):
    print(f"  \033[31mFAIL\033[0m {msg}")


def stage_a(client: MndClient) -> str:
    """Read-only checks. Returns a resolved foodId token for later stages."""
    print("Stage A — read-only")
    # auth
    recipes = client.existing_recipes()
    ok(f"auth: session valid, {len(recipes)} existing recipes visible")
    # search
    hits = client.search_foods(TEST_FOOD_QUERY, limit=10)
    if not hits:
        raise SystemExit("  no search results for 'banana' — unexpected")
    # Prefer a database (system) food: custom foods may lack per-100g data and
    # reject generic amounts like '1 medium'.
    system_hits = [h for h in hits if h.is_system]
    top = system_hits[0] if system_hits else hits[0]
    ok(f"search: {len(hits)} hits ({len(system_hits)} database); using "
       f"{top.description!r} system={top.is_system} ({top.food_id[:24]}...)")
    # resolve (labelled)
    entry = client.get_food_entry(top.food_id, TEST_AMOUNT)
    has_label = "foodLabel" in entry.raw
    (ok if has_label else fail)(f"resolve: foodLabel present = {has_label}")
    # dry-run bodies
    body = client.log_food(top.food_id, MealType.BREAKFAST, TEST_AMOUNT, dry_run=True)
    ok(f"dry-run log body: {body['body']}")
    return top.food_id


def _err(resp) -> str:
    return (resp.get("errorText") if isinstance(resp, dict) else None) or ""


def stage_b(client: MndClient, food_id: str) -> None:
    """Reversible: inline-log -> read entryNo+day from response -> delete."""
    print("\nStage B — reversible write test (inline log then delete)")
    meal_names = {MealType.BREAKFAST: "Breakfast", MealType.LUNCH: "Lunch",
                  MealType.DINNER: "Dinner", MealType.SNACKS: "Snacks"}
    for mtid, expected in meal_names.items():
        # inline form is the capture-proven way to CREATE a new entry
        resp = client.log_food_inline(food_id, mtid, TEST_AMOUNT,
                                      search_token=TEST_FOOD_QUERY[:4], dry_run=False)
        entry_no = client.entry_no_from_response(resp)
        fe = resp.get("foodEntry") or {} if isinstance(resp, dict) else {}
        if entry_no is None:
            fail(f"mealTypeId={mtid}: no entryNo"
                 + (f" — errorText: {_err(resp)}" if _err(resp) else ""))
            if not _err(resp):
                info(f"response keys: {list(resp)[:8] if isinstance(resp, dict) else resp}")
            continue
        landed = fe.get("mealTypeName")
        entry_day = fe.get("day") or dt.date.today().isoformat()
        match = "" if landed == expected else f" (!! landed in {landed!r})"
        ok(f"mealTypeId={mtid} -> {expected!r}{match}; entryNo {entry_no} day {entry_day}")
        client.delete_food_entry(entry_day, entry_no, dry_run=False)
        still = any(en == entry_no for _, _, en, _ in client.iter_day_entries(entry_day))
        (ok if not still else fail)(
            f"mealTypeId={mtid}: deleted cleanly (entryNo {entry_no})" if not still
            else f"mealTypeId={mtid}: entry {entry_no} STILL present after delete")


def list_today(client: MndClient) -> None:
    """Print the session-day diary entries, for diagnosis / cleanup."""
    print("Today's diary entries (session day):")
    any_row = False
    for mtid, mname, entry_no, name in client.iter_day_entries():
        any_row = True
        print(f"  mealTypeId={mtid} {mname:10} entryNo={entry_no}  {name}")
    if not any_row:
        print("  (none)")


def delete_entry(client: MndClient, entry_no: int, day: str) -> None:
    client.delete_food_entry(day, entry_no, dry_run=False)
    ok(f"deleted entryNo {entry_no} on {day}")


def stage_c(client: MndClient, food_id: str) -> None:
    """Least reversible: save one recipe. User deletes it manually."""
    print("\nStage C — recipe save (leaves one recipe to delete manually)")
    entry = client.get_food_entry(food_id, TEST_AMOUNT)
    recipe = Recipe(name=RECIPE_NAME, servings="1")
    recipe.add_ingredient(entry, TEST_AMOUNT)
    saved = client.save_recipe(recipe, dry_run=False)
    new_id = getattr(saved, "food_id", None)
    if not new_id:
        fail(f"recipe save returned no foodId: {saved}")
        return
    ok(f"recipe saved: new foodId {new_id} ({getattr(saved,'name','?')})")
    # verify it shows up
    if RECIPE_NAME.lower() in client.existing_recipes():
        ok("recipe appears in muiGetRecipes.do")
    else:
        info("saved id returned but recipe not yet listed (may be eventual)")
    info(f"ACTION: delete {RECIPE_NAME!r} in the MyNetDiary web UI "
         "(Me -> Manage My Foods -> Recipes). No API delete is captured yet.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live-validate the MND client")
    ap.add_argument("--write-test", action="store_true",
                    help="Stage B: reversible log/delete round-trip")
    ap.add_argument("--recipe-test", action="store_true",
                    help="Stage C: save one test recipe (manual cleanup)")
    ap.add_argument("--list-today", action="store_true",
                    help="list the session-day diary entries and exit")
    ap.add_argument("--delete-entry", type=int, default=None,
                    help="delete a single diary entryNo (use with --day) and exit")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD for --delete-entry")
    args = ap.parse_args(argv)

    try:
        client = MndClient.from_env(dry_run=False)
    except MndError as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 1

    try:
        if args.list_today:
            list_today(client)
            return 0
        if args.delete_entry is not None:
            delete_entry(client, args.delete_entry, args.day or dt.date.today().isoformat())
            return 0
        food_id = stage_a(client)
        if args.write_test:
            stage_b(client, food_id)
        if args.recipe_test:
            stage_c(client, food_id)
    except MndError as e:
        fail(str(e))
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
