"""MyNetDiary mobile login (ipActivation.do): yields a session the web mui*.do endpoints accept."""

import uuid

from .errors import MndAuthError

ACTIVATE = "https://www.mynetdiary.com/ipActivation.do"
SEP = "`+`"
HEADERS = {"User-Agent": "okhttp/4.12.0", "Android-Version-Code": "179220", "Accept": "*/*"}


def device_fields(ids: dict | None = None) -> dict:
    """Login form fields; pass stored ids so the account keeps seeing one device."""
    ids = ids or {"deviceId": str(uuid.uuid4()), "installationId": str(uuid.uuid4())}
    return {
        "appName": "FreeAndroid", "appVersion": "9.9.2",
        "deviceId": ids["deviceId"], "deviceType": "LoginSpike:synthetic",
        "osVersion": "android 14, api 34", "userLocale": "en_GB", "tz": "0",
        "installationId": ids["installationId"],
    }


def parse_mnd(text: str) -> dict:
    """Parse 'OK <id> `+` idtext k v `+` ...' / 'ERROR <msg>' into a field dict."""
    parts = text.split(SEP)
    head = parts[0]
    fields = {}
    if head.startswith("OK "):
        fields["_status"] = head[3:].strip()
    elif head.startswith("ERROR "):
        fields["_error"] = head[6:].strip()
    for p in parts[1:]:
        if p.startswith(("idtext ", "idjson ")):
            key, _, val = p.split(" ", 1)[1].partition(" ")
            if val:
                fields[key] = val
        elif p.startswith("text "):
            fields["text"] = p[5:]
        elif p.startswith("json "):
            fields["json"] = p[5:]
    return fields


def mobile_login(session, email: str, password: str, ids: dict | None = None) -> dict:
    session.headers.update(HEADERS)
    body = {"logonName": email, "password": password, "rememberMe": "true", **device_fields(ids)}
    fields = parse_mnd(session.post(ACTIVATE, data=body, timeout=30).text)
    if "_error" in fields:
        raise MndAuthError("mobile login rejected")
    return fields
