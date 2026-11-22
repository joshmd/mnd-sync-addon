#!/usr/bin/env python3
"""Command-line entry point for the MyNetDiary V1 client.

Auth: export MND_JSESSIONID (JSESSIONID cookie from a logged-in browser).
All write commands default to --dry-run; pass --commit to actually send.

Examples:
  export MND_JSESSIONID=XXXX
  python cli.py search "banana"
  python cli.py log --food-id "v1...=" --meal 1 --amount "2 medium"            # dry-run
  python cli.py log --food-id "v1...=" --meal 1 --amount "2 medium" --commit
  python cli.py recipe --name "Smoothie" --servings 2 \
      --ingredient "banana=2 medium" --ingredient "milk=1 cup"                 # dry-run
  python cli.py sync-recipe --slug my-mealie-recipe                            # dry-run
"""

from __future__ import annotations

import argparse
import json
import sys

from mynetdiary.client import MndClient
from mynetdiary.errors import MndError


def _client(commit: bool) -> MndClient:
    return MndClient.from_env(dry_run=not commit)


def _print(obj) -> None:
    def default(o):
        return getattr(o, "__dict__", str(o))
    print(json.dumps(obj, indent=2, default=default, ensure_ascii=False))


def cmd_search(args) -> int:
    client = MndClient.from_env()
    hits = client.search_foods(args.query, limit=args.limit)
    if not args.per_100g:
        for r in hits:
            print(f"{r.food_id}\t{r.calories or '?':>6}\t{r.description}")
        return 0
    # Look each hit up at 100 g so entries can be compared like for like
    # (e.g. to spot a parmesan entry listing 111 kcal/100 g instead of ~400).
    from sync.amounts import grams_of, number
    print("kcal/100g  db?   foodId                                   description")
    for r in hits:
        try:
            raw = client.get_food_entry(r.food_id, "100 g").raw
            g, k = grams_of(raw), number(raw.get("calories"))
            per = f"{k / g * 100:7.0f}" if g and k is not None else "      ?"
        except MndError:
            per = "    err"
        print(f"{per}    {'yes' if r.is_system else 'no ':3}   {r.food_id:40} {r.description}")
    print("\nPin one with: cli.py alias set \"<ingredient>\" --food-id <foodId>", file=sys.stderr)
    return 0


def cmd_log(args) -> int:
    client = _client(args.commit)
    if args.day:
        # Full form with an explicit day. Live testing showed the server rejects
        # this shape for NEW entries (it is the edit form), so it is experimental.
        print("warning: --day uses the edit-form body, which the server rejected "
              "for new entries in live testing; logging to the session day "
              "(omit --day) is the verified path.", file=sys.stderr)
        res = client.log_food(args.food_id, args.meal, args.amount, day=args.day)
    else:
        # Verified live: inline create on the session's current day.
        res = client.log_food_inline(args.food_id, args.meal, args.amount)
    if isinstance(res, dict) and res.get("errorText"):
        print(f"error from MyNetDiary: {res['errorText']}", file=sys.stderr)
        return 1
    if args.commit:
        entry_no = client.entry_no_from_response(res)
        print(f"logged: entryNo {entry_no}")
    else:
        _print(res)
    return 0


def cmd_delete(args) -> int:
    client = _client(args.commit)
    _print(client.delete_food_entry(args.day, args.entry_no))
    return 0


def _parse_ingredient(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"ingredient must be 'query=amount': {spec!r}")
    q, amt = spec.split("=", 1)
    return q.strip(), amt.strip()


def cmd_recipe(args) -> int:
    client = _client(args.commit)
    pairs = [_parse_ingredient(s) for s in args.ingredient]
    recipe = client.build_recipe(
        args.name, pairs, servings=args.servings,
        meal_not_recipe=args.meal_bundle,
    )
    res = client.save_recipe(recipe)
    _print(res if args.commit else res)  # dry-run returns the request body
    return 0


def cmd_sync_recipe(args) -> int:
    from mealie.client import MealieClient
    from pathlib import Path

    from sync.aliases import AliasMap
    from sync.mapper import map_mealie_recipe
    from sync.report import render_markdown

    mnd = _client(args.commit)
    mealie = MealieClient.from_env()
    mrecipe = mealie.get_recipe(args.slug)
    aliases = AliasMap.load(args.aliases) if args.aliases else None
    result = map_mealie_recipe(mnd, mrecipe, meal_not_recipe=args.meal_bundle,
                               aliases=aliases)

    report = render_markdown(result)
    print(report)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
        print(f"\n(report written to {args.report})", file=sys.stderr)

    blockers = []
    if result.needs_review:
        blockers.append(f"{len(result.needs_review)} ingredient(s) need review")
    if result.is_duplicate:
        blockers.append("a recipe of this name already exists")

    if args.commit and blockers and not args.force:
        print(f"\nNOT committing: {'; '.join(blockers)}. "
              f"Resolve, or re-run with --force.", file=sys.stderr)
        return 2

    print("\n# recipe payload (dry-run)" if not args.commit else "\n# saving...")
    _print(mnd.save_recipe(result.recipe))
    return 0


def cmd_sync_all(args) -> int:
    from pathlib import Path

    from mealie.client import MealieClient
    from sync.aliases import AliasMap
    from sync.batch import sync_all
    from sync.report import render_batch_markdown

    mnd = _client(args.commit)
    mealie = MealieClient.from_env()
    aliases = AliasMap.load(args.aliases) if args.aliases else None

    def progress(i, total, outcome):
        print(f"[{i}/{total}] {outcome.name or outcome.slug}: {outcome.status}"
              + (f" ({outcome.error})" if outcome.error else ""), file=sys.stderr)

    batch = sync_all(
        mnd, mealie, aliases=aliases, commit=args.commit, force=args.force,
        allow_duplicates=args.allow_duplicates, only=args.only or None,
        limit=args.limit, meal_not_recipe=args.meal_bundle, delay=args.delay,
        on_progress=progress)

    report = render_batch_markdown(batch)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"\n(report written to {args.report})", file=sys.stderr)
    else:
        print(report)
    return 1 if batch.had_errors else 0


def cmd_mealie_check(args) -> int:
    from mealie.client import MealieClient

    client = MealieClient.from_env()
    print(f"Mealie URL used: {client.base_url}")
    info = client.check()
    print(f"OK: Mealie {info['version'] or '(version unknown)'}; "
          f"token valid for user {info['user'] or '(unknown)'}")
    first = client.list_recipes(page=1, per_page=1)
    print(f"Recipes endpoint reachable ({'has recipes' if first else 'no recipes returned'}).")
    return 0


def cmd_alias(args) -> int:
    from sync.aliases import AliasMap

    amap = AliasMap.load(args.aliases)
    if args.alias_cmd == "list":
        for key, entry in amap.items():
            amt = f" @ {entry.amount}" if entry.amount else ""
            print(f"{key}\t-> {entry.food_id} ({entry.description}){amt}")
        return 0
    if args.alias_cmd == "set":
        amap.set(args.name, args.food_id, args.description or args.name, args.amount)
        amap.save()
        print(f"alias set: {args.name!r} -> {args.food_id}")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MyNetDiary V1 client")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_commit(sp):
        sp.add_argument("--commit", action="store_true",
                        help="actually send (default is a safe dry-run)")

    sp = sub.add_parser("search", help="search foods")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--per-100g", action="store_true",
                    help="look up each hit at 100 g and show kcal/100g (slower)")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("log", help="log a food to a day+meal")
    sp.add_argument("--food-id", required=True)
    sp.add_argument("--meal", type=int, required=True, help="mealTypeId (1=breakfast)")
    sp.add_argument("--amount", required=True)
    sp.add_argument("--day", default=None, help="YYYY-MM-DD (default today)")
    add_commit(sp)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("delete", help="delete a diary entry")
    sp.add_argument("--day", required=True)
    sp.add_argument("--entry-no", type=int, required=True)
    add_commit(sp)
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("recipe", help="create a recipe from search queries")
    sp.add_argument("--name", required=True)
    sp.add_argument("--servings", default=None)
    sp.add_argument("--ingredient", action="append", default=[],
                    help="'query=amount', repeatable")
    sp.add_argument("--meal-bundle", action="store_true",
                    help="save as a one-off meal instead of a reusable recipe")
    add_commit(sp)
    sp.set_defaults(func=cmd_recipe)

    sp = sub.add_parser("sync-recipe", help="import a Mealie recipe into MND")
    sp.add_argument("--slug", required=True)
    sp.add_argument("--meal-bundle", action="store_true")
    sp.add_argument("--aliases", default="pantry.json",
                    help="ingredient override map (JSON); default pantry.json")
    sp.add_argument("--report", default=None, help="write the review report to PATH")
    sp.add_argument("--force", action="store_true",
                    help="commit even if ingredients need review or it's a duplicate")
    add_commit(sp)
    sp.set_defaults(func=cmd_sync_recipe)

    sp = sub.add_parser("sync-all", help="import every Mealie recipe into MND")
    sp.add_argument("--aliases", default="pantry.json",
                    help="ingredient override map (JSON); default pantry.json")
    sp.add_argument("--report", default=None,
                    help="write the combined report to PATH (else print it)")
    sp.add_argument("--only", action="append", default=[], metavar="SLUG",
                    help="restrict to these Mealie slugs (repeatable)")
    sp.add_argument("--limit", type=int, default=None, help="process at most N recipes")
    sp.add_argument("--meal-bundle", action="store_true")
    sp.add_argument("--force", action="store_true",
                    help="also save recipes that need review (unmatched ingredients skipped)")
    sp.add_argument("--allow-duplicates", action="store_true",
                    help="save even if a recipe of the same name exists")
    sp.add_argument("--delay", type=float, default=1.0,
                    help="seconds to pause after each save (default 1.0)")
    add_commit(sp)
    sp.set_defaults(func=cmd_sync_all)

    sp = sub.add_parser("mealie-check", help="test the Mealie URL and API token")
    sp.set_defaults(func=cmd_mealie_check)

    sp = sub.add_parser("alias", help="manage the ingredient override map")
    sp.add_argument("--aliases", default="pantry.json")
    asub = sp.add_subparsers(dest="alias_cmd", required=True)
    al = asub.add_parser("list", help="list overrides")
    al.set_defaults(func=cmd_alias)
    aset = asub.add_parser("set", help="pin an ingredient to a MND foodId")
    aset.add_argument("name")
    aset.add_argument("--food-id", required=True)
    aset.add_argument("--description", default=None)
    aset.add_argument("--amount", default=None, help="optional default amount")
    aset.set_defaults(func=cmd_alias)

    return p


def _utf8_console() -> None:
    """Windows consoles default to a legacy code page (e.g. cp1252) that can't
    print characters like the report's arrows; never crash on output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    from mealie.client import MealieError

    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (MndError, MealieError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
