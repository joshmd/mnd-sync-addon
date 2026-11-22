"""Unattended sync: refresh, save quiet ready recipes, notify about the rest."""

import logging
import threading
from datetime import datetime, timedelta, timezone

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


def tick(service, store, notifier, now: datetime) -> dict:
    try:
        service.refresh()
        due = [r["slug"] for r in store.list_recipes("ready")
               if should_autosave(r, now, store.get_setting("quiet_minutes"))]
        saved = service.sync(due, force=False, trigger="auto")["saved"] if due else []
    except SyncBusy:
        return {"error": "busy"}
    except MndAuthError:
        log.warning("auto-sync: MyNetDiary login failed")
        return _outage(store, notifier, "mnd_auth", "MyNetDiary login failed")
    except Exception as e:  # noqa: BLE001  # text may carry URLs: type and status only
        kind = error_kind(e)
        log.warning("auto-sync failed (%s)", describe(e))
        return _outage(store, notifier, kind, OUTAGES[kind])
    store.set_setting("last_outage", None)

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
    return {"saved": len(saved)}


def run_forever(service, store, notifier, stop=None, wait=None) -> None:
    stop = stop or threading.Event()
    wait = wait or (lambda minutes: stop.wait(minutes * 60))
    while not stop.is_set():
        minutes = 1  # a failed iteration retries soon
        try:
            if store.get_setting("auto_sync") == "on":
                tick(service, store, notifier, datetime.now(timezone.utc))
            minutes = max(1, store.get_setting("poll_minutes"))
        except Exception as e:  # noqa: BLE001  # the loop must never die
            log.warning("auto-sync iteration failed (%s)", type(e).__name__)
        wait(minutes)
