import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from login_spike import session_works  # noqa: E402
from mynetdiary.login import device_fields, mobile_login, parse_mnd  # noqa: E402
from mynetdiary.errors import MndAuthError  # noqa: E402


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeSession:
    def __init__(self, text):
        self.text = text
        self.headers = {}
        self.posted = []

    def post(self, url, data=None, **kw):
        self.posted.append(data)
        return FakeResp(self.text)


class FakeClient:
    def __init__(self, exc=None):
        self.exc = exc

    def existing_recipes(self):
        if self.exc:
            raise self.exc
        return {}


def test_parse_ok():
    f = parse_mnd("OK 42`+`idtext userId 7`+`text hi`+`json {}")
    assert f == {"_status": "42", "userId": "7", "text": "hi", "json": "{}"}


def test_parse_error():
    assert parse_mnd("ERROR Invalid password") == {"_error": "Invalid password"}


def test_mobile_login_raises_on_error():
    with pytest.raises(MndAuthError):
        mobile_login(FakeSession("ERROR Invalid password"), "a@b.c", "x")


def test_session_works_false_on_auth_error():
    assert session_works(FakeClient(MndAuthError("no"))) is False
    assert session_works(FakeClient()) is True


def test_device_fields_reuse_stored_ids():
    ids = {"deviceId": "d1", "installationId": "i1"}
    f = device_fields(ids)
    assert (f["deviceId"], f["installationId"]) == ("d1", "i1")
    assert device_fields()["deviceId"] != device_fields()["deviceId"]


def test_mobile_login_sends_stored_device_ids():
    s = FakeSession("OK 1")
    mobile_login(s, "a@b.c", "x", {"deviceId": "d1", "installationId": "i1"})
    assert s.posted[0]["deviceId"] == "d1"
