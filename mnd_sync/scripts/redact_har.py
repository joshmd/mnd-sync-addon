#!/usr/bin/env python3
"""Blank passwords, logins and cookie values in a browser HAR before sharing it.

    py scripts\\redact_har.py capture.har      -> writes capture.redacted.har

Keeps endpoints, field names and cookie names (needed to rebuild the flows);
replaces every password/token/email-like value and every cookie value.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

SECRET_KEY = re.compile(r"pass|pwd|secret|token|logon|login|email|user|auth", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
MASK = "REDACTED"


def _scrub(obj):
    """Recursively blank secret-looking keys in parsed JSON."""
    if isinstance(obj, dict):
        return {k: (MASK if SECRET_KEY.search(k) and isinstance(v, (str, int, float)) else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return EMAIL.sub(MASK, obj) if isinstance(obj, str) else obj


def _scrub_text(text: str, mime: str) -> str:
    if not text:
        return text
    if "json" in mime or text.lstrip()[:1] in "{[":
        try:
            return json.dumps(_scrub(json.loads(text)))
        except ValueError:
            pass
    if "=" in text and "\n" not in text:
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return urlencode([(k, MASK if SECRET_KEY.search(k) else EMAIL.sub(MASK, v)) for k, v in pairs])
    return EMAIL.sub(MASK, text)


def _cookie_header(value: str) -> str:
    return "; ".join(f"{p.split('=', 1)[0].strip()}={MASK}" if "=" in p else p
                     for p in value.split(";"))


def redact(har: dict) -> dict:
    for e in har.get("log", {}).get("entries", []):
        req, resp = e.get("request", {}), e.get("response", {})
        for part in (req, resp):
            for c in part.get("cookies", []):
                c["value"] = MASK
            for h in part.get("headers", []):
                n = h.get("name", "").lower()
                if n == "cookie":
                    h["value"] = _cookie_header(h["value"])
                elif n == "set-cookie":
                    h["value"] = h["value"].split("=", 1)[0] + "=" + MASK
                elif n in ("authorization", "x-csrf-token"):
                    h["value"] = MASK
        for q in req.get("queryString", []):
            if SECRET_KEY.search(q.get("name", "")):
                q["value"] = MASK
        req["url"] = EMAIL.sub(MASK, req.get("url", ""))
        pd = req.get("postData")
        if pd:
            pd["text"] = _scrub_text(pd.get("text", ""), pd.get("mimeType", ""))
            for p in pd.get("params", []):
                if SECRET_KEY.search(p.get("name", "")):
                    p["value"] = MASK
        content = resp.get("content", {})
        if content.get("text"):
            content["text"] = _scrub_text(content["text"], content.get("mimeType", ""))
    return har


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    raw = src.read_text(encoding="utf-8")
    har, _ = json.JSONDecoder().raw_decode(raw)
    out = src.with_suffix(".redacted.har")
    out.write_text(json.dumps(redact(har)), encoding="utf-8")
    print(f"wrote {out} — passwords, logins and cookie values replaced with {MASK}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
