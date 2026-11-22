import os
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.autosync import run_forever, should_autosave, tick
from app.service import SyncBusy
from app.store import Store
from mealie.client import MealieError
from mynetdiary.errors import MndAuthError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))


class FakeService:
    def __init__(self, store, refresh_error=None):
        self.store, self.refresh_error = store, refresh_error
        self.refreshes, self.syncs = 0, []

    def refresh(self):
        self.refreshes += 1
        if self.refresh_error:
            raise self.refresh_error
        return {}

    def sync(self, slugs=None, *, force=False, trigger="manual"):
        self.syncs.append((slugs, force, trigger))
        for s in slugs:
            self.store.upsert_recipe(s, s.upper(), "t", "{}", "saved", mnd_food_id="1", saved_at="x")
        return {"saved": list(slugs), "skipped": [], "errors": []}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_setting("auto_sync", "on")
    return s


def put(store, slug, status="ready", name=None, age=120):
    store.upsert_recipe(slug, name or slug.title(), "t", "{}", status)
    store._db.execute("UPDATE recipes SET changed_at=? WHERE slug=?",
                      ((NOW - timedelta(minutes=age)).isoformat(), slug))


def row(**kw):
    return {"status": "ready", "saved_at": None,
            "changed_at": (NOW - timedelta(minutes=kw.pop("age", 61))).isoformat(), **kw}


def test_quiet_period_blocks_fresh_recipe():
    assert not should_autosave(row(age=59), NOW, 60)
    assert should_autosave(row(age=61), NOW, 60)
    assert not should_autosave(row(status="review"), NOW, 60)
    assert not should_autosave(row(saved_at="x"), NOW, 60)


def test_tick_saves_only_quiet_ready_recipes_unforced(store):
    put(store, "old"), put(store, "fresh", age=5), put(store, "rev", "review")
    svc, notes = FakeService(store), Notes()
    tick(svc, store, notes, NOW)
    assert svc.syncs == [(["old"], False, "auto")]
    assert any("1 new recipe" in m and "OLD" in m for _, m in notes.sent)


def test_review_notified_once(store):
    put(store, "rev", "review", name="Soup")
    svc, notes = FakeService(store), Notes()
    tick(svc, store, notes, NOW)
    tick(svc, store, notes, NOW)
    assert len(notes.sent) == 1 and "Soup" in notes.sent[0][1] and "1" in notes.sent[0][1]
    assert store.get_recipe("rev")["notified"] == "review"
    store.upsert_recipe("rev", "Soup", "t", "{}", "error")  # status change: notify again
    tick(svc, store, notes, NOW)
    assert len(notes.sent) == 2


def test_tick_survives_mealie_down(store):
    svc, notes = FakeService(store, MealieError("http://secret-url")), Notes()
    assert tick(svc, store, notes, NOW)["error"]
    tick(svc, store, notes, NOW)
    assert notes.sent == [("MyNetDiary Sync", "Mealie unreachable")]
    svc.refresh_error = None
    put(store, "a")
    tick(svc, store, notes, NOW)
    assert svc.syncs  # recovered
    svc.refresh_error = MealieError("x")
    tick(svc, store, notes, NOW)  # outage state was cleared: notifies again
    assert notes.sent.count(("MyNetDiary Sync", "Mealie unreachable")) == 2


def test_tick_survives_mnd_login_failure(store):
    svc, notes = FakeService(store, MndAuthError("bad")), Notes()
    tick(svc, store, notes, NOW)
    tick(svc, store, notes, NOW)
    assert notes.sent == [("MyNetDiary Sync", "MyNetDiary login failed")]


def test_busy_service_skips_quietly(store):
    notes = Notes()
    tick(FakeService(store, SyncBusy()), store, notes, NOW)
    assert notes.sent == []


def test_notification_has_no_secrets_or_nutrition(store):
    store.upsert_recipe("r", "Soup", "t", "{\"kcal\": 500, \"ingredients\": [\"pw-secret\"]}", "review")
    notes = Notes()
    tick(FakeService(store), store, notes, NOW)
    text = " ".join(t + m for t, m in notes.sent)
    assert "Soup" in text and "kcal" not in text and "500" not in text and "pw-secret" not in text


def test_autosync_off_does_nothing(store):
    store.set_setting("auto_sync", "off")
    put(store, "a")
    svc, notes = FakeService(store), Notes()
    stop = threading.Event()
    run_forever(svc, store, notes, stop=stop, wait=lambda m: stop.set())
    assert svc.refreshes == 0 and notes.sent == []


def test_loop_runs_tick_and_survives_exceptions(store):
    svc, notes = FakeService(store, RuntimeError("x")), Notes()
    waits = []
    stop = threading.Event()

    def wait(minutes):
        waits.append(minutes)
        if len(waits) == 2:
            stop.set()

    store.set_setting("poll_minutes", 7)
    svc.refresh_error = None
    store.list_recipes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))  # tick blows up
    run_forever(svc, store, notes, stop=stop, wait=wait)
    assert waits == [7, 7]


def test_review_ready_review_notifies_twice(store):
    svc, notes = FakeService(store), Notes()
    put(store, "r", "review", name="Soup", age=0)
    tick(svc, store, notes, NOW)
    store.upsert_recipe("r", "Soup", "t2", "{}", "review")  # fixed in Mealie, but not yet quiet
    store._db.execute("UPDATE recipes SET status='ready' WHERE slug='r'")
    tick(svc, store, notes, NOW)
    assert store.get_recipe("r")["notified"] is None
    store._db.execute("UPDATE recipes SET status='review' WHERE slug='r'")
    tick(svc, store, notes, NOW)
    assert len(notes.sent) == 2


def test_loop_survives_settings_errors_and_clamps_wait(store):
    svc, notes = FakeService(store), Notes()
    stop, waits, real = threading.Event(), [], store.get_setting
    calls = {"n": 0}

    def flaky(key, default=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sqlite locked")
        return real(key, default)

    def wait(minutes):
        waits.append(minutes)
        if len(waits) == 2:
            stop.set()

    store.get_setting = flaky
    store._db.execute("INSERT INTO settings VALUES ('poll_minutes', '0')")
    run_forever(svc, store, notes, stop=stop, wait=wait)
    assert waits == [1, 1]


def test_outage_names_the_side_that_failed(store, caplog):
    import logging
    import requests
    from mynetdiary.errors import MndRequestError
    mnd_login_down = requests.ConnectionError("https://www.mynetdiary.com/x?token=leak",
                                              request=requests.Request("POST", "https://www.mynetdiary.com/x").prepare())
    cases = [(MealieError("Mealie returned HTTP 502 for http://m/?token=leak"), "Mealie unreachable", "HTTP 502"),
             (MndRequestError("boom http://leak", status=503), "MyNetDiary unreachable", "HTTP 503"),
             (mnd_login_down, "MyNetDiary unreachable", "ConnectionError"),
             (RuntimeError("leak"), "Sync error — see the add-on log", "RuntimeError")]
    for error, message, logged in cases:
        notes = Notes()
        store.set_setting("last_outage", None)
        with caplog.at_level(logging.WARNING, logger="mnd_sync"):
            tick(FakeService(store, error), store, notes, NOW)
        assert notes.sent == [("MyNetDiary Sync", message)]
        assert logged in caplog.text
    assert "leak" not in caplog.text


def test_saved_notification_grammar(store):
    put(store, "a"), put(store, "b")
    notes = Notes()
    tick(FakeService(store), store, notes, NOW)
    assert any("2 new recipes synced" in m for _, m in notes.sent)
