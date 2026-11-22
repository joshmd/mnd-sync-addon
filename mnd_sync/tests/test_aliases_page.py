import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.service import SyncBusy
from app.web import create_app

H = {"X-Ingress-Path": "/api/hassio_ingress/ing"}


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

    def remap_many(self, names):
        if self.fail:
            raise self.fail
        self.calls.append(("remap_many", names))
        return []


def make(tmp_path):
    svc = FakeService()
    app = create_app(tmp_path, connections=SimpleNamespace(mnd=None, mealie=None), service=svc)
    return TestClient(app, follow_redirects=False), app.state.store, svc


def test_import_pantry_json_creates_aliases(tmp_path):
    c, store, svc = make(tmp_path)
    text = json.dumps({"Rice": {"food_id": "F1", "description": "Rice <b>", "amount": "150 g"},
                       "salt": {"food_id": "", "description": "", "exclude": True}})
    r = c.post("/aliases/import", data={"text": text}, headers=H)
    assert r.status_code == 303 and "Imported%202%20aliases" in r.headers["location"]
    assert {a["name"] for a in store.list_aliases()} == {"rice", "salt"}
    assert svc.calls == [("remap_many", ["Rice", "salt"])]  # one call: each recipe re-mapped once
    page = c.get("/aliases", headers=H).text
    assert "Rice &lt;b&gt;" in page and "<b>" not in page


def test_edit_and_delete_alias_remaps_affected_recipes(tmp_path):
    c, store, svc = make(tmp_path)
    c.post("/aliases", data={"name": "rice", "food_id": "F1", "description": "Rice"}, headers=H)
    c.post("/aliases", data={"name": "rice", "food_id": "F2", "description": "Basmati", "amount": "1 cup"}, headers=H)
    assert store.list_aliases()[0]["food_id"] == "F2" and store.list_aliases()[0]["amount"] == "1 cup"
    c.post("/aliases", data={"name": "salt", "exclude": "on"}, headers=H)
    assert store.list_aliases()[1]["exclude"]
    c.post("/aliases/delete", data={"name": "rice"}, headers=H)
    assert [a["name"] for a in store.list_aliases()] == ["salt"]
    assert svc.calls.count(("remap_using", "rice")) == 3
    svc.fail = SyncBusy()
    r = c.post("/aliases", data={"name": "oil", "food_id": "F3", "description": "Oil"}, headers=H)
    assert "Saved" in r.headers["location"] and len(store.list_aliases()) == 2
    assert c.post("/aliases", data={"name": "x"}, headers=H).status_code == 400


def test_overrides_listed_with_recipe_name(tmp_path):
    c, store, svc = make(tmp_path)
    store.upsert_recipe("pb", "Pasta <i>bake</i>", "t", None, "ready")
    store.set_override("pb", "rice", "F1", "Rice")
    store.set_override("gone", "salt", "F1", "Salt")
    c.post("/aliases", data={"slug": "gone", "name": "oil", "food_id": "F3", "description": "Oil"}, headers=H)
    assert ("remap", "gone") in svc.calls
    page = c.get("/aliases", headers=H).text
    assert "Pasta &lt;i&gt;bake&lt;/i&gt;" in page and "<i>" not in page
    assert "gone" in page and "oil" in page
    c.post("/aliases/delete", data={"slug": "gone", "name": "oil"}, headers=H)
    assert [o["name"] for o in store.list_overrides()] == ["salt", "rice"]


def test_invalid_json_import_shows_error_not_500(tmp_path):
    c, store, svc = make(tmp_path)
    r = c.post("/aliases/import", data={"text": "{nope secret"}, headers=H)
    assert r.status_code == 400 and "not a valid pantry.json" in r.text.lower() and "secret" not in r.text
    assert c.post("/aliases/import", data={"text": "[1]"}, headers=H).status_code == 400
    assert store.list_aliases() == [] and svc.calls == []
