import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mealie.client import MealieError
from mynetdiary.errors import MndAuthError, MndError

from .autosync import tick
from .notify import Notifier, valid_service
from .connections import Connections
from .credentials import CredentialStore
from .service import SyncBusy, SyncService, describe, error_kind, log
from .store import (PLAN_FAILED, PLAN_LOGGED, PLAN_NOTE, PLAN_USER_REMOVED, PLAN_WAITING, Store)
from sync.aliases import normalize_key
from sync.amounts import grams_of, number
from sync.serialize import result_from_json

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


INGRESS_CLIENT = "172.30.32.2"  # the HA Supervisor's ingress proxy


def url(request: Request, path: str) -> str:
    """Prefix a path with HA ingress's X-Ingress-Path (ignored unless it looks like one)."""
    prefix = request.headers.get("X-Ingress-Path", "")
    return (prefix if prefix.startswith("/api/hassio_ingress/") else "") + path


def redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(url(request, path), status_code=303)


templates.env.globals["url"] = url
templates.env.filters["fromjson"] = json.loads


_FAILED = {"mealie": "Sync failed: Mealie unreachable", "mnd": "Sync failed: MyNetDiary unreachable",
           "error": "Sync failed \u2014 see the add-on log"}
_MEALS = {1: "Breakfast", 2: "Lunch", 3: "Dinner", 4: "Snacks"}
_PLAN_STATES = {PLAN_LOGGED: "in diary", PLAN_WAITING: "waiting for recipe to sync", PLAN_NOTE: "note only, not logged",
                PLAN_FAILED: "failed, will retry", PLAN_USER_REMOVED: "removed by you"}
_STATUSES = ("ready", "review", "saved", "changed", "error", "duplicate", "empty", "removed")
_SECRETS = ("mealie_token", "mnd_email", "mnd_password", "mnd_jsessionid")


def create_app(data_dir: Path, connections=None, service=None, notifier=None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def ingress_only(request: Request, call_next):
        """Only HA ingress may talk to the app (port 8099 is reachable from other add-ons)."""
        if os.environ.get("MND_ALLOW_ANY_CLIENT") != "1" and (request.client is None
                                                              or request.client.host != INGRESS_CLIENT):
            return PlainTextResponse("Forbidden: open this add-on from the Home Assistant sidebar.", status_code=403)
        return await call_next(request)

    app.state.data_dir = data_dir
    store = app.state.store = Store(data_dir / "mnd_sync.db")
    creds = app.state.creds = CredentialStore(data_dir / "credentials.json")
    conns = connections or Connections(store, creds)
    svc = app.state.service = service or SyncService(store, conns.mnd, conns.mealie)
    app.state.notifier = notifier or Notifier(os.environ.get("SUPERVISOR_TOKEN"),
                                              lambda: store.get_setting("notify_service"))

    def settings_page(request: Request, message=None):
        return templates.TemplateResponse(request, "settings.html", {
            "mealie_url": store.get_setting("mealie_url"),
            "notify_service": store.get_setting("notify_service") or "", "message": message,
            "saved": {n: creds.has(n) for n in _SECRETS}, "auto_sync": store.get_setting("auto_sync") == "on",
            "log_meal_plan": store.get_setting("log_meal_plan") == "on",
            "poll_minutes": store.get_setting("poll_minutes"), "quiet_minutes": store.get_setting("quiet_minutes"),
            "mealie_ok": store.get_setting("mealie_ok"), "mnd_ok": store.get_setting("mnd_ok")})

    def record_ok(name: str, ok: bool) -> None:
        """Remember each connection's last test, for display. Auto-sync is only ever switched on by the user."""
        store.set_setting(name, "1" if ok else None)

    def back(request, path, msg):
        return redirect(request, f"{path}?msg={quote(msg)}")

    def run(request, path, action, ok):
        """Run a service action and redirect with a short message (exception text can carry URLs: never shown)."""
        try:
            return back(request, path, ok(action()))
        except SyncBusy:
            return back(request, path, "A sync is already running")
        except MndAuthError:
            log.warning("MyNetDiary login failed")
            return back(request, path, "Sync failed: MyNetDiary login failed")
        except Exception as e:  # noqa: BLE001
            log.warning("action failed (%s)", describe(e))
            return back(request, path, _FAILED[error_kind(e)])

    def mapping(row):
        return result_from_json(row["mapping_json"]) if row["mapping_json"] else None

    def unresolved(row):
        m = mapping(row)
        return [r for r in m["resolutions"] if r["needs_review"] or not (r["matched"] or r["skipped"])] if m else []

    @app.get("/")
    @app.get("/recipes")
    def recipes(request: Request, msg: str = "", status: str = ""):
        status = status if status in _STATUSES else ""
        return templates.TemplateResponse(request, "recipes.html", {
            "recipes": store.list_recipes(status or None), "msg": msg, "busy": svc.busy,
            "last_run": store.last_run(), "last_refresh": store.get_setting("last_refresh"),
            "statuses": _STATUSES, "status": status})

    @app.get("/recipes/{slug}")
    def recipe(request: Request, slug: str, msg: str = ""):
        return recipe_page(request, slug, msg)

    def recipe_page(request, slug, msg="", ask_forget=False, ask_update=None, ask_delete=False):
        row = store.get_recipe(slug)
        if row is None:
            raise HTTPException(404, "No such recipe")
        m = mapping(row)
        for r in m["resolutions"] if m else []:  # kcal per 100 g, as the CLI report shows it
            r["grams"], r["calories"] = number(r["grams"]), number(r["calories"])  # MyNetDiary sends text, e.g. "438cals"
            r["per100"] = r["calories"] / r["grams"] * 100 if r["calories"] is not None and (r["grams"] or 0) >= 5 else None
        return templates.TemplateResponse(request, "recipe.html", {"row": row, "m": m, "msg": msg,
                                                                "ask_forget": ask_forget, "ask_update": ask_update,
                                                                "ask_delete": ask_delete})

    @app.post("/refresh")
    def refresh(request: Request):
        def ok(c):
            linked = "0 linked \u2014 check before turning on auto-sync" if c.get("check_links") else f"{c.get('linked', 0)} linked"
            rest = [f"{n} {k}" for k, n in c.items() if k not in ("new", "linked", "check_links") and n]
            msg = "Refreshed: " + ", ".join([f"{c.get('new', 0)} new", linked] + rest)
            # a refresh takes minutes: keep the result for the page, in case the browser moved on
            store.set_setting("last_refresh", f"{datetime.now():%Y-%m-%d %H:%M} {msg}")
            return msg
        return run(request, "/", svc.refresh, ok)

    @app.post("/recipes/{slug}/forget")
    def forget(request: Request, slug: str, confirm: str = Form("")):
        row = store.get_recipe(slug)
        if row is None or row["status"] != "changed":
            raise HTTPException(400, "Only a recipe that changed after it was saved can be synced again")
        if not confirm:
            return recipe_page(request, slug, ask_forget=True)
        return run(request, f"/recipes/{quote(slug)}", lambda: svc.forget_saved(slug),
                   lambda _: f"Status now: {store.get_recipe(slug)['status']}")

    @app.post("/recipes/{slug}/update")
    def update(request: Request, slug: str, confirm: str = Form("")):
        row = store.get_recipe(slug)
        if row is None or row["status"] != "changed" or not row["mnd_food_id"]:
            raise HTTPException(400, "Only a recipe that changed after it was saved can be updated")
        path = f"/recipes/{quote(slug)}"
        if confirm:
            return run(request, path, lambda: svc.update(slug, commit=True), lambda stuck: "Updated in MyNetDiary" + (
                ", but the old copy could not be removed: delete it in MyNetDiary" if stuck else ""))
        try:
            names = svc.update(slug, commit=False)
        except Exception as e:  # noqa: BLE001  text can carry URLs: never shown
            log.warning("action failed (%s)", describe(e))
            return back(request, path, _FAILED[error_kind(e)])
        return recipe_page(request, slug, ask_update=names)

    @app.post("/recipes/{slug}/delete")
    def delete(request: Request, slug: str, confirm: str = Form("")):
        row = store.get_recipe(slug)
        if row is None or row["status"] != "removed" or not row["mnd_food_id"]:
            raise HTTPException(400, "Only a recipe removed from Mealie can be deleted in MyNetDiary")
        if not confirm:
            return recipe_page(request, slug, ask_delete=True)
        return run(request, f"/recipes/{quote(slug)}", lambda: svc.delete_in_mnd(slug), lambda _: "Deleted in MyNetDiary")

    @app.post("/recipes/{slug}/recheck")
    def recheck(request: Request, slug: str):
        return run(request, f"/recipes/{quote(slug)}", lambda: svc.remap(slug), lambda _: "Re-checked")

    @app.post("/sync")
    def sync(request: Request, slugs: list[str] = Form([], alias="slugs[]"), mode: str = Form("sync"),
             confirm: str = Form(""), all_ready: str = Form("", alias="all")):
        def ok(out):
            return (f"Saved {len(out['saved'])}, skipped {len(out['skipped'])}, errors {len(out['errors'])}")
        if all_ready and mode == "sync":  # only the explicit "Sync all ready" button; an empty selection does nothing
            return run(request, "/", lambda: svc.sync(None), ok)
        if not slugs:
            return back(request, "/", "Nothing selected")
        if mode == "force" and not confirm:
            rows = [r for r in map(store.get_recipe, slugs) if r]
            return templates.TemplateResponse(request, "confirm.html", {
                "slugs": slugs, "lines": [(r["name"], unresolved(r)) for r in rows]})
        if mode == "dry":
            def dry():
                svc.refresh()
                for s in slugs:
                    svc.remap(s)
            return run(request, "/", dry, lambda _: "Checked " + ", ".join(
                f"{r['name']}: {r['status']}" for r in map(store.get_recipe, slugs) if r))

        return run(request, "/", lambda: svc.sync(slugs, force=mode == "force"), ok)

    @app.get("/review")
    def review(request: Request, msg: str = ""):
        groups = {}  # normalized ingredient -> {"name", "items": [(row, resolution)]}
        for row in store.list_recipes("review"):
            for r in mapping(row)["resolutions"] if row["mapping_json"] else []:
                if r["needs_review"]:
                    g = groups.setdefault(normalize_key(r["query"]), {"name": r["query"], "items": []})
                    g["items"].append((row, r))
        waiting = [r for r in store.list_plan_entries(date.today().isoformat()) if r["state"] == PLAN_WAITING]
        return templates.TemplateResponse(request, "review.html", {
            "groups": list(groups.values()), "msg": msg, "waiting": waiting, "meals": _MEALS,
            "known": {r["slug"] for r in waiting if store.get_recipe(r["slug"])}})

    @app.get("/mealplan")
    def mealplan(request: Request, msg: str = ""):
        rows = store.list_plan_entries(date.today().isoformat())
        return templates.TemplateResponse(request, "mealplan.html", {
            "rows": rows, "msg": msg, "meals": _MEALS, "states": _PLAN_STATES,
            "known": {r["slug"] for r in rows if store.get_recipe(r["slug"])}})

    @app.post("/mealplan/run")
    def mealplan_run(request: Request):
        def ok(c):
            parts = [f"{c[k]} {w}" for k, w in (("logged", "added"), ("moved", "moved"), ("removed", "removed"),
                                                ("waiting", "waiting"), ("failed", "failed"),
                                                ("user_removed", "removed by you")) if c[k]]
            return "Meal plan: " + (", ".join(parts) or "nothing to do")
        return run(request, "/mealplan", lambda: svc.log_meal_plan(date.today()), ok)

    @app.post("/resolve")
    def resolve(request: Request, ingredient: str = Form(""), slug: str = Form(""), scope: str = Form(""),
                action: str = Form(""), food_id: str = Form(""), description: str = Form(""), amount: str = Form("")):
        ingredient = ingredient.strip()
        if not ingredient:  # a 400 like the checks below, never FastAPI's raw 422
            raise HTTPException(400, "Give an ingredient name")
        row = store.get_recipe(slug)
        line = next((r for r in mapping(row)["resolutions"] if normalize_key(r["query"]) == normalize_key(ingredient)),
                    None) if row and row["mapping_json"] else None
        if action == "accept":
            if not line or not line["mnd_food_id"]:
                raise HTTPException(400, "Nothing to accept for this line")
            food_id, description = line["mnd_food_id"], line["mnd_description"]
        elif action == "choose" and not food_id:
            raise HTTPException(400, "Pick a food first")
        elif action == "exclude":
            food_id = description = ""
        elif action != "choose":
            raise HTTPException(400, "Unknown action")
        if scope not in ("all", "recipe"):
            raise HTTPException(400, "Unknown scope")
        if scope == "recipe" and row is None:
            raise HTTPException(400, "No such recipe")
        args = (food_id, description, amount.strip() or None, action == "exclude")
        if scope == "all":
            store.set_alias(ingredient, *args)
            return remapped(request, lambda: svc.remap_using(ingredient),
                            lambda s: f"Saved for all recipes; re-checked {len(s)}", "/review")
        store.set_override(slug, ingredient, *args)
        return remapped(request, lambda: svc.remap(slug), lambda _: "Saved for this recipe; re-checked", "/review")

    @app.get("/pick")
    def pick(request: Request, ingredient: str = "", slug: str = "", scope: str = "all", q: str = ""):
        query, foods, error = (q or ingredient).strip(), [], None

        def search(client):
            out = []
            for hit in client.search_foods(query, limit=10):  # one kcal lookup per hit
                try:  # kcal per 100 g, as the CLI's search --per-100g computes it
                    raw = client.get_food_entry(hit.food_id, "100 g").raw
                    g, k = grams_of(raw), number(raw.get("calories"))
                    per = k / g * 100 if g and k is not None else None
                except MndError:
                    per = None
                out.append({"id": hit.food_id, "description": hit.description, "per100": per, "system": hit.is_system})
            return out

        if query:  # a new alias starts with no ingredient: nothing to search yet
            try:
                try:
                    foods = search(conns.mnd.client())
                except MndAuthError:  # the session expired: log in again once
                    foods = search(conns.mnd.relogin())
            except Exception:  # noqa: BLE001  exception text can carry URLs: never shown
                error = "MyNetDiary search failed: try again or check the connection in Settings."
        return templates.TemplateResponse(request, "pick.html", {
            "ingredient": ingredient.strip(), "slug": slug, "scope": scope, "q": query, "foods": foods, "error": error})

    def aliases_page(request, msg="", error=None, status=200):
        overrides = [{**o, "recipe": (store.get_recipe(o["slug"]) or {}).get("name") or o["slug"]}
                     for o in store.list_overrides()]
        return templates.TemplateResponse(request, "aliases.html", {
            "aliases": store.list_aliases(), "overrides": overrides, "msg": msg, "error": error},
            status_code=status)

    def remapped(request, action, ok, path="/aliases"):
        """The store change is kept and the recipes are marked stale; a re-map that can't run waits for the next refresh."""
        try:
            msg = ok(action())
        except Exception:  # noqa: BLE001  SyncBusy, auth errors, anything: text is never shown
            msg = "Saved \u2014 will apply on the next refresh"
        return back(request, path, msg)

    @app.get("/aliases")
    def aliases(request: Request, msg: str = ""):
        return aliases_page(request, msg)

    @app.post("/aliases")
    def alias_save(request: Request, name: str = Form(...), food_id: str = Form(""), description: str = Form(""),
                   amount: str = Form(""), exclude: str = Form(""), slug: str = Form("")):
        if not name.strip() or not (exclude or food_id.strip()):
            raise HTTPException(400, "Give an ingredient name and a food (or tick leave out)")
        args = (food_id.strip(), description.strip(), amount.strip() or None, bool(exclude))
        if slug:
            store.set_override(slug, name, *args)
            return remapped(request, lambda: svc.remap(slug), lambda _: "Saved for this recipe; re-checked")
        store.set_alias(name, *args)
        return remapped(request, lambda: svc.remap_using(name), lambda s: f"Saved; re-checked {len(s)}")

    @app.post("/aliases/delete")
    def alias_delete(request: Request, name: str = Form(...), slug: str = Form("")):
        if slug:
            store.delete_override(slug, name)
            return remapped(request, lambda: svc.remap(slug), lambda _: "Deleted; re-checked")
        store.delete_alias(name)
        return remapped(request, lambda: svc.remap_using(name), lambda s: f"Deleted; re-checked {len(s)}")

    @app.post("/aliases/import")
    def alias_import(request: Request, text: str = Form("")):
        try:
            names = list(json.loads(text))  # same shape check as import_pantry, to know which names to re-map
            count = store.import_pantry(text)
        except (ValueError, TypeError):  # text can quote the pasted data: fixed message only
            return aliases_page(request, error="That is not a valid pantry.json (expected an object of ingredient: food).",
                                status=400)
        return remapped(request, lambda: svc.remap_many(names), lambda _: f"Imported {count} aliases")

    @app.get("/settings")
    def settings_get(request: Request, msg: str = ""):
        return settings_page(request, msg or None)

    @app.post("/settings")
    def settings_post(request: Request, mealie_url: str = Form(""), mealie_username: str = Form(""),
                      mealie_password: str = Form(""), mealie_token: str = Form(""),
                      mnd_email: str = Form(""), mnd_password: str = Form(""), mnd_jsessionid: str = Form(""),
                      notify_service: str = Form(""), auto_sync: str = Form(""),
                      log_meal_plan: str = Form(""),
                      poll_minutes: str | None = Form(None), quiet_minutes: str | None = Form(None)):
        notify_service = notify_service.strip()
        if notify_service and not valid_service(notify_service):
            return settings_page(request, "Phone notification service must use only lowercase letters, "
                                          "digits and underscores. Nothing was saved.")
        minutes = {k: v.strip() for k, v in (("poll_minutes", poll_minutes), ("quiet_minutes", quiet_minutes))
                   if v is not None}  # a field left out (FastAPI also reads a blank one as None) keeps its value
        if not all(re.fullmatch(r"[0-9]{1,6}", v) and int(v) >= 1 for v in minutes.values()):
            return settings_page(request, "Check interval and quiet period must be whole minutes, 1 or more. "
                                          "Nothing was saved.")
        if mealie_url.strip() != store.get_setting("mealie_url") or mealie_token:
            store.set_setting("mealie_ok", None)  # the last test no longer says anything about this connection
        if mnd_email.strip() or mnd_password or mnd_jsessionid.strip():
            store.set_setting("mnd_ok", None)
        store.set_setting("mealie_url", mealie_url.strip())
        store.set_setting("notify_service", notify_service)  # blank unsets
        store.set_setting("auto_sync", "on" if auto_sync else "off")
        store.set_setting("log_meal_plan", "on" if log_meal_plan else "off")
        for k, v in minutes.items():
            store.set_setting(k, int(v))
        message = "Settings saved."
        if mealie_username and mealie_password and not mealie_token:
            try:
                mealie_token = conns.create_token(store.get_setting("mealie_url"), mealie_username, mealie_password)
            except MealieError:  # text can hold URLs, so it is never shown
                message = "Settings saved, but no Mealie token was created: check the URL, username and password."
        # blank means keep what is saved
        given = {"mealie_token": mealie_token, "mnd_email": mnd_email.strip(),
                 "mnd_password": mnd_password, "mnd_jsessionid": mnd_jsessionid.strip()}
        creds.save(**{k: v for k, v in given.items() if v})
        return settings_page(request, message)

    @app.post("/settings/run-now")
    def run_now(request: Request):
        result = tick(svc, store, app.state.notifier, datetime.now(timezone.utc),
                      recipes=True, plan=store.get_setting("log_meal_plan") == "on")
        msg = {"busy": "A sync is already running", "mnd_auth": "Sync failed: MyNetDiary login failed",
               **_FAILED}.get(result.get("error"), "Sync finished")
        return back(request, "/settings", msg)

    @app.post("/settings/test-mealie")
    def test_mealie(request: Request):
        if not creds.has("mealie_token"):
            return settings_page(request, "Mealie: nothing saved yet. Fill in the form and press Save first, then test.")
        try:
            info = conns.mealie.client().check()
            message = f"Mealie OK: {info['user']} on {info['version']}."
        except Exception as e:  # text can hold URLs or tokens: type and status only, in the log too
            log.warning("Mealie test failed (%s)", describe(e))
            message = ("Mealie failed: check the URL and token (is Mealie reachable?)." if isinstance(e, MealieError)
                       else f"Mealie failed ({type(e).__name__}).")
        record_ok("mealie_ok", message.startswith("Mealie OK"))
        return settings_page(request, message)

    @app.post("/settings/test-mnd")
    def test_mnd(request: Request):
        if not (creds.has("mnd_email") and creds.has("mnd_password")) and not creds.has("mnd_jsessionid"):
            return settings_page(request, "MyNetDiary: nothing saved yet. Fill in the form and press Save first, then test.")
        try:
            try:
                conns.mnd.client().existing_recipes()
            except MndAuthError:  # session may just have expired
                conns.mnd.relogin().existing_recipes()
            message = "MyNetDiary OK."
        except Exception as e:  # text can hold URLs or tokens: type and status only, in the log too
            log.warning("MyNetDiary test failed (%s)", describe(e))
            message = ("MyNetDiary failed: login rejected, check the email, password or JSESSIONID."
                       if isinstance(e, MndAuthError) else f"MyNetDiary failed ({type(e).__name__}).")
        record_ok("mnd_ok", message == "MyNetDiary OK.")
        return settings_page(request, message)

    return app
