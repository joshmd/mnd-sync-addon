"""Log the Mealie meal plan (today..today+13) into the MyNetDiary diary and keep it following the plan."""

import logging
from datetime import date, timedelta

from app.service import CHANGED, describe
from app.store import PLAN_FAILED, PLAN_LOGGED, PLAN_NOTE, PLAN_USER_REMOVED, PLAN_WAITING
from mynetdiary.errors import MndAuthError
from sync.batch import SAVED

log = logging.getLogger("mnd_sync")

MEALS = {"breakfast": 1, "lunch": 2, "dinner": 3, "side": 3, "snack": 4, "dessert": 4, "drink": 4, "brunch": 3}


def _remove_ours(mnd, row) -> bool:
    """Delete our diary entry only if its entryNo still holds our food; False if the user removed it."""
    name = mnd.day_entries(row["mnd_day"]).get(row["entry_no"])
    if row["food_name"] is None or name not in (row["food_name"], row["food_name"] + " (deleted)"):
        return False  # " (deleted)": the recipe was retired since
    mnd.delete_food_entry(row["mnd_day"], row["entry_no"], dry_run=False)
    return True


def sync_plan(store, mnd, mealie, today: date) -> dict[str, int]:
    t, end = today.isoformat(), (today + timedelta(days=13)).isoformat()
    items = mealie.list_mealplans(t, end)  # raises before anything changes
    live = {it["id"]: (it["date"], MEALS.get(it["entryType"], 3), (it.get("recipe") or {}).get("slug"),
                       (it.get("recipe") or {}).get("name") or it.get("title") or "")
            for it in items}
    counts = dict.fromkeys(("logged", "moved", "removed", "waiting", "user_removed", "failed"), 0)
    for pid, (day, meal, slug, title) in live.items():
        try:
            _sync_item(store, mnd, pid, day, meal, slug, title, t, counts)
        except MndAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("meal plan item %s failed (%s)", pid, describe(e))
            if (store.get_plan_entry(pid) or {}).get("state") != PLAN_LOGGED:
                store.put_plan_entry(pid, date=day, meal=meal, slug=slug, title=title, state=PLAN_FAILED)
            counts["failed"] += 1
    for row in store.list_plan_entries(t):
        if row["plan_id"] in live:
            continue
        try:
            if row["state"] == PLAN_LOGGED:
                _remove_ours(mnd, row)  # False: the user already deleted it
            store.delete_plan_entry(row["plan_id"])
            counts["removed"] += 1
        except MndAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("meal plan removal %s failed (%s)", row["plan_id"], describe(e))
            counts["failed"] += 1
    return counts


def _sync_item(store, mnd, pid, day, meal, slug, title, t, counts) -> None:
    row = store.get_plan_entry(pid)
    if row and (row["state"] == PLAN_USER_REMOVED or row["date"] < t):
        return  # the user wins; the past is frozen
    recipe = store.get_recipe(slug) if slug else None
    food = recipe["mnd_food_id"] if recipe and recipe["status"] in (SAVED, CHANGED) else None
    plan = dict(date=day, meal=meal, slug=slug, title=title)
    if row and row["state"] == PLAN_LOGGED:
        if (row["date"], row["meal"], row["slug"], row["food_id"]) == (day, meal, slug, food):
            return
        if not _remove_ours(mnd, row):  # a new recipe version (food changed) is a move too
            store.put_plan_entry(pid, state=PLAN_USER_REMOVED)
            counts["user_removed"] += 1
            return
        store.put_plan_entry(pid, state=PLAN_FAILED, entry_no=None)  # gone now: a rerun must not look for it
        counts["moved"] += 1
    if slug is None:
        store.put_plan_entry(pid, **plan, state=PLAN_NOTE, entry_no=None)
    elif not food:
        store.put_plan_entry(pid, **plan, state=PLAN_WAITING, entry_no=None)
        counts["waiting"] += not row or row["state"] != PLAN_WAITING
    else:
        n, name = mnd.log_on_day(food, day, meal, "1 serving", dry_run=False)
        store.put_plan_entry(pid, **plan, state=PLAN_LOGGED, mnd_day=day, entry_no=n, food_name=name, food_id=food)
        counts["logged"] += 1
