import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app.notify as notify
from app.notify import Notifier


class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.fail:
            raise RuntimeError("boom tok-secret")


def test_no_token_is_noop(monkeypatch, caplog):
    rec = Recorder()
    monkeypatch.setattr(notify.requests, "post", rec)
    Notifier(None, "mobile_app_x").send("T", "M")
    assert rec.calls == [] and "no Supervisor token" in caplog.text


def test_sent_notification_is_logged_without_text(monkeypatch, caplog):
    caplog.set_level("INFO", logger="mnd_sync")
    ok = type("Ok", (), {"raise_for_status": lambda self: None})()
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: ok)
    Notifier("tok-secret", None).send("T", "Soup synced")
    assert "notification sent via persistent_notification/create" in caplog.text
    assert "Soup" not in caplog.text and "tok-secret" not in caplog.text


def test_sends_persistent_and_notify_service(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(notify.requests, "post", rec)
    Notifier("tok-secret", "mobile_app_x").send("T", "M")
    assert [c[0] for c in rec.calls] == [
        "http://supervisor/core/api/services/persistent_notification/create",
        "http://supervisor/core/api/services/notify/mobile_app_x"]
    for _, kw in rec.calls:
        assert kw["json"] == {"title": "T", "message": "M"}
        assert kw["headers"] == {"Authorization": "Bearer tok-secret"}


def test_persistent_only_without_service(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(notify.requests, "post", rec)
    Notifier("tok", None).send("T", "M")
    assert len(rec.calls) == 1


def test_failure_never_raises_or_logs_secret(monkeypatch, caplog):
    monkeypatch.setattr(notify.requests, "post", Recorder(fail=True))
    Notifier("tok-secret", "svc").send("T", "M")
    assert "tok-secret" not in caplog.text


def test_service_setting_read_at_send_time(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(notify.requests, "post", rec)
    current = {"v": "svc_a"}
    n = Notifier("tok", lambda: current["v"])
    n.send("T", "M")
    current["v"] = "svc_b"
    n.send("T", "M")
    assert [c[0].rsplit("/", 1)[1] for c in rec.calls if "notify/" in c[0]] == ["svc_a", "svc_b"]


@pytest.mark.parametrize("bad", ["../x", "a/b"])
def test_invalid_service_name_is_not_posted(monkeypatch, bad):
    rec = Recorder()
    monkeypatch.setattr(notify.requests, "post", rec)
    Notifier("tok", bad).send("T", "M")
    assert [c[0] for c in rec.calls] == ["http://supervisor/core/api/services/persistent_notification/create"]
