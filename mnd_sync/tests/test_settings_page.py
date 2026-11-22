import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.credentials import CredentialStore
from app.store import Store
from app.web import create_app
from mealie.client import MealieError
from mynetdiary.errors import MndAuthError

SECRETS = {"mnd_email": "me@example.test", "mnd_password": "pw-secret-1", "mealie_token": "tok-secret-2",
           "mnd_jsessionid": "cookie-secret-3"}


class FakeMnd:
    def __init__(self, ok=True):
        self.ok = ok

    def client(self):
        return self

    def relogin(self):
        return self

    def existing_recipes(self):
        if not self.ok:
            raise MndAuthError("https://x/?token=leak")
        return {}


class FakeMealie:
    def __init__(self, ok=True):
        self.ok = ok

    def client(self):
        return self

    def check(self):
        if not self.ok:
            raise MealieError("Mealie rejected the API token")
        return {"base_url": "http://m", "version": "v2.1", "user": "josh"}


class FakeConns:
    def __init__(self, mnd_ok=True, mealie_ok=True):
        self.mnd, self.mealie = FakeMnd(mnd_ok), FakeMealie(mealie_ok)
        self.created = []

    def create_token(self, base, user, pw):
        self.created.append((base, user, pw))
        return "minted-token"


class NoNotes:
    def send(self, title, message):
        pass


def make(tmp_path, saved=True, **kw):
    if saved:  # the test buttons only test what is saved
        CredentialStore(tmp_path / "credentials.json").save(**SECRETS)
    conns = FakeConns(**kw)
    return TestClient(create_app(tmp_path, connections=conns, notifier=NoNotes())), conns


def test_settings_page_never_shows_secrets(tmp_path):
    c, _ = make(tmp_path, saved=False)
    assert "<em>saved</em>" not in c.get("/settings").text  # the auto-sync note also says "saved"
    r = c.post("/settings", data={**SECRETS, "mealie_url": "http://m:9000"})
    assert r.status_code == 200
    page = c.get("/settings").text
    for v in SECRETS.values():
        assert v not in page
    assert page.count("<em>saved</em>") == 4
    assert CredentialStore(tmp_path / "credentials.json").get("mnd_password") == "pw-secret-1"


def test_blank_fields_keep_saved_secrets(tmp_path):
    c, _ = make(tmp_path)
    c.post("/settings", data=SECRETS)
    c.post("/settings", data={"mealie_url": "http://other:9000", "mnd_password": ""})
    creds = CredentialStore(tmp_path / "credentials.json")
    assert creds.get("mnd_password") == "pw-secret-1"
    assert Store(tmp_path / "mnd_sync.db").get_setting("mealie_url") == "http://other:9000"


def test_username_password_mints_token_and_stores_only_token(tmp_path):
    c, conns = make(tmp_path)
    c.post("/settings", data={"mealie_url": "http://m:9000", "mealie_username": "u", "mealie_password": "mp"})
    creds = CredentialStore(tmp_path / "credentials.json")
    assert conns.created == [("http://m:9000", "u", "mp")]
    assert creds.get("mealie_token") == "minted-token"
    assert not creds.has("mealie_password") and not creds.has("mealie_username")


def test_test_buttons_report_ok_and_failure(tmp_path):
    c, _ = make(tmp_path)
    assert "Mealie OK" in c.post("/settings/test-mealie").text
    assert "MyNetDiary OK" in c.post("/settings/test-mnd").text
    c, _ = make(tmp_path, mnd_ok=False, mealie_ok=False)
    r = c.post("/settings/test-mealie")
    assert "Mealie failed" in r.text
    r = c.post("/settings/test-mnd")
    assert "MyNetDiary" in r.text and "leak" not in r.text


def test_autosync_stays_off_when_both_connections_ok(tmp_path):
    c, _ = make(tmp_path)
    store = Store(tmp_path / "mnd_sync.db")
    c.post("/settings/test-mealie")
    page = c.post("/settings/test-mnd").text
    assert store.get_setting("auto_sync") is None and store.get_setting("mealie_ok") and store.get_setting("mnd_ok")
    assert page.count("last test passed") == 2 and 'name="auto_sync" >' in page  # unchecked


def test_autosync_controls_save_and_validate(tmp_path):
    c, _ = make(tmp_path)
    store = Store(tmp_path / "mnd_sync.db")
    page = c.get("/settings").text
    assert "Turn this on after the first Refresh shows your existing recipes as saved." in page
    assert 'name="poll_minutes" value="30"' in page and 'name="quiet_minutes" value="60"' in page
    c.post("/settings", data={"auto_sync": "on", "poll_minutes": "15", "quiet_minutes": " 5 "})
    assert (store.get_setting("auto_sync"), store.get_setting("poll_minutes"), store.get_setting("quiet_minutes")) == ("on", 15, 5)
    assert 'name="auto_sync" checked' in c.get("/settings").text
    for bad in ("0", "-1", "x", "1.5", "٣"):
        r = c.post("/settings", data={"poll_minutes": bad, "quiet_minutes": "5", "mealie_url": "http://changed"})
        assert "Nothing was saved" in r.text
    assert (store.get_setting("auto_sync"), store.get_setting("poll_minutes")) == ("on", 15)
    assert store.get_setting("mealie_url") != "http://changed"
    c.post("/settings", data={"poll_minutes": "", "quiet_minutes": "5"})  # box unticked, blank keeps the value
    assert (store.get_setting("auto_sync"), store.get_setting("poll_minutes")) == ("off", 15)


def test_changed_connection_clears_its_last_test(tmp_path):
    c, _ = make(tmp_path)
    store = Store(tmp_path / "mnd_sync.db")
    c.post("/settings/test-mealie"), c.post("/settings/test-mnd")
    url = store.get_setting("mealie_url")
    c.post("/settings", data={"mealie_url": url})
    assert store.get_setting("mealie_ok") and store.get_setting("mnd_ok")
    c.post("/settings", data={"mealie_url": url, "mnd_password": "new"})
    assert store.get_setting("mealie_ok") and not store.get_setting("mnd_ok")
    c.post("/settings", data={"mealie_url": "http://other:9000"})
    assert not store.get_setting("mealie_ok")


def test_autosync_explicit_off_is_kept(tmp_path):
    c, _ = make(tmp_path)
    store = Store(tmp_path / "mnd_sync.db")
    store.set_setting("auto_sync", "off")
    c.post("/settings/test-mealie")
    c.post("/settings/test-mnd")
    assert store.get_setting("auto_sync") == "off"


def test_mealie_error_text_never_shown(tmp_path):
    leak = "could not reach Mealie at http://user:tok-leak-9@host:9000/api/app/about"
    c, conns = make(tmp_path)

    def boom(*a):
        raise MealieError(leak)

    conns.mealie.check = boom
    conns.create_token = boom
    for r in (c.post("/settings/test-mealie"),
              c.post("/settings", data={"mealie_username": "u", "mealie_password": "p"})):
        assert "tok-leak-9" not in r.text and "host:9000" not in r.text and "Mealie" in r.text


def test_run_now(tmp_path):
    from app.service import SyncBusy
    client, _ = make(tmp_path)
    svc = client.app.state.service
    calls = []
    svc.refresh = lambda: calls.append(1) or {}
    r = client.post("/settings/run-now", follow_redirects=True)
    assert r.status_code == 200 and calls == [1] and "Sync finished" in r.text

    def busy():
        raise SyncBusy("x")
    svc.refresh = busy
    r = client.post("/settings/run-now", follow_redirects=True)
    assert "A sync is already running" in r.text


def test_notify_service_field(tmp_path):
    client, _ = make(tmp_path)
    store = client.app.state.store
    r = client.post("/settings", data={"notify_service": "mobile_app_my_phone"})
    assert store.get_setting("notify_service") == "mobile_app_my_phone"
    assert 'value="mobile_app_my_phone"' in client.get("/settings").text
    r = client.post("/settings", data={"notify_service": "../x"})
    assert "Nothing was saved" in r.text and store.get_setting("notify_service") == "mobile_app_my_phone"
    client.post("/settings", data={"notify_service": ""})
    assert store.get_setting("notify_service") is None


def test_test_buttons_say_when_nothing_is_saved(tmp_path):
    c, conns = make(tmp_path, saved=False)
    for path, name in (("/settings/test-mealie", "Mealie"), ("/settings/test-mnd", "MyNetDiary")):
        r = c.post(path)
        assert f"{name}: nothing saved yet" in r.text and "press Save first" in r.text
    assert conns.created == []


def test_test_failure_reason_is_logged_without_secrets(tmp_path, caplog):
    c, _ = make(tmp_path, mnd_ok=False, mealie_ok=False)
    c.post("/settings/test-mealie"), c.post("/settings/test-mnd")
    logged = caplog.text
    assert "Mealie test failed (MealieError)" in logged and "MyNetDiary test failed (MndAuthError)" in logged
    assert "leak" not in logged and "rejected the API token" not in logged
