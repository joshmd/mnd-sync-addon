"""Credentials file: a small JSON dict, mode 0600, never shown in repr."""

import json
import os
import tempfile
import threading
from pathlib import Path

_lock = threading.Lock()


class CredentialStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"CredentialStore({self.path})"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):  # unreadable file counts as empty
            return {}

    def _write(self, data: dict) -> None:
        # temp file in the same dir so os.replace is atomic
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".creds-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def save(self, **fields: str) -> None:
        with _lock:
            self._write({**self._read(), **fields})

    def get(self, name: str):
        return self._read().get(name)

    def has(self, name: str) -> bool:
        return name in self._read()

    def clear(self, name: str) -> None:
        with _lock:
            data = self._read()
            if data.pop(name, None) is not None:
                self._write(data)
