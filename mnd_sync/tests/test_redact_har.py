"""The HAR redactor must remove every credential before a capture is shared."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.redact_har import redact  # noqa: E402

PW, EMAIL, SID = "hunter2-secret", "me@example.org", "ABCDEF123456SESSION"


def har():
    return {"log": {"entries": [{
        "request": {
            "url": "https://www.mynetdiary.com/logon.do?next=/home",
            "headers": [{"name": "Cookie", "value": f"JSESSIONID={SID}; theme=dark"}],
            "cookies": [{"name": "JSESSIONID", "value": SID}],
            "queryString": [],
            "postData": {"mimeType": "application/x-www-form-urlencoded",
                         "text": f"logonName={EMAIL}&password={PW}&rememberMe=true",
                         "params": [{"name": "password", "value": PW}]},
        },
        "response": {
            "headers": [{"name": "Set-Cookie", "value": f"JSESSIONID={SID}; Path=/; HttpOnly"}],
            "cookies": [{"name": "JSESSIONID", "value": SID}],
            "content": {"mimeType": "application/json",
                        "text": json.dumps({"userType": "Free", "email": EMAIL, "securityToken": "tok123"})},
        },
    }, {
        "request": {"url": "https://www.mynetdiary.com/muiSaveFoodEntry.do", "headers": [], "cookies": [],
                    "postData": {"mimeType": "application/json",
                                 "text": json.dumps({"foodId": "v1.x=", "day": "2026-11-30", "mealTypeId": 3})}},
        "response": {"headers": [], "cookies": [], "content": {"text": "{}"}},
    }]}}


def test_no_secret_survives():
    out = json.dumps(redact(har()))
    for secret in (PW, EMAIL, SID, "tok123"):
        assert secret not in out


def test_structure_needed_for_rebuild_is_kept():
    h = redact(har())
    login, save = h["log"]["entries"]
    assert "logon.do" in login["request"]["url"]
    assert "rememberMe=true" in login["request"]["postData"]["text"]
    assert login["response"]["headers"][0]["value"].startswith("JSESSIONID=")
    body = json.loads(save["request"]["postData"]["text"])
    assert body == {"foodId": "v1.x=", "day": "2026-11-30", "mealTypeId": 3}
