import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.web import create_app
from mynetdiary.errors import MndAuthError, MndError

H = {"X-Ingress-Path": "/api/hassio_ingress/ing"}


def line(query, review=True, food="F1", desc="Rice, white"):
    return {"ingredient": query, "query": query, "amount": "1 cup", "matched": True, "mnd_food_id": food,
            "mnd_description": desc, "confidence": "low", "score": 50, "via_alias": False, "custom_food": False,
            "skipped": False, "amount_issue": None, "grams": 100, "calories": 130, "note": "weak match",
            "needs_review": review}


class FakeService:
    busy = False

    def __init__(self):
        self.calls, self.fail = [], None

    def remap(self, slug):
        if self.fail:
            raise self.fail
        self.calls.append(("remap", slug))

    def remap_using(self, name):
        if self.fail:
            raise self.fail
        self.calls.append(("remap_using", name))
        return []


class FakeMnd:
    def __init__(self, fail=False):
        self.fail = fail

    def client(self):
        if self.fail:
            raise MndError("secret-url boom")
        return self

    def search_foods(self, q, limit=30):
        return [SimpleNamespace(food_id="S1", description="Rice <b>white</b>", is_system=True),
                SimpleNamespace(food_id="C1", description="My rice", is_system=False)]

    def get_food_entry(self, food_id, amount):
        if food_id == "C1":
            raise MndError("nope")
        return SimpleNamespace(raw={"calories": 130, "gmWgt": 100})


def make(tmp_path, mnd=None):
    svc = FakeService()
    app = create_app(tmp_path, connections=SimpleNamespace(mnd=mnd or FakeMnd(), mealie=None), service=svc)
    for slug, status, lines in [("a", "review", [line("Rice"), line("salt", review=False)]),
                                ("b", "review", [line("rice ")]), ("c", "ready", [line("rice", review=False)])]:
        m = {"name": slug, "servings": 1, "total_kcal": 1, "kcal_per_serving": 1, "existing_food_id": None, "resolutions": lines}
        app.state.store.upsert_recipe(slug, f"Recipe {slug}", "t", json.dumps(m), status)
    return TestClient(app), app.state.store, svc


def post(c, **f):
    base = {"ingredient": "rice", "slug": "a", "scope": "all", "action": "accept", "food_id": "", "description": "", "amount": ""}
    return c.post("/resolve", data={**base, **f}, headers=H, follow_redirects=False)


def test_review_groups_by_ingredient(tmp_path):
    c, *_ = make(tmp_path)
    t = c.get("/review", headers=H).text
    assert t.count("<h2") == 1 and "Recipe a" in t and "Recipe b" in t and "Recipe c" not in t and "salt" not in t


def test_accept_creates_global_alias_and_remaps_both(tmp_path):
    c, store, svc = make(tmp_path)
    r = post(c)
    assert r.status_code == 303 and r.headers["location"].startswith("/api/hassio_ingress/ing/review?msg=")
    assert store.list_aliases() == [{"name": "rice", "food_id": "F1", "description": "Rice, white", "amount": None, "exclude": False}]
    assert svc.calls == [("remap_using", "rice")]


def test_choose_recipe_scope_creates_override_only(tmp_path):
    c, store, svc = make(tmp_path)
    post(c, scope="recipe", action="choose", food_id="S1", description="Rice X")
    assert store.list_aliases() == []
    o = store.list_overrides("a")[0]
    assert (o["name"], o["food_id"], o["description"]) == ("rice", "S1", "Rice X")
    assert svc.calls == [("remap", "a")]


def test_exclude_leaves_line_out(tmp_path):
    c, store, _ = make(tmp_path)
    post(c, action="exclude")
    a = store.list_aliases()[0]
    assert a["exclude"] is True and a["food_id"] == ""


def test_amount_override_saved(tmp_path):
    c, store, _ = make(tmp_path)
    post(c, amount=" 150 g ")
    assert store.list_aliases()[0]["amount"] == "150 g"


def test_invalid_resolve_is_400_and_stores_nothing(tmp_path):
    c, store, svc = make(tmp_path)
    assert post(c, action="bogus").status_code == 400
    assert post(c, scope="x").status_code == 400
    assert post(c, action="choose").status_code == 400
    assert store.list_aliases() == [] and svc.calls == []


def test_picker_shows_kcal_per_100g_and_db_flag(tmp_path):
    c, *_ = make(tmp_path)
    t = c.get("/pick?ingredient=rice&slug=a&scope=recipe", headers=H).text
    assert "Rice &lt;b&gt;white&lt;/b&gt;" in t and "<b>white" not in t
    assert "130" in t and "database" in t and "custom" in t and "?" in t
    assert 'value="rice"' in t and 'value="recipe"' in t


def test_picker_failure_message_is_fixed(tmp_path):
    c, *_ = make(tmp_path, mnd=FakeMnd(fail=True))
    r = c.get("/pick?ingredient=rice&slug=a", headers=H)
    assert "secret-url" not in r.text and "MyNetDiary search failed" in r.text


def test_recipe_only_on_second_recipe_in_group(tmp_path):
    c, store, svc = make(tmp_path)
    t = c.get("/review", headers=H).text
    assert "slug=b" in t and 'name="slug" value="b"' in t  # row b has its own controls
    post(c, slug="b", scope="recipe", action="choose", food_id="S1", description="Rice X")
    assert store.list_overrides("a") == [] and store.list_aliases() == []
    assert [o["food_id"] for o in store.list_overrides("b")] == ["S1"]
    assert svc.calls == [("remap", "b")]


def test_recipe_scope_needs_a_real_slug(tmp_path):
    c, store, svc = make(tmp_path)
    for slug in ("", "nope"):
        assert post(c, slug=slug, scope="recipe", action="exclude").status_code == 400
    assert store.list_overrides() == [] and svc.calls == []


def test_resolve_while_busy_says_it_applies_later(tmp_path):
    from app.service import SyncBusy
    c, store, svc = make(tmp_path)
    svc.fail = SyncBusy()
    r = post(c)
    assert "will%20apply%20on%20the%20next%20refresh" in r.headers["location"] and store.list_aliases()


class ExpiringMnd(FakeMnd):
    def __init__(self, failures):
        super().__init__()
        self.failures, self.relogins, self.searches = failures, 0, []

    def relogin(self):
        self.relogins += 1
        return self

    def search_foods(self, q, limit=30):
        self.searches.append(q)
        if self.failures:
            self.failures -= 1
            raise MndAuthError("expired https://x/?token=leak")
        return super().search_foods(q, limit)


def test_picker_logs_in_again_once(tmp_path):
    mnd = ExpiringMnd(1)
    c, *_ = make(tmp_path, mnd=mnd)
    t = c.get("/pick?ingredient=rice&slug=a", headers=H).text
    assert mnd.relogins == 1 and "S1" in t and "search failed" not in t
    mnd.failures = 2
    t = c.get("/pick?ingredient=rice&slug=a", headers=H).text
    assert mnd.relogins == 2 and "MyNetDiary search failed" in t and "leak" not in t


def test_picker_without_ingredient_asks_for_it(tmp_path):
    mnd = ExpiringMnd(0)
    c, *_ = make(tmp_path, mnd=mnd)
    t = c.get("/pick?scope=all&ingredient=", headers=H).text
    assert mnd.searches == [] and '<input name="ingredient" required>' in t
    t = c.get("/pick?scope=all&ingredient=&q=rice", headers=H).text
    assert mnd.searches == ["rice"] and t.count('name="ingredient" required') == 3  # search form + 2 choose forms


def test_resolve_without_ingredient_is_400_not_422(tmp_path):
    c, store, svc = make(tmp_path)
    assert post(c, ingredient=" ", action="choose", food_id="S1").status_code == 400
    assert c.post("/resolve", data={}, headers=H).status_code == 400
    assert store.list_aliases() == [] and svc.calls == []
