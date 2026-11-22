import json

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def test_settings_defaults(store):
    assert store.get_setting("mealie_url") == "http://a0d7b954-mealie:9000"
    assert store.get_setting("auto_sync") is None
    assert store.get_setting("poll_minutes") == 30
    assert store.get_setting("quiet_minutes") == 60
    assert store.get_setting("notify_service") is None
    store.set_setting("poll_minutes", 5)
    store.set_setting("auto_sync", "on")
    assert store.get_setting("poll_minutes") == 5
    assert store.get_setting("auto_sync") == "on"


def test_override_beats_alias(store):
    store.set_alias("Rice", "1", "white rice")
    store.set_override("x", "rice", "2", "brown rice", amount="100 g")
    assert store.alias_map_for("x").get("rice").food_id == "2"
    assert store.alias_map_for("y").get("rice").food_id == "1"
    assert store.alias_map_for(None).get("rice").food_id == "1"


def test_exclude_round_trips(store):
    store.set_alias("salt", "", "", exclude=True)
    assert store.alias_map_for(None).get("salt").exclude is True
    assert store.list_aliases()[0]["exclude"] is True
    store.delete_alias("salt")
    assert store.alias_map_for(None).get("salt") is None


def test_import_pantry_counts_entries(store):
    text = json.dumps({"maida": {"food_id": "9", "description": "flour"},
                       "ghee": {"food_id": "8", "description": "ghee", "amount": "1 tbsp"}})
    assert store.import_pantry(text) == 2
    assert store.alias_map_for(None).get("ghee").amount == "1 tbsp"
    with pytest.raises(ValueError):
        store.import_pantry("not json")


def test_recipes_upsert_and_notified(store):
    store.upsert_recipe("a", "A", "t1", "{}", "ready")
    store.set_notified("a", "ready")
    store.upsert_recipe("a", "A", "t2", "{}", "saved", mnd_food_id="7")
    r = store.get_recipe("a")
    assert (r["mealie_updated"], r["status"], r["mnd_food_id"], r["notified"]) == ("t2", "saved", "7", "ready")
    assert [x["slug"] for x in store.list_recipes("saved")] == ["a"]
    assert store.list_recipes("error") == [] and store.get_recipe("zz") is None


def test_runs(store):
    assert store.last_run() is None
    rid = store.start_run("manual")
    store.finish_run(rid, {"saved": 2}, None)
    run = store.last_run()
    assert run["trigger"] == "manual" and run["counts"] == {"saved": 2} and run["error"] is None
    assert run["ended_at"]


def test_set_setting_none_or_empty_means_unset(tmp_path):
    s = Store(tmp_path / "s.db")
    s.set_setting("notify_service", "notify.x")
    s.set_setting("notify_service", None)
    assert s.get_setting("notify_service") is None
    s.set_setting("mealie_url", "http://x")
    s.set_setting("mealie_url", "")
    assert s.get_setting("mealie_url") == "http://a0d7b954-mealie:9000"


def test_mealie_id_and_rename(store):
    store.upsert_recipe("a", "A", "t", "{}", "saved", mnd_food_id="7", mealie_id="m1")
    store.set_notified("a", "ready")
    store.rename_recipe("a", "b")
    r = store.get_recipe("b")
    assert (r["mealie_id"], r["mnd_food_id"], r["notified"]) == ("m1", "7", "ready")
    assert store.get_recipe("a") is None


def test_changed_at_set_on_create_and_on_new_mealie_marker(store):
    store.upsert_recipe("a", "A", "t1", "{}", "ready")
    first = store.get_recipe("a")["changed_at"]
    assert first
    store._db.execute("UPDATE recipes SET changed_at='old' WHERE slug='a'")
    store.upsert_recipe("a", "A2", "t1", "{}", "review")  # same marker: keeps the time
    assert store.get_recipe("a")["changed_at"] == "old"
    store.upsert_recipe("a", "A2", "t2", "{}", "ready")  # new marker: resets it
    assert store.get_recipe("a")["changed_at"] != "old"


def test_rename_moves_overrides(store):
    store.upsert_recipe("old", "Soup", "t", None, "ready")
    store.set_override("old", "salt", "F1", "Salt")
    store.rename_recipe("old", "new")
    assert [o["slug"] for o in store.list_overrides()] == ["new"]
    assert store.alias_map_for("new").get("salt") is not None


def test_old_database_gets_stale_column(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE recipes (slug TEXT PRIMARY KEY, name TEXT, mealie_updated TEXT, mapping_json TEXT,"
               " status TEXT, mnd_food_id TEXT, saved_at TEXT, saved_mealie_updated TEXT, notified TEXT,"
               " mealie_id TEXT, changed_at TEXT)")
    db.execute("INSERT INTO recipes (slug, name, status) VALUES ('a', 'A', 'ready')")
    db.commit()
    db.close()
    assert Store(tmp_path / "old.db").get_recipe("a")["stale"] == 0


def test_plan_entry_roundtrip(store):
    store.put_plan_entry(95, date="2026-11-23", meal=3, state="waiting")
    store.put_plan_entry(95, state="logged", entry_no=301)
    row = store.get_plan_entry(95)
    assert (row["date"], row["state"], row["entry_no"]) == ("2026-11-23", "logged", 301)
    assert row["title"] is None
    assert store.list_plan_entries("2026-11-24") == []
    assert [r["plan_id"] for r in store.list_plan_entries("2026-11-23")] == [95]
    store.delete_plan_entry(95)
    assert store.get_plan_entry(95) is None


def test_plan_entry_rejects_unknown_columns(store):
    with pytest.raises(ValueError):
        store.put_plan_entry(1, date="2026-11-23", bogus="x")
