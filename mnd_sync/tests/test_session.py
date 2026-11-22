"""An expired MyNetDiary session must stop the run, not look like 'no matches'."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.errors import MndAuthError  # noqa: E402
from sync.batch import sync_all  # noqa: E402

LOGGED_IN = {"userType": "Free", "securityToken": "x"}


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


class _Session:
    def __init__(self, data):
        self.data, self.headers, self.cookies = data, {}, None

    def request(self, method, url, timeout=None, **kw):
        return _Resp(self.data)


def client_returning(data):
    return MndClient(session=_Session(data), dry_run=True)


def test_logged_in_response_passes():
    c = client_returning({**LOGGED_IN, "foundFoods": [{"foodId": "v1.a=", "foodDesc": "Banana"}]})
    assert [h.description for h in c.search_foods("banana")] == ["Banana"]


def test_logged_in_empty_search_is_not_an_auth_error():
    assert client_returning({**LOGGED_IN, "foundFoods": []}).search_foods("zzz") == []


@pytest.mark.parametrize("data", [{}, {"foundFoods": []}, {"redirect": "/login.do"}])
def test_response_without_account_details_raises(data):
    with pytest.raises(MndAuthError, match="session has most likely expired"):
        client_returning(data).search_foods("banana")


def test_error_text_response_is_still_logged_in():
    c = client_returning({**LOGGED_IN, "errorText": "bad amount"})
    with pytest.raises(Exception, match="bad amount"):
        c.get_food_entry("v1.a=", "1 medium")


def test_sync_all_aborts_on_expired_session():
    class Mealie:
        def list_all_recipes(self):
            raise AssertionError("should stop before listing Mealie recipes")

    with pytest.raises(MndAuthError):
        sync_all(client_returning({}), Mealie())
