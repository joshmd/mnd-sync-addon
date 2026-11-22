import json
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.web import create_app

H = {"X-Ingress-Path": "/api/hassio_ingress/ing"}
D = (date.today() + timedelta(days=1)).isoformat()


class FakeService:
    busy = False

    def __init__(self):
        self.calls = []

    def log_meal_plan(self, today):
        self.calls.append(today)
        return {"logged": 2, "moved": 0, "removed": 1, "waiting": 3, "user_removed": 0, "failed": 0}


def make(tmp_path):
    svc = FakeService()
    app = create_app(tmp_path, connections=SimpleNamespace(mnd=None, mealie=None), service=svc)
    return TestClient(app), app.state.store, svc


def put(store, plan_id, state, meal=1, slug="soup", title="Soup"):
    store.put_plan_entry(plan_id, date=D, meal=meal, slug=slug, title=title, state=state)


def test_mealplan_lists_rows_with_states(tmp_path):
    c, store, _ = make(tmp_path)
    store.upsert_recipe("soup", "Soup", "t", "{}", "review")
    for i, (state, meal) in enumerate([("logged", 1), ("waiting", 2), ("note", 3), ("failed", 4), ("removed_by_user", 4)], 1):
        put(store, i, state, meal)
    t = c.get("/mealplan", headers=H).text
    for text in ("in diary", "waiting for recipe to sync", "note only, not logged", "failed, will retry",
                 "removed by you", "Breakfast", "Lunch", "Dinner", "Snacks", D, "Soup"):
        assert text in t
    assert "/api/hassio_ingress/ing/recipes/soup" in t


def test_mealplan_run_redirects_with_message(tmp_path):
    c, _, svc = make(tmp_path)
    r = c.post("/mealplan/run", headers=H, follow_redirects=False)
    assert svc.calls == [date.today()] and r.status_code == 303
    assert "Meal%20plan%3A%202%20added%2C%201%20removed%2C%203%20waiting" in r.headers["location"]


def test_review_lists_waiting_plan_rows(tmp_path):
    c, store, _ = make(tmp_path)
    assert "Planned but not in MyNetDiary" not in c.get("/review", headers=H).text
    put(store, 1, "logged")
    assert "Planned but not in MyNetDiary" not in c.get("/review", headers=H).text
    put(store, 2, "waiting", meal=3, title="Stew")
    t = c.get("/review", headers=H).text
    assert "Planned but not in MyNetDiary" in t and "Stew" in t and "Dinner" in t


def test_settings_checkbox_saves_log_meal_plan(tmp_path):
    c, store, _ = make(tmp_path)
    c.post("/settings", data={"log_meal_plan": "on"}, headers=H)
    assert store.get_setting("log_meal_plan") == "on"
    c.post("/settings", data={}, headers=H)
    assert store.get_setting("log_meal_plan") == "off"
