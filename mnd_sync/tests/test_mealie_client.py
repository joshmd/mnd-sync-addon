"""Offline tests for the Mealie client's URL handling and error reporting."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mealie.client import MealieClient, MealieError, normalize_base_url  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("http://192.168.1.10:9000", "http://192.168.1.10:9000"),
    ("http://192.168.1.10:9000/", "http://192.168.1.10:9000"),
    ("http://host:9000/api", "http://host:9000"),
    ("http://host:9000/api/", "http://host:9000"),
    ("https://mealie.example/g/home", "https://mealie.example"),
    ("https://mealie.example/login?redirect=/", "https://mealie.example"),
    ("https://recipes.example.com", "https://recipes.example.com"),  # host not cut
    ("https://x.example/apiary", "https://x.example/apiary"),         # not an /api segment
])
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


class _Resp:
    def __init__(self, status=200, text="", ctype="text/html", url="http://h/api/recipes"):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {"Content-Type": ctype}

    def json(self):
        import json
        return json.loads(self.text)


class _Session:
    def __init__(self, resp):
        self.resp, self.headers = resp, {}

    def get(self, url, timeout=None, **kw):
        return self.resp


def _client(resp):
    return MealieClient("http://h", "tok", session=_Session(resp))


def test_html_response_raises_clear_error():
    c = _client(_Resp(text="<!DOCTYPE html><html>Mealie</html>", url="http://h/login"))
    with pytest.raises(MealieError) as e:
        c.list_recipes()
    msg = str(e.value)
    assert "expected JSON" in msg and "text/html" in msg
    assert "redirected from http://h/api/recipes" in msg
    assert "tok" not in msg  # never echo the token


def test_bad_token_message():
    with pytest.raises(MealieError, match="rejected the API token"):
        _client(_Resp(status=401, text="{}", ctype="application/json")).list_recipes()


def test_json_ok():
    c = _client(_Resp(text='{"items": [{"slug": "a"}]}', ctype="application/json"))
    assert c.list_recipes() == [{"slug": "a"}]


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:8123/app/a0d7b954_mealie",
    "http://homeassistant.local:8123/api/hassio_ingress/abc/",
])
def test_home_assistant_url_rejected_with_guidance(url):
    with pytest.raises(MealieError, match="Home Assistant add-on page"):
        MealieClient(url, "tok", session=_Session(_Resp()))


def test_direct_addon_port_accepted():
    c = MealieClient("http://192.168.1.10:9000", "tok", session=_Session(_Resp()))
    assert c.base_url == "http://192.168.1.10:9000"
