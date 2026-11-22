import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Request
from fastapi.testclient import TestClient

from app.web import create_app, redirect

PREFIX = "/api/hassio_ingress/abc"
HEADERS = {"X-Ingress-Path": PREFIX}


def test_home_links_use_ingress_prefix(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/", headers=HEADERS)
    assert r.status_code == 200
    links = re.findall(r'(?:href|action)="([^"]*)"', r.text)
    assert links
    assert all(link.startswith(PREFIX + "/") for link in links)


def test_redirect_is_prefixed(tmp_path):
    app = create_app(tmp_path)

    @app.get("/go")
    def go(request: Request):
        return redirect(request, "/recipes")

    r = TestClient(app).get("/go", headers=HEADERS, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == PREFIX + "/recipes"


def test_only_ingress_client_is_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("MND_ALLOW_ANY_CLIENT")
    app = create_app(tmp_path)
    r = TestClient(app, client=("172.30.32.9", 5000)).get("/")
    assert r.status_code == 403 and "Forbidden" in r.text and "<" not in r.text
    assert TestClient(app, client=("172.30.32.2", 5000)).get("/").status_code == 200


def test_bad_ingress_header_is_ignored(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/", headers={"X-Ingress-Path": "https://evil.example"})
    assert "evil" not in r.text and 'href="/recipes"' in r.text
