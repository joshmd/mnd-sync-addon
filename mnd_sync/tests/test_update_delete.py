import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.web import create_app  # noqa: E402
from mynetdiary.models import SavedFood  # noqa: E402
from tests.test_service import R, FakeMnd, FakeMealie, Store, SyncService  # noqa: E402

HEADERS = {"X-Ingress-Path": "/api/hassio_ingress/abc"}


class UpdMnd(FakeMnd):
    def __init__(self, store=None, in_diary=False):
        super().__init__()
        self.store, self.in_diary, self.updated, self.retired, self.retire_fails = store, in_diary, [], [], False
        self.id_at_retire = None

    def update_recipe(self, food_id, recipe, dry_run=None):
        self.updated.append((food_id, recipe.name))
        return None if self.in_diary else SavedFood(food_id=food_id, name=recipe.name)

    def retire_food(self, food_id, dry_run=None):
        self.retired.append(food_id)
        self.id_at_retire = self.store.get_recipe("smoothie")["mnd_food_id"]
        if self.retire_fails:
            raise RuntimeError("boom")


def changed(tmp_path, **kw):
    store = Store(tmp_path / "s.db")
    mnd, mealie = UpdMnd(store, **kw), FakeMealie({k: dict(v) for k, v in R.items()})
    svc = SyncService(store, mnd, mealie)
    svc.refresh()
    svc.sync(["smoothie"])
    mealie.recipes["smoothie"]["updated"] = "2"
    mealie.recipes["smoothie"]["ingredients"] = ["banana", "unobtainium"]
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "changed"
    return svc, mnd, mealie, store


def test_update_saves_in_place(tmp_path):
    svc, mnd, _, store = changed(tmp_path)
    old = store.get_recipe("smoothie")["mnd_food_id"]
    svc.update("smoothie", commit=True)
    row = store.get_recipe("smoothie")
    assert mnd.updated == [(old, "Smoothie")] and mnd.retired == []
    assert (row["status"], row["mnd_food_id"], row["saved_mealie_updated"]) == ("saved", old, "2")


def test_update_in_diary_saves_new_and_retires_old(tmp_path):
    svc, mnd, _, store = changed(tmp_path, in_diary=True)
    old = store.get_recipe("smoothie")["mnd_food_id"]
    svc.update("smoothie", commit=True)
    new = store.get_recipe("smoothie")["mnd_food_id"]
    assert new != old and mnd.saved.count("Smoothie") == 2
    assert mnd.retired == [old] and mnd.id_at_retire == new
    assert store.get_recipe("smoothie")["status"] == "saved"


def test_update_retire_failure_keeps_new_id(tmp_path):
    svc, mnd, _, store = changed(tmp_path, in_diary=True)
    mnd.retire_fails = True
    assert svc.update("smoothie", commit=True) is True  # update succeeded, old copy stuck
    row = store.get_recipe("smoothie")
    assert row["mnd_food_id"] == mnd.id_at_retire != mnd.retired[0] and row["status"] == "saved"


def test_update_preview_saves_nothing(tmp_path):
    svc, mnd, _, store = changed(tmp_path)
    assert svc.update("smoothie", commit=False) == ["unobtainium"]
    assert mnd.updated == [] and store.get_recipe("smoothie")["status"] == "changed"


def test_update_refused_unless_changed(tmp_path):
    svc, _, _, _ = changed(tmp_path)
    with pytest.raises(ValueError):
        svc.update("porridge", commit=True)


def test_delete_only_for_removed(tmp_path):
    svc, mnd, mealie, store = changed(tmp_path)
    with pytest.raises(ValueError):
        svc.delete_in_mnd("smoothie")
    old = store.get_recipe("smoothie")["mnd_food_id"]
    del mealie.recipes["smoothie"]
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "removed"
    svc.delete_in_mnd("smoothie")
    row = store.get_recipe("smoothie")
    assert mnd.retired == [old] and row["mnd_food_id"] is None and row["status"] == "removed"
    mealie.recipes["smoothie"] = dict(R["smoothie"])
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] != "saved"


class FakeSvc:
    def __init__(self):
        self.calls, self.stuck = [], False

    busy = False

    def update(self, slug, *, commit):
        self.calls.append(("update", slug, commit))
        return ["goo"] if not commit else self.stuck

    def delete_in_mnd(self, slug):
        self.calls.append(("delete", slug))


def web(tmp_path):
    svc = FakeSvc()
    app = create_app(tmp_path, service=svc)
    app.state.store.upsert_recipe("c", "Cake", "t", None, "changed", mnd_food_id="5")
    app.state.store.upsert_recipe("r", "Gone", "t", None, "removed", mnd_food_id="6")
    return TestClient(app), svc


def test_update_route_asks_then_updates(tmp_path):
    c, svc = web(tmp_path)
    page = c.get("/recipes/c", headers=HEADERS).text
    assert "Update in MyNetDiary" in page
    r = c.post("/recipes/c/update", headers=HEADERS)
    assert r.status_code == 200 and "Replace the MyNetDiary recipe" in r.text and "goo" in r.text
    assert svc.calls == [("update", "c", False)]
    r = c.post("/recipes/c/update", data={"confirm": "1"}, headers=HEADERS, follow_redirects=False)
    assert r.status_code == 303 and svc.calls[-1] == ("update", "c", True) and "Updated" in r.headers["location"]


def test_update_route_reports_old_copy_not_removed(tmp_path):
    c, svc = web(tmp_path)
    svc.stuck = True
    r = c.post("/recipes/c/update", data={"confirm": "1"}, headers=HEADERS, follow_redirects=False)
    assert "old%20copy%20could%20not%20be%20removed" in r.headers["location"]


def test_delete_route_asks_then_deletes(tmp_path):
    c, svc = web(tmp_path)
    assert "Delete in MyNetDiary too" in c.get("/recipes/r", headers=HEADERS).text
    r = c.post("/recipes/r/delete", headers=HEADERS)
    assert r.status_code == 200 and "Past diary entries keep it" in r.text and svc.calls == []
    r = c.post("/recipes/r/delete", data={"confirm": "1"}, headers=HEADERS, follow_redirects=False)
    assert r.status_code == 303 and svc.calls == [("delete", "r")] and "Deleted" in r.headers["location"]
