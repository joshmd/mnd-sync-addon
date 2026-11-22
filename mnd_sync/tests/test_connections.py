import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import connections
from app.connections import MealieSession, MndSession, mealie_create_token
from app.credentials import CredentialStore
from app.store import Store
from mealie.client import MealieError
from mynetdiary.errors import MndAuthError


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, token_status=200):
        self.calls, self.token_status = [], token_status

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if url.endswith("/api/auth/token"):
            return Resp(self.token_status, {"access_token": "bearer-1"})
        return Resp(200, {"token": "api-token-1"})


def test_mealie_token_flow_stores_only_token():
    http = FakeHttp()
    token = mealie_create_token("http://m:9000/", "me", "pw", http=http)
    assert token == "api-token-1"
    (u1, k1), (u2, k2) = http.calls
    assert u1 == "http://m:9000/api/auth/token" and k1["data"] == {"username": "me", "password": "pw"}
    assert u2 == "http://m:9000/api/users/api-tokens" and k2["json"] == {"name": "mnd-sync"}
    assert k2["headers"] == {"Authorization": "Bearer bearer-1"}


def test_mealie_token_rejected_login_raises_without_secrets():
    with pytest.raises(MealieError) as e:
        mealie_create_token("http://m:9000", "me", "pw", http=FakeHttp(token_status=401))
    assert "pw" not in str(e.value)


def test_mealie_session_needs_token(tmp_path):
    store, creds = Store(tmp_path / "s.db"), CredentialStore(tmp_path / "c.json")
    with pytest.raises(MealieError):
        MealieSession(store, creds).client()
    creds.save(mealie_token="t")
    store.set_setting("mealie_url", "http://m:9000")
    assert MealieSession(store, creds).client().base_url == "http://m:9000"


class FakeMndClient:
    def __init__(self, session, dry_run):
        self.session, self.dry_run = session, dry_run
        self.working = session.logged_in

    def existing_recipes(self):
        if not self.working:
            raise MndAuthError("expired")
        return {}


class FakeHttpSession:
    logged_in = True


def test_mnd_relogin_on_auth_error(tmp_path, monkeypatch):
    creds = CredentialStore(tmp_path / "c.json")
    creds.save(mnd_email="a@b.c", mnd_password="pw")
    logins = []

    def login(session, email, password, ids):
        logins.append(ids)
        session.logged_in = len(logins) > 1  # first login yields a dead session

    monkeypatch.setattr(connections, "MndClient", FakeMndClient)
    s = MndSession(creds, login=login, http=FakeHttpSession)
    first = s.client()
    assert s.client() is first and len(logins) == 1
    with pytest.raises(MndAuthError):
        first.existing_recipes()
    second = s.relogin()
    assert second is not first and second.existing_recipes() == {} and second.dry_run is False
    assert logins[0] == logins[1] == json.loads(creds.get("mnd_device"))  # one device id, reused


def test_mnd_cookie_mode_and_missing(tmp_path):
    creds = CredentialStore(tmp_path / "c.json")
    with pytest.raises(MndAuthError):
        MndSession(creds).client()
    creds.save(mnd_jsessionid="abc")
    assert MndSession(creds).client().session.cookies.get("JSESSIONID") == "abc"
