from datetime import date

import pytest

from app.mealplan import MEALS, sync_plan
from app.service import SyncService
from app.store import Store
from mealie.client import MealieError
from mynetdiary.errors import MndAuthError, MndRequestError

TODAY = date(2026, 11, 23)
ZERO = dict.fromkeys(("logged", "moved", "removed", "waiting", "user_removed", "failed"), 0)


class FakeDiary:
    def __init__(self, names=None):
        self.days = {}  # day -> {entryNo: name}
        self.names = names or {}
        self.logs = []  # (food_id, day, meal, amount)
        self.fail_foods, self.auth_failures, self.relogins = set(), 0, 0
        self.save_then_fail = set()

    def client(self):
        return self

    def relogin(self):
        self.relogins += 1
        return self

    def log_on_day(self, food_id, day, meal_type_id, amount, *, dry_run=None):
        assert dry_run is False
        if self.auth_failures:
            self.auth_failures -= 1
            raise MndAuthError("dead")
        if food_id in self.fail_foods:
            raise MndRequestError("boom")
        self.logs.append((food_id, day, meal_type_id, amount))
        entries = self.days.setdefault(day, {})
        n = meal_type_id * 100 + len(entries) + 1
        while n in entries:
            n += 1
        name = self.names.get(food_id, f"Food {food_id}")
        entries[n] = name
        if food_id in self.save_then_fail:  # saved server-side, but the reply had no entryNo
            self.save_then_fail.discard(food_id)
            raise MndRequestError("returned no entryNo")
        return n, name

    def day_entries(self, day):
        return dict(self.days.get(day, {}))

    def delete_food_entry(self, day, entry_no, *, dry_run=None):
        assert dry_run is False
        del self.days[day][entry_no]

    def all(self):
        return [(d, n, name) for d, e in sorted(self.days.items()) for n, name in sorted(e.items())]


class FakeMealie:
    def __init__(self, items=None):
        self.items, self.error, self.ranges = items or [], False, []

    def client(self):
        return self

    def list_mealplans(self, start, end):
        self.ranges.append((start, end))
        if self.error:
            raise MealieError("down")
        return [dict(i) for i in self.items]


def item(pid, day, slug="smoothie", entry="dinner", name=None, title=""):
    recipe = {"slug": slug, "name": name or slug.title()} if slug else None
    return {"id": pid, "date": day, "entryType": entry, "title": title,
            "recipeId": "rid" if slug else None, "recipe": recipe}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.upsert_recipe("smoothie", "Smoothie", "1", None, "saved", mnd_food_id="F1")
    s.upsert_recipe("porridge", "Porridge", "1", None, "changed", mnd_food_id="F2")
    return s


@pytest.fixture
def mnd():
    return FakeDiary({"F1": "Smoothie", "F2": "Porridge", "F9": "Smoothie"})


def test_logs_saved_recipe_once(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24"), item(2, "2026-11-25", slug="unsynced")])
    counts = sync_plan(store, mnd, mealie, TODAY)
    assert counts == dict(ZERO, logged=1, waiting=1)
    assert mealie.ranges == [("2026-11-23", "2026-12-06")]
    assert mnd.logs == [("F1", "2026-11-24", 3, "1 serving")]
    row = store.get_plan_entry(1)
    assert (row["state"], row["entry_no"], row["food_name"], row["food_id"]) == ("logged", 301, "Smoothie", "F1")
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO
    assert len(mnd.logs) == 1 and store.get_plan_entry(2)["state"] == "waiting"


def test_meal_map():
    assert [MEALS[k] for k in ("breakfast", "lunch", "dinner", "side", "snack", "dessert", "drink", "brunch")] \
        == [1, 2, 3, 3, 4, 4, 4, 3]


def test_waiting_until_recipe_saved(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24", slug="later", entry="lunch")])
    sync_plan(store, mnd, mealie, TODAY)
    assert store.get_plan_entry(1)["state"] == "waiting" and mnd.logs == []
    store.upsert_recipe("later", "Later", "1", None, "saved", mnd_food_id="F3")
    assert sync_plan(store, mnd, mealie, TODAY)["logged"] == 1
    assert mnd.logs == [("F3", "2026-11-24", 2, "1 serving")]
    assert store.get_plan_entry(1)["state"] == "logged"


def test_note_and_deleted_recipe_are_skipped(store, mnd):
    deleted = dict(item(2, "2026-11-24", slug=None, title="Old"), recipeId="gone")
    mealie = FakeMealie([item(1, "2026-11-24", slug=None, title="Eat out"), deleted])
    sync_plan(store, mnd, mealie, TODAY)
    assert mnd.logs == []
    assert [store.get_plan_entry(i)["state"] for i in (1, 2)] == ["note", "note"]
    assert store.get_plan_entry(1)["title"] == "Eat out"


def test_moved_entry_follows(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    mealie.items = [item(1, "2026-11-26")]
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, logged=1, moved=1)
    assert mnd.all() == [("2026-11-26", 301, "Smoothie")]
    assert store.get_plan_entry(1)["mnd_day"] == "2026-11-26"


def test_removed_from_plan_deletes_entry(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    mealie.items = []
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, removed=1)
    assert mnd.all() == [] and store.get_plan_entry(1) is None


def test_user_deleted_entry_is_never_readded(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    mnd.days["2026-11-24"].clear()  # the user deletes it
    mealie.items = [item(1, "2026-11-26")]
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, user_removed=1)
    assert store.get_plan_entry(1)["state"] == "removed_by_user" and mnd.all() == []
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO
    mealie.items = [item(1, "2026-11-27")]
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO and mnd.all() == []


def test_renumbered_entry_is_never_deleted(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    mnd.days["2026-11-24"] = {301: "My own toast"}  # the user's food now has our entryNo
    mealie.items = []
    sync_plan(store, mnd, mealie, TODAY)
    assert mnd.all() == [("2026-11-24", 301, "My own toast")]
    assert store.get_plan_entry(1) is None


def test_mealie_failure_changes_nothing(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    before, row = mnd.all(), store.get_plan_entry(1)
    mealie.error = True
    with pytest.raises(MealieError):
        sync_plan(store, mnd, mealie, TODAY)
    assert mnd.all() == before and store.get_plan_entry(1) == row


def test_past_entries_are_frozen(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-23")])
    sync_plan(store, mnd, mealie, TODAY)
    mealie.items = []
    assert sync_plan(store, mnd, mealie, date(2026, 11, 24)) == ZERO
    assert mnd.all() == [("2026-11-23", 301, "Smoothie")]


def test_same_recipe_twice_tracked_separately(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24"), item(2, "2026-11-24")])
    assert sync_plan(store, mnd, mealie, TODAY)["logged"] == 2
    assert [n for _, n, _ in mnd.all()] == [301, 302]
    mealie.items = [item(2, "2026-11-24")]
    assert sync_plan(store, mnd, mealie, TODAY)["removed"] == 1
    assert mnd.all() == [("2026-11-24", 302, "Smoothie")]


def test_entries_follow_new_recipe_version(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-23"), item(2, "2026-11-25")])
    sync_plan(store, mnd, mealie, TODAY)
    store.upsert_recipe("smoothie", "Smoothie", "2", None, "saved", mnd_food_id="F9")
    for entries in mnd.days.values():  # the old recipe was retired
        for n in entries:
            entries[n] += " (deleted)"
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, logged=2, moved=2)
    assert mnd.all() == [("2026-11-23", 301, "Smoothie"), ("2026-11-25", 301, "Smoothie")]
    assert mnd.logs[-2:] == [("F9", "2026-11-23", 3, "1 serving"), ("F9", "2026-11-25", 3, "1 serving")]
    assert {store.get_plan_entry(i)["state"] for i in (1, 2)} == {"logged"}


def test_one_failure_does_not_stop_the_rest(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24", slug="porridge"), item(2, "2026-11-24"), item(3, "2026-11-25")])
    mnd.fail_foods = {"F2"}
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, logged=2, failed=1)
    assert [store.get_plan_entry(i)["state"] for i in (1, 2, 3)] == ["failed", "logged", "logged"]
    mnd.fail_foods = set()
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, logged=1)
    assert store.get_plan_entry(1)["state"] == "logged"


def test_service_log_meal_plan_relogs_on_auth_error(store, mnd):
    mnd.auth_failures = 1
    svc = SyncService(store, mnd, FakeMealie([item(1, "2026-11-24")]))
    assert svc.log_meal_plan(TODAY)["logged"] == 1
    assert mnd.all() == [("2026-11-24", 301, "Smoothie")] and mnd.relogins == 1


def test_saved_but_no_entry_no_is_adopted(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    mnd.save_then_fail = {"F1"}
    assert sync_plan(store, mnd, mealie, TODAY) == dict(ZERO, logged=1)
    row = store.get_plan_entry(1)
    assert (row["state"], row["entry_no"], row["food_name"], row["food_id"], row["mnd_day"]) \
        == ("logged", 301, "Smoothie", "F1", "2026-11-24")
    assert mnd.all() == [("2026-11-24", 301, "Smoothie")]
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO and len(mnd.all()) == 1


def test_item_outside_window_is_ignored(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    sync_plan(store, mnd, mealie, TODAY)
    mealie.items = [item(1, "2026-11-24"), item(2, "2026-11-22"), item(3, "2026-12-07")]
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO
    assert mnd.all() == [("2026-11-24", 301, "Smoothie")]
    assert store.get_plan_entry(2) is None and store.get_plan_entry(3) is None
    # a stored row whose item moved out of the window stays: it is still live, so it is not a removal
    mealie.items = [item(1, "2026-12-10")]
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO
    assert store.get_plan_entry(1)["state"] == "logged" and len(mnd.all()) == 1


def test_removed_counts_only_deleted_entries(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24"), item(2, "2026-11-25", slug="later")])
    sync_plan(store, mnd, mealie, TODAY)
    mnd.days["2026-11-24"].clear()  # the user deleted ours
    mealie.items = []
    assert sync_plan(store, mnd, mealie, TODAY) == ZERO
    assert store.get_plan_entry(1) is None and store.get_plan_entry(2) is None


def test_auth_error_mid_move_relogs_without_marking_removed(store, mnd):
    mealie = FakeMealie([item(1, "2026-11-24")])
    svc = SyncService(store, mnd, mealie)
    svc.log_meal_plan(TODAY)
    mealie.items = [item(1, "2026-11-26")]
    mnd.auth_failures = 1  # the first log_on_day after the removal
    svc.log_meal_plan(TODAY)
    assert mnd.relogins == 1
    assert mnd.all() == [("2026-11-26", 301, "Smoothie")]
    row = store.get_plan_entry(1)
    assert (row["state"], row["mnd_day"], row["entry_no"]) == ("logged", "2026-11-26", 301)
