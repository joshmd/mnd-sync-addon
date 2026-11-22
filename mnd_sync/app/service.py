"""SyncService: keeps per-recipe state in SQLite and saves recipes to MyNetDiary exactly once."""

import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import requests

from mealie.client import MealieError
from mynetdiary.errors import MndAuthError, MndError
from sync.aliases import normalize_key
from sync.batch import READY, REVIEW, SAVED, ERROR, classify, sync_all
from sync.mapper import map_mealie_recipe
from sync.serialize import content_hash, result_to_json

CHANGED, REMOVED = "changed", "removed"
log = logging.getLogger("mnd_sync")


def error_kind(e: Exception) -> str:
    """Which side failed: "mealie", "mnd" (MyNetDiary) or "error" (anything else)."""
    if isinstance(e, MealieError):
        return "mealie"
    if isinstance(e, MndError):
        return "mnd"
    if isinstance(e, requests.RequestException):  # the Mealie client wraps its own; only the MyNetDiary login does not
        url = getattr(getattr(e, "request", None), "url", None) or ""
        return "mnd" if "mynetdiary.com" in url else "error"
    return "error"


def describe(e: Exception) -> str:
    """Exception type and HTTP status for the log; never the message, which can carry URLs or tokens."""
    status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
    if status is None and isinstance(e, MealieError):
        found = re.search(r"HTTP (\d{3})", str(e))
        status = found and found.group(1)
    return type(e).__name__ + (f", HTTP {status}" if status else "")


class SyncBusy(Exception):
    """A refresh or sync is already running."""


def _marker(recipe) -> str:
    return recipe.updated_at or content_hash(recipe.raw)


class SyncService:
    def __init__(self, store, mnd, mealie):
        self.store, self.mnd, self.mealie = store, mnd, mealie
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @contextmanager
    def _guard(self):
        if not self._lock.acquire(blocking=False):
            raise SyncBusy("a refresh or sync is already running")
        try:
            yield
        finally:
            self._lock.release()

    def _retry(self, op):
        """Run op(mnd_client); on a dead session log in again and retry once."""
        try:
            return op(self.mnd.client())
        except MndAuthError:
            return op(self.mnd.relogin())

    def _put(self, slug, **changes) -> None:
        row = self.store.get_recipe(slug) or {"slug": slug, "name": slug, "mealie_updated": None,
                                              "mapping_json": None, "status": ERROR, "mnd_food_id": None,
                                              "saved_at": None, "saved_mealie_updated": None, "mealie_id": None}
        row.update(changes)
        self.store.upsert_recipe(**{k: row[k] for k in (
            "slug", "name", "mealie_updated", "mapping_json", "status", "mnd_food_id",
            "saved_at", "saved_mealie_updated", "mealie_id")})

    def _map(self, client, mrecipe, existing, mealie_id=None) -> None:
        result = map_mealie_recipe(client, mrecipe, aliases=self.store.alias_map_for(mrecipe.slug),
                                   existing=existing)
        self._put(mrecipe.slug, name=mrecipe.name, mealie_updated=_marker(mrecipe),
                  mealie_id=mealie_id or mrecipe.raw.get("id"), mapping_json=result_to_json(result), status=classify(result, existing))
        self.store.mark_stale([mrecipe.slug], False)

    def _saved(self, slug) -> bool:
        row = self.store.get_recipe(slug)
        return bool(row and (row["mnd_food_id"] or row["status"] in (SAVED, CHANGED)))

    def _isolate(self, slug, name, fn) -> None:
        """One bad recipe must not stop the run; auth errors still propagate for the relogin retry."""
        try:
            fn()
        except MndAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("recipe %s failed (%s)", slug, describe(e))
            if not self._saved(slug):
                self._put(slug, name=name, status=ERROR)

    # ----- refresh / remap --------------------------------------------------

    def refresh(self) -> dict:
        with self._guard():
            return self._retry(self._refresh)

    def _refresh(self, client) -> dict:
        counts = dict.fromkeys(("new", "linked", "remapped", "renamed", "changed", "removed"), 0)
        first_run = not any(self._saved(r["slug"]) for r in self.store.list_recipes())
        existing = client.existing_recipes()
        mealie = self.mealie.client()
        live = set()
        for summary in mealie.list_all_recipes():
            slug = summary["slug"]
            live.add(slug)
            self._isolate(slug, summary.get("name") or slug,
                          lambda: self._refresh_one(client, mealie, summary, existing, counts))
        for row in self.store.list_recipes():
            if row["slug"] not in live and row["status"] != REMOVED:
                self._put(row["slug"], status=REMOVED)
                counts["removed"] += 1
        # nothing in MyNetDiary to link to on a first run: maybe a wrong account, so everything would be saved again
        counts["check_links"] = first_run and not existing and bool(live)
        return counts

    def _refresh_one(self, client, mealie, summary, existing, counts) -> None:
        slug = summary["slug"]
        mrecipe = mealie.get_recipe(slug)
        mid = summary.get("id") or mrecipe.raw.get("id")
        stored = self.store.get_recipe(slug)
        renamed = False
        if stored is None and mid:  # Mealie changes the slug on rename; the id is stable
            old = next((r for r in self.store.list_recipes() if r["mealie_id"] == mid), None)
            if old:
                self.store.rename_recipe(old["slug"], slug)
                stored, renamed = self.store.get_recipe(slug), True
                counts["renamed"] += 1
        marker = _marker(mrecipe)
        if stored and stored["mealie_id"] != mid:
            self._put(slug, mealie_id=mid)
        if stored and self._saved(slug):  # saved before: never re-map, only track edits
            status = SAVED if marker == stored["saved_mealie_updated"] and not renamed else CHANGED
            counts["changed"] += status == CHANGED and stored["status"] != CHANGED
            self._put(slug, name=mrecipe.name, mealie_updated=marker, status=status)
        elif stored is None and mrecipe.name.strip().lower() in existing:  # saved earlier, e.g. by the CLI
            self._put(slug, name=mrecipe.name, mealie_updated=marker, status=SAVED, mealie_id=mid,
                      mnd_food_id=existing[mrecipe.name.strip().lower()], saved_mealie_updated=marker)
            counts["linked"] += 1
        elif (stored is None or renamed or stored["mealie_updated"] != marker
              or stored["status"] in (ERROR, REMOVED) or stored["stale"]):
            self._map(client, mrecipe, existing, mid)
            counts["new" if stored is None else "remapped"] += 1

    def remap(self, slug) -> None:
        self._remap_marked([slug])

    def remap_using(self, ingredient_name) -> list[str]:
        return self.remap_many([ingredient_name])

    def remap_many(self, ingredient_names) -> list[str]:
        """Re-map, once each, every unsaved recipe whose cached mapping mentions any of these ingredients."""
        keys = {normalize_key(n) for n in ingredient_names}
        return self._remap_marked([
            r["slug"] for r in self.store.list_recipes()
            if r["mapping_json"] and r["status"] != REMOVED
            and any(keys & {normalize_key(x["ingredient"]), normalize_key(x["query"])}
                    for x in json.loads(r["mapping_json"])["resolutions"])])

    def _remap_marked(self, slugs) -> list[str]:
        """Mark stale first (store only), so a busy or failed re-map still happens on the next refresh."""
        slugs = [s for s in slugs if self.store.get_recipe(s) and not self._saved(s)]
        self.store.mark_stale(slugs)
        with self._guard():
            self._retry(lambda c: self._remap(c, slugs))
        return slugs

    def _remap(self, client, slugs) -> None:
        existing = client.existing_recipes()
        mealie = self.mealie.client()
        for slug in slugs:
            if not self._saved(slug):
                name = (self.store.get_recipe(slug) or {}).get("name") or slug  # a failure keeps the shown name
                self._isolate(slug, name, lambda: self._map(client, mealie.get_recipe(slug), existing))

    def forget_saved(self, slug) -> None:
        """The user deleted a changed recipe in MyNetDiary: forget the save and map it again.

        If it is still there, the name check makes it a duplicate, never a second copy."""
        with self._guard():
            row = self.store.get_recipe(slug)
            if not row or row["status"] != CHANGED:
                raise ValueError("only a recipe that changed after it was saved can be forgotten")
            self._put(slug, status=ERROR, mnd_food_id=None, saved_at=None, saved_mealie_updated=None)
            self.store.mark_stale([slug])  # if MyNetDiary is down now, the next refresh maps it
            self._retry(lambda c: self._remap(c, [slug]))

    def update(self, slug, *, commit) -> list[str]:
        """Replace a changed recipe in MyNetDiary; returns the ingredient lines left out for review."""
        with self._guard():
            row = self.store.get_recipe(slug)
            if not row or row["status"] != CHANGED or not row["mnd_food_id"]:
                raise ValueError("only a recipe that changed after it was saved can be updated")
            result = self._retry(lambda c: map_mealie_recipe(
                c, self.mealie.client().get_recipe(slug), aliases=self.store.alias_map_for(slug), existing={}))
            if not (result.recipe and result.recipe.ingredients):
                raise ValueError("nothing to save: the recipe has no matched ingredients")
            self._put(slug, mapping_json=result_to_json(result))  # status stays changed
            lines = [r.source.display or r.query for r in result.resolutions if r.needs_review]  # covers unmatched
            if commit:
                old = row["mnd_food_id"]

                def save(c):
                    saved = c.update_recipe(old, result.recipe)
                    return saved or c.save_recipe(result.recipe, dry_run=False)  # in the diary: save a new copy

                new = self._retry(save).food_id
                self._put(slug, status=SAVED, mnd_food_id=new, saved_mealie_updated=row["mealie_updated"],
                          saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                if new != old:  # after the new id is stored: a failed retire leaves a copy, never a lost link
                    self._retry(lambda c: c.retire_food(old))
            return lines

    def delete_in_mnd(self, slug) -> None:
        """Retire the MyNetDiary recipe of a recipe removed from Mealie; past diary entries keep it."""
        with self._guard():
            row = self.store.get_recipe(slug)
            if not row or row["status"] != REMOVED or not row["mnd_food_id"]:
                raise ValueError("only a recipe removed from Mealie can be deleted in MyNetDiary")
            self._retry(lambda c: c.retire_food(row["mnd_food_id"]))
            self._put(slug, mnd_food_id=None, saved_at=None, saved_mealie_updated=None)

    # ----- sync -------------------------------------------------------------

    def sync(self, slugs=None, *, force=False, trigger="manual") -> dict:
        """Save ready recipes (review ones too with force). Never saves a slug twice."""
        with self._guard():
            run = self.store.start_run(trigger)
            out = {"saved": [], "skipped": [], "errors": []}
            try:
                self._retry(lambda c: self._sync(c, slugs, force, out))
            except Exception as e:
                self.store.finish_run(run, {k: len(v) for k, v in out.items()}, describe(e))  # text can carry URLs
                raise
            self.store.finish_run(run, {k: len(v) for k, v in out.items()}, None)
            return out

    @staticmethod
    def _note(out, kind, slug, item) -> None:
        """Record a slug under one result list only (a retry may revisit it)."""
        for k in out:
            out[k] = [x for x in out[k] if (x["slug"] if isinstance(x, dict) else x) != slug]
        out[kind].append(item)

    def _sync(self, client, slugs, force, out) -> None:
        if slugs is None:  # everything the page would offer; explicit slugs are the user's choice
            wanted = (READY, REVIEW) if force else (READY,)
            slugs = [r["slug"] for r in self.store.list_recipes() if r["status"] in wanted]
        eligible = [s for s in slugs if self.store.get_recipe(s) and not self._saved(s)
                    and self.store.get_recipe(s)["status"] != REMOVED]
        for s in slugs:
            if s not in eligible and s not in out["saved"]:  # first-attempt saves stay saved on a retry
                self._note(out, "skipped", s, s)
        if not eligible:  # sync_all(only=[]) would mean "everything"
            return

        def record(_i, _n, o):
            if o.status == SAVED:
                self._put(o.slug, name=o.name, status=SAVED, mnd_food_id=o.saved_food_id,
                          mapping_json=result_to_json(o.result),
                          saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          saved_mealie_updated=self.store.get_recipe(o.slug)["mealie_updated"])
                self._note(out, "saved", o.slug, o.slug)
            elif o.status == ERROR:
                self._put(o.slug, status=ERROR)
                self._note(out, "errors", o.slug, {"slug": o.slug, "error": o.error})
            else:
                self._put(o.slug, status=o.status, mapping_json=result_to_json(o.result))
                self._note(out, "skipped", o.slug, o.slug)

        sync_all(client, self.mealie.client(), commit=True, force=force, only=eligible,
                 aliases_for=self.store.alias_map_for, on_progress=record)
