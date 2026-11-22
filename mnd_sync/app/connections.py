"""Live connections: MyNetDiary session (login, relogin) and Mealie client/token."""

import json
import threading

import requests

from mealie.client import MealieClient, MealieError, normalize_base_url
from mynetdiary.client import MndClient
from mynetdiary.errors import MndAuthError
from mynetdiary.login import device_fields, mobile_login


def mealie_create_token(base_url: str, username: str, password: str, http=requests) -> str:
    """Log in once with username/password and mint a long-lived API token."""
    base = normalize_base_url(base_url)
    try:
        r = http.post(f"{base}/api/auth/token", data={"username": username, "password": password}, timeout=30)
        if r.status_code != 200:
            raise MealieError(f"Mealie rejected the username/password (HTTP {r.status_code})")
        r = http.post(f"{base}/api/users/api-tokens", json={"name": "mnd-sync"},
                      headers={"Authorization": f"Bearer {r.json()['access_token']}"}, timeout=30)
        if r.status_code not in (200, 201):
            raise MealieError(f"Mealie would not create an API token (HTTP {r.status_code})")
        return r.json()["token"]
    except (requests.RequestException, ValueError, KeyError) as e:
        raise MealieError(f"could not create a token ({type(e).__name__})") from None


class MealieSession:
    def __init__(self, store, creds):
        self.store, self.creds = store, creds

    def client(self) -> MealieClient:
        token = self.creds.get("mealie_token")
        if not token:
            raise MealieError("no Mealie token saved")
        return MealieClient(self.store.get_setting("mealie_url"), token)


class MndSession:
    """Lazily logged-in MyNetDiary client. Modes: password (email+password) or cookie (jsessionid)."""

    def __init__(self, creds, login=mobile_login, http=requests.Session):
        self.creds, self._login, self._http = creds, login, http
        self._client, self._key = None, None
        self._lock = threading.Lock()

    def _device_ids(self) -> dict:
        raw = self.creds.get("mnd_device")
        if raw:
            return json.loads(raw)
        ids = {k: device_fields()[k] for k in ("deviceId", "installationId")}
        self.creds.save(mnd_device=json.dumps(ids))
        return ids

    def _build(self) -> MndClient:
        email, password = self.creds.get("mnd_email"), self.creds.get("mnd_password")
        if email and password:
            session = self._http()
            self._login(session, email, password, self._device_ids())
            return MndClient(session=session, dry_run=False)
        jsessionid = self.creds.get("mnd_jsessionid")
        if jsessionid:  # shortcut: a cookie cannot be refreshed, relogin just rebuilds it
            return MndClient.from_session_id(jsessionid, dry_run=False)
        raise MndAuthError("no MyNetDiary login saved")

    def _creds_key(self):
        return tuple(self.creds.get(n) for n in ("mnd_email", "mnd_password", "mnd_jsessionid"))

    def client(self) -> MndClient:
        with self._lock:
            if self._client is None or self._key != self._creds_key():  # saved creds changed
                self._client, self._key = self._build(), self._creds_key()
            return self._client

    def relogin(self) -> MndClient:
        with self._lock:
            self._client, self._key = self._build(), self._creds_key()
            return self._client


class Connections:
    """What the web app uses to talk to MyNetDiary and Mealie; tests pass a fake with the same names."""

    def __init__(self, store, creds):
        self.mnd, self.mealie = MndSession(creds), MealieSession(store, creds)
        self.create_token = mealie_create_token
