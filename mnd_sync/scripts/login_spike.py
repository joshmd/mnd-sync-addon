#!/usr/bin/env python3
"""Spike: can an email+password mobile login yield a session the web mui*.do
endpoints accept?  Prints only PASS/FAIL and response field NAMES, never values.

    MND_EMAIL=you@example.com python scripts/login_spike.py   # prompts for password
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from mynetdiary.client import MndClient  # noqa: E402
from mynetdiary.errors import MndAuthError  # noqa: E402
from mynetdiary.login import mobile_login  # noqa: E402

def session_works(client) -> bool:
    try:
        client.existing_recipes()
    except MndAuthError:
        return False
    return True


def main() -> int:
    email = os.environ.get("MND_EMAIL") or input("MyNetDiary email: ").strip()
    password = getpass.getpass("MyNetDiary password (hidden): ")
    s = requests.Session()
    try:
        fields = mobile_login(s, email, password)
    except MndAuthError:
        print("FAIL: login rejected")
        return 1
    print("login response fields:", sorted(fields))
    print("cookies set:", sorted(c.name for c in s.cookies))
    ok = session_works(MndClient(session=s, dry_run=True))
    print("PASS" if ok else "FAIL: session not accepted by mui*.do endpoints")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
