"""Persistent ingredient -> MyNetDiary food overrides ("pantry map").

Fuzzy matching gets most ingredients right, but some need a human decision once
("maida" -> "white flour"). Recording that choice makes future syncs
deterministic and lets the user correct a bad auto-match permanently. The map
is a small JSON file, keyed by a normalized ingredient name.

It stores only the chosen MND foodId + a human-readable description (+ optional
default amount). No credentials, no health data — safe to commit per-project if
desired, though a user's personal pantry map is better kept local.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_key(name: str) -> str:
    """Canonical key for an ingredient name ('Maida Flour ' -> 'maida flour')."""
    return _NORM_RE.sub(" ", (name or "").lower()).strip()


@dataclass
class AliasEntry:
    food_id: str
    description: str
    amount: Optional[str] = None  # optional default amount override
    exclude: bool = False  # leave this ingredient out of the recipe


class AliasMap:
    """A load/lookup/save wrapper over the JSON pantry map."""

    def __init__(self, entries: Optional[dict[str, AliasEntry]] = None,
                 path: Optional[Path] = None):
        self._entries: dict[str, AliasEntry] = entries or {}
        self.path = path

    # ----- lookup / mutate ------------------------------------------------

    def get(self, name: str) -> Optional[AliasEntry]:
        return self._entries.get(normalize_key(name))

    def set(self, name: str, food_id: str, description: str,
            amount: Optional[str] = None, exclude: bool = False) -> AliasEntry:
        entry = AliasEntry(food_id=food_id, description=description, amount=amount,
                           exclude=exclude)
        self._entries[normalize_key(name)] = entry
        return entry

    def __contains__(self, name: str) -> bool:
        return normalize_key(name) in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def items(self):
        return self._entries.items()

    # ----- persistence ----------------------------------------------------

    @classmethod
    def load(cls, path) -> "AliasMap":
        p = Path(path)
        if not p.exists():
            return cls(entries={}, path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = {k: AliasEntry(**v) for k, v in data.items()}
        return cls(entries=entries, path=p)

    def save(self, path=None) -> None:
        target = Path(path or self.path)
        if target is None:
            raise ValueError("no path given to save AliasMap")
        data = {k: {kk: vv for kk, vv in asdict(v).items() if vv is not None and vv is not False}
                for k, v in sorted(self._entries.items())}
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
