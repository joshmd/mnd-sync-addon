"""Unattended sync: refresh, save quiet ready recipes, notify about the rest."""

import logging
import threading
from datetime import date, datetime, timedelta, timezone

from mynetdiary.errors import MndAuthError

from .service import SyncBusy, describe, error_kind

log = logging.getLogger("mnd_sync")
TITLE = "MyNetDiary Sync"
OUTAGES = {"mealie": "Mealie unreachable", "mnd": "MyNetDiary unreachable",
           "error": "Sync error \u2014 see the add-on log"}


def should_autosave(recipe: dict, now: datetime, quiet_minutes: int) -> bool:
    if recipe["status"] != "ready" or recipe["saved_at"] or not recipe["changed_at"]:
        return False
    return now - datetime.fromisoformat(recipe["changed_at"]) >= timedelta(minutes=quiet_minutes)


def _plural(n: int, noun: str = "recipe") -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _outage(store, notifier, key: str, message: str) -> dict:
    if store.get_setting("last_outage") != key:  # once per outage, not once per tick
        notifier.send(TITLE, message)
        store.set_setting("last_outage", key)
    return {"error": key}


def _fail(store, notifier, e: Exception) -> dict:
    if isinstance(e, SyncBusy):
        return {"error": "busy"}
    if isinstance(e, MndAuthError):
        log.warning("auto-sync: MyNetDiary login failed")
        return _outage(store, notifier, "mnd_auth", "MyNetDiary login failed")
    kind = error_kind(e)  # text may carry URLs: type and status only
    log.warning("auto-sync failed (%s)", describe(e))
    return _outage(store, notifier, kind, OUTAGES[kind])


def _notify_recipes(store, notifier, saved: list) -> None:
    rows = {r["slug"]: r for r in store.list_recipes()}
    if saved:
        names = ", ".join(rows[s]["name"] for s in saved if s in rows)
        notifier.send(TITLE, f"{_plural(len(saved), 'new recipe')} synced to MyNetDiary: {names}")
    for status, what in (("review", "need review"), ("error", "failed to map")):
        todo = [r for r in rows.values() if r["status"] == status and r["notified"] != status]
        if todo:
            notifier.send(TITLE, f"{_plural(len(todo))} {what}: {', '.join(r['name'] for r in todo)}")
            for r in todo:
                store.set_notified(r["slug"], status)
    for r in rows.values():  # back to normal: forget, so a later return to review notifies again
        if r["notified"] and r["status"] not in ("review", "error"):
            store.set_notified(r["slug"], None)


def tick(service, store, notifier, now: datetime, *, recipes: bool = True, plan: bool = False) -> dict:
    saved, planned = [], {}
    try:
        if recipes:
            service.refresh()
            due = [r["slug"] for r in store.list_recipes("ready")
                   if should_autosave(r, now, store.get_setting("quiet_minutes"))]
            saved = service.sync(due, force=False, trigger="auto")["saved"] if due else []
    except Exception as e:  # noqa: BLE001
        return _fail(store, notifier, e)
    plan_error = None
    if plan:  # after the recipes, so entries whose recipe just synced are logged in the same tick
        try:
            planned = service.log_meal_plan(date.today())  # local date: HA gives add-ons TZ
        except Exception as e:  # noqa: BLE001
            plan_error = e
    if recipes:  # sent even when the plan part failed: the saves already happened
        _notify_recipes(store, notifier, saved)
    if plan_error:
        return _fail(store, notifier, plan_error)
    store.set_setting("last_outage", None)
    result = {"saved": len(saved)}
    if plan:
        moved = planned["moved"]
        added = max(planned["logged"] - moved, 0)  # a move whose meal became a note re-logs nothing
        result["planned"] = planned["logged"]
        if added:
            notifier.send(TITLE, f"{_plural(added, 'planned meal')} added to the MyNetDiary diary"
                                 + (f", {moved} moved" if moved else ""))
        elif moved:
            notifier.send(TITLE, f"{_plural(moved, 'planned meal')} moved in the MyNetDiary diary")
        if planned["waiting"]:
            n = planned["waiting"]
            notifier.send(TITLE, f"{_plural(n, 'planned meal')} {'waits' if n == 1 else 'wait'} for their recipe to sync: see Review")
    return result


def run_forever(service, store, notifier, stop=None, wait=None) -> None:
    stop = stop or threading.Event()
    wait = wait or (lambda minutes: stop.wait(minutes * 60))
    while not stop.is_set():
        minutes = 1  # a failed iteration retries soon
        try:
            recipes, plan = store.get_setting("auto_sync") == "on", store.get_setting("log_meal_plan") == "on"
            if recipes or plan:
                tick(service, store, notifier, datetime.now(timezone.utc), recipes=recipes, plan=plan)
            minutes = max(1, store.get_setting("poll_minutes"))
        except Exception as e:  # noqa: BLE001  # the loop must never die
            log.warning("auto-sync iteration failed (%s)", type(e).__name__)
        wait(minutes)
