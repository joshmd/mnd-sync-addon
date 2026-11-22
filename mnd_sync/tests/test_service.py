import threading

import pytest

from app.service import SyncBusy, SyncService
from app.store import Store
from mealie.models import MealieRecipe
from mynetdiary.errors import MndAuthError
from mynetdiary.models import FoodEntry, FoodSearchResult, SavedFood

SYS = {"system": True}


def ing(name):
    return {"food": {"name": name}, "quantity": 1, "unit": {"name": "medium"}}


class FakeMealie:
    def __init__(self, recipes):
        self.recipes = recipes  # slug -> {name, updated, ingredients}

    def client(self):
        return self

    def list_all_recipes(self):
        return [{"slug": s, "name": r["name"], "id": r.get("id")} for s, r in self.recipes.items()]

    def get_recipe(self, slug):
        r = self.recipes[slug]
        if r.get("boom"):
            raise RuntimeError("mealie 500")
        return MealieRecipe.from_api({"slug": slug, "id": r.get("id"), "name": r["name"], "dateUpdated": r.get("updated"),
                                      "recipeIngredient": [ing(i) for i in r["ingredients"]]})


class FakeMnd:
    def __init__(self, existing=None):
        self._existing, self.saved = existing or {}, []
        self.auth_failures, self.relogins, self.gate = 0, 0, None
        self.save_auth_failures = 0

    def client(self):
        return self

    def relogin(self):
        self.relogins += 1
        return self

    def existing_recipes(self):
        if self.auth_failures:
            self.auth_failures -= 1
            raise MndAuthError("dead")
        if self.gate:
            self.gate.wait(5)
        return dict(self._existing)

    def search_foods(self, query, limit=8):
        if "unobtainium" in query.lower():
            return []
        return [FoodSearchResult(food_id=f"v1.{query}=", description=query.title(), raw=SYS)]

    def get_food_entry(self, food_id, amount, meal_type_id=1):
        return FoodEntry(food_id=food_id, description="x", raw={"foodId": food_id, "foodDesc": "x", "foodLabel": {}})

    def save_recipe(self, recipe, dry_run=None):
        if self.save_auth_failures and self.saved:
            self.save_auth_failures -= 1
            raise MndAuthError("dead")
        self.saved.append(recipe.name)
        self._existing[recipe.name.lower()] = str(90000 + len(self.saved))
        return SavedFood(food_id=str(90000 + len(self.saved)), name=recipe.name)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def make(store, recipes, existing=None):
    mnd, mealie = FakeMnd(existing), FakeMealie({k: dict(v) for k, v in recipes.items()})
    return SyncService(store, mnd, mealie), mnd, mealie


R = {"smoothie": {"name": "Smoothie", "updated": "1", "ingredients": ["banana"]},
     "mystery": {"name": "Mystery", "updated": "1", "ingredients": ["banana", "unobtainium"]},
     "porridge": {"name": "Porridge", "updated": "1", "ingredients": ["banana"]}}


def test_refresh_maps_new_and_links_existing(store):
    svc, mnd, _ = make(store, R, existing={"porridge": "123"})
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "ready"
    assert store.get_recipe("mystery")["status"] == "review"
    p = store.get_recipe("porridge")
    assert (p["status"], p["mnd_food_id"], p["mapping_json"]) == ("saved", "123", None)
    assert svc.sync(None)["saved"] == ["smoothie"]


def test_changed_after_save_is_marked_changed(store):
    svc, mnd, mealie = make(store, R)
    svc.refresh()
    svc.sync(None)
    assert store.get_recipe("smoothie")["status"] == "saved"
    mealie.recipes["smoothie"]["updated"] = "2"
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "changed"
    assert svc.sync(None)["saved"] == []


def test_renamed_in_mealie_is_not_saved_twice(store):
    svc, mnd, mealie = make(store, R)
    svc.refresh()
    svc.sync(None)
    mealie.recipes["smoothie"].update(name="Banana Smoothie", updated="2")
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "changed"
    assert svc.sync(None)["saved"] == [] and mnd.saved == ["Smoothie", "Porridge"]
    assert svc.sync(["smoothie"], force=True)["saved"] == []


def test_concurrent_sync_raises_busy(store):
    svc, mnd, _ = make(store, R)
    mnd.gate = threading.Event()
    t = threading.Thread(target=svc.refresh)
    t.start()
    while not svc.busy:
        pass
    with pytest.raises(SyncBusy):
        svc.sync(None)
    mnd.gate.set()
    t.join()
    assert not svc.busy


def test_force_saves_review_never_duplicates(store):
    svc, mnd, _ = make(store, R)
    svc.refresh()
    assert svc.sync(None)["saved"] == ["smoothie", "porridge"]
    assert svc.sync(None, force=True)["saved"] == ["mystery"]
    assert svc.sync(None, force=True)["saved"] == []
    assert mnd.saved == ["Smoothie", "Porridge", "Mystery"]
    assert store.last_run()["counts"]["saved"] == 0


def test_relogin_once_on_auth_error(store):
    svc, mnd, _ = make(store, R)
    mnd.auth_failures = 1
    svc.refresh()
    assert mnd.relogins == 1
    mnd.auth_failures = 2
    with pytest.raises(MndAuthError):
        svc.refresh()


def test_removed_from_mealie(store):
    svc, mnd, mealie = make(store, R)
    svc.refresh()
    del mealie.recipes["mystery"]
    assert svc.refresh()["removed"] == 1
    assert store.get_recipe("mystery")["status"] == "removed"
    assert "mystery" not in svc.sync(["mystery"], force=True)["saved"]


def test_remap_using_remaps_only_recipes_with_ingredient(store):
    svc, mnd, _ = make(store, R)
    svc.refresh()
    store.set_alias("unobtainium", "v1.x=", "Thing")
    assert svc.remap_using("Unobtainium") == ["mystery"]
    assert store.get_recipe("mystery")["status"] == "ready"


def test_saved_with_empty_food_id_is_never_remapped_or_saved_again(store):
    svc, mnd, mealie = make(store, {"smoothie": R["smoothie"]})
    store.upsert_recipe("smoothie", "Smoothie", "1", "{}", "saved", mnd_food_id="", saved_mealie_updated="1")
    mealie.recipes["smoothie"].update(name="Banana Smoothie", updated="2")
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "changed"
    assert svc.sync(None)["saved"] == [] and mnd.saved == []


def test_auth_error_mid_sync_keeps_first_attempt_results(store):
    svc, mnd, _ = make(store, R)
    svc.refresh()
    mnd.save_auth_failures = 1
    out = svc.sync(None)
    assert out["saved"] == ["smoothie", "porridge"] and mnd.saved == ["Smoothie", "Porridge"]
    assert store.last_run()["counts"]["saved"] == 2


def test_one_bad_recipe_does_not_abort_refresh(store):
    svc, mnd, mealie = make(store, R)
    svc.refresh()
    mealie.recipes["smoothie"]["boom"] = True
    mealie.recipes["porridge"] = dict(R["porridge"], updated="2", ingredients=["banana", "unobtainium"])
    mealie.recipes["new"] = {"name": "New", "updated": "1", "ingredients": ["banana"]}
    del mealie.recipes["mystery"]
    counts = svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "error"
    assert store.get_recipe("porridge")["status"] == "review"
    assert store.get_recipe("new")["status"] == "ready"
    assert store.get_recipe("mystery")["status"] == "removed" and counts["removed"] == 1


def test_renamed_saved_recipe_with_new_slug_is_not_saved_twice(store):
    recipes = {"smoothie": dict(R["smoothie"], id="m1")}
    svc, mnd, mealie = make(store, recipes)
    svc.refresh()
    svc.sync(None)
    mealie.recipes["banana-smoothie"] = dict(mealie.recipes.pop("smoothie"), name="Banana Smoothie")
    assert svc.refresh()["renamed"] == 1
    assert store.get_recipe("smoothie") is None
    r = store.get_recipe("banana-smoothie")
    assert (r["status"], r["mnd_food_id"], r["mealie_id"]) == ("changed", "90001", "m1")
    assert svc.sync(None)["saved"] == [] and mnd.saved == ["Smoothie"]


def test_busy_resolve_marks_stale_and_next_refresh_remaps(store):
    svc, mnd, _ = make(store, R)
    svc.refresh()
    store.set_alias("unobtainium", "v1.x=", "Thing")
    svc._lock.acquire()  # a sync is running
    with pytest.raises(SyncBusy):
        svc.remap_using("unobtainium")
    svc._lock.release()
    m = store.get_recipe("mystery")
    assert (m["status"], m["stale"]) == ("review", 1) and store.get_recipe("smoothie")["stale"] == 0
    assert svc.refresh()["remapped"] == 1
    m = store.get_recipe("mystery")
    assert (m["status"], m["stale"]) == ("ready", 0)


def test_remap_many_maps_each_recipe_once(store):
    svc, mnd, mealie = make(store, {"stew": {"name": "Stew", "updated": "1", "ingredients": ["a", "b", "c"]}})
    svc.refresh()
    fetched, real = [], mealie.get_recipe
    mealie.get_recipe = lambda slug: fetched.append(slug) or real(slug)
    for n in "abc":
        store.set_alias(n, "v1.x=", n.upper())
    assert svc.remap_many(["a", "b", "c"]) == ["stew"] and fetched == ["stew"]


def test_forget_saved_maps_again_and_never_saves_twice(store):
    svc, mnd, mealie = make(store, {"smoothie": R["smoothie"]})
    svc.refresh()
    svc.sync(None)
    mealie.recipes["smoothie"]["updated"] = "2"
    svc.refresh()
    with pytest.raises(ValueError):
        svc.forget_saved("nope")
    svc.forget_saved("smoothie")  # not really deleted in MyNetDiary
    r = store.get_recipe("smoothie")
    assert (r["status"], r["mnd_food_id"], r["saved_at"]) == ("duplicate", None, None)
    assert svc.sync(["smoothie"])["saved"] == [] and mnd.saved == ["Smoothie"]
    svc.refresh()
    assert store.get_recipe("smoothie")["status"] == "duplicate"
    del mnd._existing["smoothie"]  # now deleted there
    svc.refresh()  # unchanged in Mealie: still duplicate until re-checked
    svc.remap("smoothie")
    assert store.get_recipe("smoothie")["status"] == "ready"
    assert svc.sync(None)["saved"] == ["smoothie"] and svc.sync(None)["saved"] == []
    assert mnd.saved == ["Smoothie", "Smoothie"]


def test_refresh_counts_linked_and_flags_first_run_with_nothing_to_link(store, tmp_path):
    svc, mnd, _ = make(store, R)
    c = svc.refresh()
    assert (c["new"], c["linked"], c["check_links"]) == (3, 0, True)
    svc.sync(None)
    assert svc.refresh()["check_links"] is False  # saved rows exist now
    svc2, *_ = make(Store(tmp_path / "other.db"), R, existing={"porridge": "1"})
    c = svc2.refresh()
    assert (c["new"], c["linked"], c["check_links"]) == (2, 1, False)


def test_failed_recipe_is_logged_without_message(store, caplog):
    import logging
    svc, mnd, mealie = make(store, {"smoothie": R["smoothie"]})
    mealie.recipes["smoothie"]["boom"] = True
    with caplog.at_level(logging.WARNING, logger="mnd_sync"):
        svc.refresh()
    assert "recipe smoothie failed (RuntimeError)" in caplog.text and "mealie 500" not in caplog.text
