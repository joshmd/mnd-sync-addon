import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.service import SyncBusy
from app.web import create_app

PREFIX = "/api/hassio_ingress/abc"
HEADERS = {"X-Ingress-Path": PREFIX}


def res(query, grams, cal, review=False, matched=True):
    return {"ingredient": query, "query": query, "amount": "1 cup", "matched": matched, "mnd_description": "Flour, white",
            "confidence": "high", "score": 90, "via_alias": False, "custom_food": False, "skipped": False,
            "amount_issue": None, "grams": grams, "calories": cal, "note": None, "needs_review": review}


class FakeService:
    busy = False

    def __init__(self, raise_busy=False):
        self.calls, self.raise_busy = [], raise_busy

    def _go(self, name, *a, **k):
        if self.raise_busy:
            raise SyncBusy()
        self.calls.append((name, a, k))

    def refresh(self):
        self._go("refresh")
        return {}

    def remap(self, slug):
        self._go("remap", slug)

    def forget_saved(self, slug):
        self._go("forget_saved", slug)

    def sync(self, slugs=None, *, force=False, trigger="manual"):
        self._go("sync", slugs, force=force)
        return {"saved": list(slugs or []), "skipped": [], "errors": []}


def make(tmp_path, **kw):
    svc = FakeService(**kw)
    app = create_app(tmp_path, service=svc)
    mapping = json.dumps({"name": "Cake", "servings": 4, "total_kcal": 400, "kcal_per_serving": 100,
                          "existing_food_id": None,
                          "resolutions": [res("flour", 120, 438), res("mystery goo", None, None, review=True, matched=False)]})
    for slug, status in [("a", "ready"), ("b", "review"), ("c", "saved"), ("d", "changed"), ("e", "empty")]:
        app.state.store.upsert_recipe(slug, f"Recipe {slug}", "t", mapping if slug in "ab" else None, status)
    return TestClient(app), svc


def test_recipes_table_shows_status_badges(tmp_path):
    c, _ = make(tmp_path)
    r = c.get("/", headers=HEADERS)
    assert r.status_code == 200
    for s in ("ready", "review", "saved", "changed", "empty"):
        assert f'class="badge {s}"' in r.text
    links = re.findall(r'(?:href|action)="([^"]*)"', r.text)
    assert all(link.startswith(PREFIX + "/") for link in links)


def test_sync_selected_calls_service_with_slugs(tmp_path):
    c, svc = make(tmp_path)
    r = c.post("/sync", data={"slugs[]": ["a", "d"], "mode": "sync"}, headers=HEADERS, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(PREFIX + "/")
    assert svc.calls == [("sync", (["a", "d"],), {"force": False})]


def test_force_requires_confirmation_listing_lines(tmp_path):
    c, svc = make(tmp_path)
    r = c.post("/sync", data={"slugs[]": ["b"], "mode": "force"}, headers=HEADERS)
    assert r.status_code == 200 and "mystery goo" in r.text and svc.calls == []
    c.post("/sync", data={"slugs[]": ["b"], "mode": "force", "confirm": "1"}, headers=HEADERS)
    assert svc.calls == [("sync", (["b"],), {"force": True})]


def test_busy_shows_message(tmp_path):
    c, _ = make(tmp_path, raise_busy=True)
    r = c.post("/sync", data={"slugs[]": ["a"], "mode": "sync"}, follow_redirects=True)
    assert "A sync is already running" in r.text
    r = c.post("/refresh", follow_redirects=True)
    assert "A sync is already running" in r.text


def test_dry_run_refreshes_and_remaps_without_saving(tmp_path):
    c, svc = make(tmp_path)
    c.post("/sync", data={"slugs[]": ["a", "b"], "mode": "dry"}, headers=HEADERS)
    assert [x[0] for x in svc.calls] == ["refresh", "remap", "remap"]


def test_recipe_detail_shows_grams_kcal_per100(tmp_path):
    c, svc = make(tmp_path)
    r = c.get("/recipes/a", headers=HEADERS)
    assert r.status_code == 200
    assert "120" in r.text and "438" in r.text and "365" in r.text  # 438/120*100
    assert c.get("/recipes/nope", headers=HEADERS).status_code == 404
    c.post("/recipes/a/recheck", headers=HEADERS)
    assert svc.calls == [("remap", ("a",), {})]


def test_changed_recipe_can_be_synced_again_after_confirming(tmp_path):
    c, svc = make(tmp_path)
    page = c.get("/recipes/d", headers=HEADERS).text
    assert "changed in Mealie after it was saved" in page and "/recipes/d/forget" in page
    assert "changed in Mealie" not in c.get("/recipes/a", headers=HEADERS).text
    r = c.post("/recipes/d/forget", headers=HEADERS)
    assert r.status_code == 200 and 'name="confirm"' in r.text and svc.calls == []
    r = c.post("/recipes/d/forget", data={"confirm": "1"}, headers=HEADERS, follow_redirects=False)
    assert r.status_code == 303 and svc.calls == [("forget_saved", ("d",), {})]
    assert c.post("/recipes/a/forget", data={"confirm": "1"}, headers=HEADERS).status_code == 400


def test_refresh_message_shows_linked_and_first_run_warning(tmp_path):
    c, svc = make(tmp_path)
    svc.refresh = lambda: {"new": 2, "linked": 3, "removed": 1, "check_links": False}
    t = c.post("/refresh", follow_redirects=True).text
    assert "Refreshed: 2 new, 3 linked, 1 removed" in t
    svc.refresh = lambda: {"new": 5, "linked": 0, "check_links": True}
    t = c.post("/refresh", follow_redirects=True).text
    assert "5 new, 0 linked — check before turning on auto-sync" in t


def test_last_refresh_result_stays_on_the_page(tmp_path):
    c, svc = make(tmp_path)
    svc.refresh = lambda: {"new": 0, "linked": 33, "check_links": False}
    c.post("/refresh", follow_redirects=False)  # the browser never follows: user clicked away
    t = c.get("/recipes").text
    assert "Last refresh" in t and "0 new, 33 linked" in t


def test_sync_all_ready_only_from_its_own_button(tmp_path):
    c, svc = make(tmp_path)
    page = c.get("/", headers=HEADERS).text
    assert 'name="all" value="1"' in page and "Sync all ready" in page
    c.post("/sync", data={"mode": "sync"}, headers=HEADERS)  # checkbox form with nothing ticked
    assert svc.calls == []
    c.post("/sync", data={"all": "1", "mode": "sync"}, headers=HEADERS)
    assert svc.calls == [("sync", (None,), {"force": False})]


def test_sync_this_recipe_button(tmp_path):
    c, svc = make(tmp_path)
    page = c.get("/recipes/a", headers=HEADERS).text
    assert 'name="slugs[]" value="a"' in page and "Sync this recipe" in page
    assert "Sync this recipe" not in c.get("/recipes/c", headers=HEADERS).text  # already saved


def test_status_filter_links(tmp_path):
    c, _ = make(tmp_path)
    page = c.get("/recipes?status=review", headers=HEADERS).text
    assert "Recipe b" in page and "Recipe a" not in page
    for s in ("ready", "review", "saved", "changed", "error", "duplicate", "empty", "removed"):
        assert f'href="{PREFIX}/recipes?status={s}"' in page
    assert "Recipe a" in c.get("/recipes?status=bogus", headers=HEADERS).text


def test_failure_message_names_the_side(tmp_path):
    from mealie.client import MealieError
    from mynetdiary.errors import MndRequestError
    c, svc = make(tmp_path)
    for error, text in ((MealieError("http://leak"), "Mealie unreachable"),
                        (MndRequestError("http://leak"), "MyNetDiary unreachable"), (KeyError("leak"), "add-on log")):
        def boom():
            raise error
        svc.refresh = boom
        t = c.post("/refresh", follow_redirects=True).text
        assert text in t and "leak" not in t
