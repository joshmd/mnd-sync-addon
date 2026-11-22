"""SQLite state for the add-on: settings, recipes, aliases, overrides, runs."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from sync.aliases import AliasMap, normalize_key

_lock = threading.Lock()

_DEFAULTS = {"mealie_url": "http://a0d7b954-mealie:9000", "poll_minutes": 30, "quiet_minutes": 60}
_INT_SETTINGS = ("poll_minutes", "quiet_minutes")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS recipes (
  slug TEXT PRIMARY KEY, name TEXT, mealie_updated TEXT, mapping_json TEXT, status TEXT,
  mnd_food_id TEXT, saved_at TEXT, saved_mealie_updated TEXT, notified TEXT, mealie_id TEXT,
  changed_at TEXT, stale INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS aliases (
  name TEXT PRIMARY KEY, food_id TEXT, description TEXT, amount TEXT, exclude INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS overrides (
  slug TEXT, name TEXT, food_id TEXT, description TEXT, amount TEXT, exclude INTEGER DEFAULT 0,
  PRIMARY KEY (slug, name));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, ended_at TEXT, trigger TEXT,
  counts TEXT, error TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry(row: dict) -> dict:
    return {**row, "exclude": bool(row["exclude"])}


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        if "stale" not in {r[1] for r in self._db.execute("PRAGMA table_info(recipes)")}:  # pre-release databases
            self._db.execute("ALTER TABLE recipes ADD COLUMN stale INTEGER DEFAULT 0")

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with _lock, self._db:
            return self._db.execute(sql, args)

    def _rows(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self._db.execute(sql, args)]

    # ----- settings -------------------------------------------------------

    def get_setting(self, key, default=None):
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return _DEFAULTS.get(key, default)
        return int(row[0]) if key in _INT_SETTINGS else row[0]

    def set_setting(self, key, value) -> None:
        if value is None or value == "":  # unset: get_setting falls back to the default
            self._write("DELETE FROM settings WHERE key=?", (key,))
            return
        self._write("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))

    # ----- recipes --------------------------------------------------------

    def upsert_recipe(self, slug, name, mealie_updated, mapping_json, status, mnd_food_id=None,
                      saved_at=None, saved_mealie_updated=None, mealie_id=None) -> None:
        # `notified` is deliberately not touched here; set_notified owns it
        self._write(
            "INSERT INTO recipes (slug, name, mealie_updated, mapping_json, status, mnd_food_id,"
            " saved_at, saved_mealie_updated, mealie_id, changed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(slug) DO UPDATE SET name=excluded.name, mealie_updated=excluded.mealie_updated,"
            " mapping_json=excluded.mapping_json, status=excluded.status, mnd_food_id=excluded.mnd_food_id,"
            " saved_at=excluded.saved_at, saved_mealie_updated=excluded.saved_mealie_updated,"
            " mealie_id=excluded.mealie_id,"
            " changed_at=CASE WHEN recipes.mealie_updated IS excluded.mealie_updated"
            " THEN recipes.changed_at ELSE excluded.changed_at END",
            (slug, name, mealie_updated, mapping_json, status, mnd_food_id, saved_at, saved_mealie_updated,
             mealie_id, _now()))

    def rename_recipe(self, old_slug, new_slug) -> None:
        with _lock, self._db:  # one transaction: per-recipe overrides follow the recipe
            self._db.execute("UPDATE recipes SET slug=? WHERE slug=?", (new_slug, old_slug))
            self._db.execute("UPDATE overrides SET slug=? WHERE slug=?", (new_slug, old_slug))

    def get_recipe(self, slug):
        rows = self._rows("SELECT * FROM recipes WHERE slug=?", (slug,))
        return rows[0] if rows else None

    def list_recipes(self, status=None) -> list[dict]:
        if status is None:
            return self._rows("SELECT * FROM recipes ORDER BY name")
        return self._rows("SELECT * FROM recipes WHERE status=? ORDER BY name", (status,))

    def mark_stale(self, slugs, stale=True) -> None:
        """Stale: an alias or override changed since this recipe was mapped; refresh re-maps it."""
        with _lock, self._db:
            self._db.executemany("UPDATE recipes SET stale=? WHERE slug=?", [(int(stale), s) for s in slugs])

    def set_notified(self, slug, status) -> None:
        self._write("UPDATE recipes SET notified=? WHERE slug=?", (status, slug))

    # ----- aliases and overrides -----------------------------------------

    def set_alias(self, name, food_id, description, amount=None, exclude=False) -> None:
        self._write("REPLACE INTO aliases VALUES (?,?,?,?,?)",
                    (normalize_key(name), food_id, description, amount, int(exclude)))

    def delete_alias(self, name) -> None:
        self._write("DELETE FROM aliases WHERE name=?", (normalize_key(name),))

    def set_override(self, slug, name, food_id, description, amount=None, exclude=False) -> None:
        self._write("REPLACE INTO overrides VALUES (?,?,?,?,?,?)",
                    (slug, normalize_key(name), food_id, description, amount, int(exclude)))

    def delete_override(self, slug, name) -> None:
        self._write("DELETE FROM overrides WHERE slug=? AND name=?", (slug, normalize_key(name)))

    def list_aliases(self) -> list[dict]:
        return [_entry(r) for r in self._rows("SELECT * FROM aliases ORDER BY name")]

    def list_overrides(self, slug=None) -> list[dict]:
        if slug is None:
            rows = self._rows("SELECT * FROM overrides ORDER BY slug, name")
        else:
            rows = self._rows("SELECT * FROM overrides WHERE slug=? ORDER BY name", (slug,))
        return [_entry(r) for r in rows]

    def alias_map_for(self, slug) -> AliasMap:
        amap = AliasMap()
        for r in self.list_aliases() + (self.list_overrides(slug) if slug else []):
            amap.set(r["name"], r["food_id"], r["description"], r["amount"], r["exclude"])
        return amap

    def import_pantry(self, json_text: str) -> int:
        try:
            data = json.loads(json_text)
            rows = [(normalize_key(k), v["food_id"], v["description"], v.get("amount"),
                     int(bool(v.get("exclude")))) for k, v in data.items()]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise ValueError(f"not a valid pantry.json: {e}") from e
        with _lock, self._db:
            self._db.executemany("REPLACE INTO aliases VALUES (?,?,?,?,?)", rows)
        return len(rows)

    # ----- runs -----------------------------------------------------------

    def start_run(self, trigger) -> int:
        return self._write("INSERT INTO runs (started_at, trigger) VALUES (?,?)", (_now(), trigger)).lastrowid

    def finish_run(self, run_id, counts: dict, error) -> None:
        self._write("UPDATE runs SET ended_at=?, counts=?, error=? WHERE id=?",
                    (_now(), json.dumps(counts), error, run_id))

    def last_run(self):
        rows = self._rows("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        return {**rows[0], "counts": json.loads(rows[0]["counts"] or "{}")}
