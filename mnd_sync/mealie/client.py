"""Minimal Mealie REST client.

Mealie uses a Bearer API token (Settings -> API Tokens). Only the reads the
sync needs are implemented: list recipes and fetch one by slug. This is a thin
scaffold — expand as the sync grows.
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .models import MealieRecipe


class MealieError(RuntimeError):
    """Mealie could not be reached, rejected the token, or returned non-JSON."""


# Path fragments people often paste into MEALIE_URL from the browser or docs.
# The API lives at <server root>/api/..., so anything from these on is dropped.
_URL_SUFFIXES_TO_DROP = ("/api", "/g/", "/login", "/recipes", "/recipe/")


def normalize_base_url(url: str) -> str:
    """Reduce a pasted Mealie URL to the server root the API hangs off.

    "https://m.example/g/home" -> "https://m.example"
    "http://host:9000/api/"    -> "http://host:9000"
    """
    url = url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    lower = url.lower()
    scheme_end = lower.find("://") + 3 if "://" in lower else 0
    cut = len(url)
    for marker in _URL_SUFFIXES_TO_DROP:
        i = lower.find(marker, scheme_end)
        if i != -1 and (i + len(marker) == len(lower) or marker.endswith("/")
                        or lower[i + len(marker)] == "/"):
            cut = min(cut, i)
    return url[:cut].rstrip("/")


_HA_URL_MARKERS = (":8123", "/app/", "hassio_ingress", "/hassio/")


def looks_like_home_assistant_url(url: str) -> bool:
    """True for a Home Assistant add-on panel URL rather than Mealie itself."""
    lower = url.lower()
    return any(m in lower for m in _HA_URL_MARKERS)


class MealieClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0,
                 session=None):
        if requests is None:
            raise RuntimeError("The 'requests' package is required: pip install requests")
        if looks_like_home_assistant_url(base_url):
            raise MealieError(
                f"MEALIE_URL {base_url!r} is a Home Assistant add-on page, not Mealie's "
                "API. Home Assistant requires its own login there, so scripts can't use "
                "it. In Home Assistant open Settings -> Add-ons -> Mealie -> "
                "Configuration -> Network, set 9000/tcp to 9000, save, then use "
                "MEALIE_URL=http://<home-assistant-ip>:9000")
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers.setdefault("Accept", "application/json")

    @classmethod
    def from_env(cls, **kwargs) -> "MealieClient":
        base = os.environ.get("MEALIE_URL")
        token = os.environ.get("MEALIE_TOKEN")
        if not base or not token:
            raise MealieError("Set MEALIE_URL and MEALIE_TOKEN.")
        return cls(base, token, **kwargs)

    def _get(self, path: str, **kwargs) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:  # type: ignore[union-attr]
            raise MealieError(f"could not reach Mealie at {self.base_url}: {e}") from e
        if resp.status_code in (401, 403):
            raise MealieError(
                f"Mealie rejected the API token (HTTP {resp.status_code}) at {resp.url}. "
                "Check MEALIE_TOKEN (Mealie -> Profile -> API Tokens).")
        if resp.status_code >= 400:
            raise MealieError(f"Mealie returned HTTP {resp.status_code} for {resp.url}")
        try:
            return resp.json()
        except ValueError:
            ctype = resp.headers.get("Content-Type", "") or "no content type"
            redirected = f" (redirected from {url})" if resp.url != url else ""
            snippet = " ".join(resp.text[:160].split())
            raise MealieError(
                f"expected JSON from {resp.url}{redirected} but got {ctype} "
                f"(HTTP {resp.status_code}). This usually means MEALIE_URL is not "
                "Mealie's server address: use the root, e.g. http://192.168.1.10:9000, "
                "not a page URL, and not a Home Assistant add-on panel or a login "
                f"proxy in front of Mealie. Response starts: {snippet!r}") from None

    def check(self) -> dict[str, Any]:
        """Diagnose connectivity: server reachable (public) and token valid.

        /api/app/about needs no auth, so it separates a wrong URL from a bad
        token. /api/users/self needs the token.
        """
        about = self._get("/api/app/about")
        me = self._get("/api/users/self")
        return {
            "base_url": self.base_url,
            "version": about.get("version") if isinstance(about, dict) else None,
            "user": (me.get("username") or me.get("fullName")) if isinstance(me, dict) else None,
        }

    def list_recipes(self, *, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        """One page of recipe summaries (each has at least 'slug' and 'name')."""
        data = self._get("/api/recipes", params={"page": page, "perPage": per_page})
        return data.get("items", data) if isinstance(data, dict) else data

    def list_all_recipes(self, *, per_page: int = 50,
                         max_pages: int = 1000) -> list[dict[str, Any]]:
        """All recipe summaries, following Mealie's pagination.

        Mealie returns {"items": [...], "page": n, "total_pages": m, ...}. Stops
        at the last page, on an empty page, or on a short page when the total
        isn't reported.
        """
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self._get("/api/recipes", params={"page": page, "perPage": per_page})
            if isinstance(data, list):  # unpaginated response
                out.extend(data)
                break
            items = data.get("items") or []
            out.extend(items)
            total_pages = data.get("total_pages") or data.get("totalPages")
            if not items:
                break
            if total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(items) < per_page:
                break
        return out

    def get_recipe(self, slug: str) -> MealieRecipe:
        return MealieRecipe.from_api(self._get(f"/api/recipes/{slug}"))
